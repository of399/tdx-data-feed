"""双写输出：vipdoc .day（32字节/条）+ Parquet/CSV。

.day 记录布局（小端）：<i i i i i f i i>
  date(YYYYMMDD int32) | open*100 | high*100 | low*100 | close*100 (int32)
  | amount(元 float32) | volume(手 int32) | reserved(0)
"""
import os
import struct
import threading
from pathlib import Path

import pandas as pd

from tdxfeed.amount_fix import fill_missing_amount_marked, ledger_path_for, merge_estimated
from tdxfeed.parquet_io import write_table


class Writers:
    def __init__(self, out_root):
        self.out_root = out_root

    @staticmethod
    def _norm_date(d):
        import datetime as _dt
        if isinstance(d, (_dt.datetime, _dt.date, pd.Timestamp)):
            return int(d.strftime('%Y%m%d'))
        if isinstance(d, (int, float)):
            return int(d)
        s = str(d).replace('-', '').replace(' ', '').replace(':', '')
        return int(s[:8])

    def _day_bytes(self, bars, saturate=False, int32_max=2147483647):
        buf = bytearray()
        for b in bars:
            date = self._norm_date(b[0])
            o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
            vol = round(float(b[5]))
            if saturate and vol > int32_max:
                vol = int32_max
            amt = float(b[6]) if len(b) > 6 else 0.0
            buf += struct.pack(
                '<iiiiifii',
                date,
                round(o * 100), round(h * 100),
                round(l * 100), round(c * 100),
                amt, vol, 0,
            )
        return bytes(buf)

    def _write_day_atomic(self, symbol, buf):
        """tmp→rename：就地截断会让同期读 .day 的进程读到半截文件。"""
        d = os.path.join(self.out_root, symbol[:2], 'lday')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, symbol + '.day')
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, 'wb') as f:
                f.write(buf)
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return p

    def write_day(self, symbol, bars):
        return self._write_day_atomic(symbol, self._day_bytes(bars))

    def write_day_safe(self, symbol, bars, int32_max=2147483647):
        """write_day 的容量安全版：volume 超 int32 上限时饱和截断，避免 struct 溢出。"""
        return self._write_day_atomic(
            symbol, self._day_bytes(bars, saturate=True, int32_max=int32_max))

    @staticmethod
    def _date_int(x):
        """任意日期表示 → int YYYYMMDD（统一合并键）"""
        import datetime as _dt
        if isinstance(x, (_dt.datetime, _dt.date, pd.Timestamp)):
            return int(x.strftime('%Y%m%d'))
        return int(str(x).replace('-', '').replace(' ', '').replace(':', '')[:8])

    @staticmethod
    def _write_parquet_date32(p: str, df: pd.DataFrame) -> None:
        """写 daily parquet：date 强制 date32 并原子替换（实现见 tdxfeed/parquet_io.py）。"""
        write_table(Path(p), df)

    def merge_write(self, symbol, new_bars, category='daily'):
        """增量合并写：新 bars 与已有 parquet 按 date 合并（新覆盖旧），
        防止 full_sync 的 1023 根覆盖/截断已回填的全历史。
        同时用合并结果重建 .day 文件，保证两格式一致。
        date 列统一为 int YYYYMMDD（兼容 Sina datetime / akshare 字符串混并）。"""
        d = os.path.join(self.out_root, 'parquet', category)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, symbol + '.parquet')
        new = pd.DataFrame(
            new_bars, columns=['date', 'open', 'high', 'low', 'close',
                               'volume', 'amount'])
        for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            new[c] = pd.to_numeric(new[c], errors='coerce').astype('float64')
        new['date'] = new['date'].apply(self._date_int)
        new = new.dropna(subset=['date', 'close'])

        # 本次拿到真实成交额的日期：它们若曾被标成估算，标记要撤销
        real_days = {
            int(d) for d, a in zip(new['date'], new['amount'])
            if pd.notna(a) and float(a) > 0
        }
        est_days: set[int] = set()

        if os.path.exists(p):
            old = pd.read_parquet(p)
            old['date'] = old['date'].apply(self._date_int)
            # 单位一致性检测（防 volume 手/股混入）：新 bars 与旧 parquet
            # volume 中位数量级差 >50× 时告警（不阻断，历史混用已知）。
            try:
                om = float(old['volume'].median())
                nm = float(new['volume'].median())
                if om > 0 and nm > 0:
                    ratio = max(nm, om) / min(nm, om)
                    if ratio > 50:
                        print(f'[单位告警] {symbol}: 新vol中位={nm:,.0f} vs 旧={om:,.0f} '
                              f'相差{ratio:.0f}倍，疑似 volume 单位不一致，请核对')
            except Exception as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
                print(f"[WARN] swallowed: {e!r}")
            merged = pd.concat([old, new], ignore_index=True)
            # 用稳定排序（mergesort）：new 追加在 old 之后，并列日期下
            # keep='last' 才能可靠地让新数据覆盖旧数据（quicksort 不稳定会随机保留）。
            merged = merged.sort_values('date', kind='mergesort').reset_index(drop=True)
            # amount 保护：同日取 max（旧 akshare 全历史有 amount，
            # 新 Sina 日K amount=0，避免 0 覆盖真值）
            merged['amount'] = merged.groupby('date')['amount'].transform('max')
            merged, _, est = fill_missing_amount_marked(merged)
            est_days.update(int(d) for d in merged.loc[est, 'date'])
            merged = merged.drop_duplicates(subset=['date'], keep='last')
            merged = merged.sort_values('date', kind='mergesort').reset_index(drop=True)
        else:
            merged = new.sort_values('date', kind='mergesort').reset_index(drop=True)
            merged, _, est = fill_missing_amount_marked(merged)
            est_days.update(int(d) for d in merged.loc[est, 'date'])
        self._write_parquet_date32(p, merged)
        merge_estimated(symbol, est_days, real_days,
                        ledger_path_for(self.out_root))

        rows = merged.values.tolist()
        sat = []
        for r in rows:
            vol = round(float(r[5]))
            if vol > 2147483647:
                vol = 2147483647
            sat.append([r[0], r[1], r[2], r[3], r[4], vol, r[6]])
        self.write_day_safe(symbol, sat)
        return len(rows)
