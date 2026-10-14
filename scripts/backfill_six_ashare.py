"""对 6 只滞后 A 股用 akshare/腾讯补拉日K到最新可获日期，处理停牌/缺失。

数据范围: 指定 6 只 A 股 (sh601238/sh605303/sh688496/sz000016/sz002731/sz301139)
频率:    日K (daily)
复权:    前复权 qfq (与现有 parquet 口径一致，已比对确认)
字段:    date, open, high, low, close, volume(手), amount(元)
        (腾讯源无成交额 → amount 填 0；akshare 源含 amount)
处理逻辑:
  - 依次尝试 akshare(qfq) → tencent(qfq)
  - 源末日 > 现有末日: merge_write 增量合并 (保留旧历史, 不覆盖/不截断)
  - 源末日 <= 现有末日: 视为无更新 (大概率停牌), 不写盘, 标记 NOOP
  - 两源均无数据: 视为退市/彻底缺失, 标记 SKIP
用法: venv/bin/python scripts/backfill_six_ashare.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from tdxfeed.sources.akshare_source import AkshareSource
from tdxfeed.sources.tencent_source import TencentSource
from tdxfeed.writers import Writers

OUT_ROOT = os.environ.get("TDX_OUT_ROOT", "/home/jiuben/tdx-data-feed/data")
TARGETS = ["sh601238", "sh605303", "sh688496", "sz000016", "sz002731", "sz301139"]


def _ymd(d) -> int:
    return int(str(d).replace("-", "").replace(" ", "").replace(":", "")[:8])


def main():
    w = Writers(OUT_ROOT)
    ak = AkshareSource()
    tx = TencentSource()
    results = []

    for sym in TARGETS:
        pf = os.path.join(OUT_ROOT, "parquet", "daily", f"{sym}.parquet")
        exist_max = None
        if os.path.exists(pf):
            exist_max = _ymd(pd.read_parquet(pf, columns=["date"])["date"].max())

        bars, used = None, None
        for src, name in [(ak, "akshare(qfq)"), (tx, "tencent(qfq)")]:
            try:
                b = src.fetch_bars(sym)
                if b:
                    bars, used = b, name
                    break
            except Exception as e:
                print(f"  [{sym}] {name} 失败: {e!s}")

        if not bars:
            print(f"[SKIP] {sym}: akshare 与 tencent 均无数据 (疑似退市/彻底缺失)")
            results.append((sym, exist_max, None, "无数据源/SKIP"))
            continue

        new_max = _ymd(bars[-1][0])
        if new_max <= (exist_max or 0):
            print(
                f"[NOOP] {sym}: 现有末日={exist_max}, {used} 末日={new_max} "
                "→ 无更新 (已是最新可获, 疑似停牌)"
            )
            results.append((sym, exist_max, new_max, "无更新/停牌"))
        else:
            n = w.merge_write(sym, bars)
            print(
                f"[UPDATE] {sym}: {exist_max} → {new_max} ({used}), 总行数={n}"
            )
            results.append((sym, exist_max, new_max, f"已更新/{used}"))

    print("\n===== 补拉汇总 =====")
    for sym, em, nm, st in results:
        print(f"  {sym:9s} 现有末日={em}  源末日={nm}  状态={st}")


if __name__ == "__main__":
    main()
