"""方案 B 补充：用 akshare 新浪源补北交所 920XXX 复权因子
baostock 不支持 920XXX → 改用 akshare.stock_zh_a_daily(symbol='bj920000', adjust='hfq')

输出格式与 baostock 版完全一致：date, adj_factor, qfq_close, raw_close, code
直接覆盖 data/adj_factor/{code}.parquet
"""
import socket
import sys
import time
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：让 tdxfeed.atomic 可导入
from tdxfeed.atomic import atomic_write_parquet  # SEM001 原子写

socket.setdefaulttimeout(30)
warnings.filterwarnings('ignore')

from _common import adj_factor_dir

from tdxfeed.names import load_universe  # 股票池唯一入口（P1-9）

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

OUT_DIR = adj_factor_dir()
LOG = Path('/tmp/adj_factor_bj_akshare.log')


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def fetch_one(code: str, max_retries: int = 3) -> str:
    """下载单只北交所股复权因子"""
    cache_fp = OUT_DIR / f'{code}.parquet'
    sym = f'bj{code}'

    last_err = None
    for attempt in range(max_retries):
        try:
            df_q = ak.stock_zh_a_daily(symbol=sym, adjust='hfq')
            df_r = ak.stock_zh_a_daily(symbol=sym, adjust='')

            if df_q is None or df_r is None or len(df_q) == 0 or len(df_r) == 0:
                last_err = 'empty'
                time.sleep(2 ** attempt)
                continue

            df_q = df_q[['date', 'close']].rename(columns={'close': 'qfq_close'})
            df_r = df_r[['date', 'close']].rename(columns={'close': 'raw_close'})

            df_q['date'] = pd.to_datetime(df_q['date'])
            df_r['date'] = pd.to_datetime(df_r['date'])

            merged = df_q.merge(df_r, on='date')
            if len(merged) == 0:
                last_err = 'no overlap'
                time.sleep(2 ** attempt)
                continue

            merged['adj_factor'] = (merged['qfq_close'] / merged['raw_close']).round(6)
            merged['code'] = code
            merged = merged[['code', 'date', 'adj_factor', 'qfq_close', 'raw_close']]

            atomic_write_parquet(merged, cache_fp, index=False)
            return 'ok'

        except Exception as e:
            _ERROR_COUNT['fetch_one'] += 1
            last_err = f'{type(e).__name__}: {str(e)[:50]}'
            time.sleep(2 ** attempt + 0.5)

    return last_err


def main():
    raise SystemExit('已移除：北交所数据已于 2026-10-21 下线，本脚本不再使用')
    LOG.unlink(missing_ok=True)
    log('=== 方案 B 补充启动 (akshare 新浪源 · 北交所 920XXX) ===')

    codes = sorted(load_universe())

    targets = [c for c in codes if c.startswith('920') and not (OUT_DIR / f'{c}.parquet').exists()]
    log(f'目标: {len(targets)} 只 (北交所 920XXX，未下)')

    ok = fail = 0
    fail_codes = []
    t0 = time.time()
    for i, code in enumerate(targets):
        result = fetch_one(code)
        if result == 'ok':
            ok += 1
        else:
            fail += 1
            fail_codes.append((code, result))

        elapsed = time.time() - t0
        speed = (i + 1) / max(elapsed, 1)
        eta = (len(targets) - i - 1) / max(speed, 0.01) / 60
        if (i + 1) % 20 == 0 or i < 5:
            log(f'  [{i+1}/{len(targets)}] {code} {result} ok={ok} fail={fail} '
                f'speed={speed:.2f}/s ETA={eta:.0f}min')

        time.sleep(0.5)

    log(f'\n=== 完成 === ok={ok} fail={fail}  耗时={(time.time()-t0)/60:.1f}min')
    if fail_codes:
        log('失败列表:')
        for code, err in fail_codes[:30]:
            log(f'  {code}: {err}')


if __name__ == '__main__':
    main()
