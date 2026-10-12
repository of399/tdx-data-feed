"""阶段一入口：拉取历史日线 → 落盘 parquet → 校验报告。

用法（在 workspace 根目录执行）：
    venv/bin/python quant/fetch_history.py --symbol 600519 --start 20240101 --end 20240901

后端固定新浪（DataFeed 默认 'sina'），避免东财后端被阻断。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from quant.datafeed import DataFeed
from quant.validate import validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("quant.fetch_history")

OUT_ROOT = Path("data/quant_demo")


def main() -> None:
    ap = argparse.ArgumentParser(description="量化阶段一：历史日线拉取+校验")
    ap.add_argument("--symbol", default="600519", help="6 位代码或 sh/sz 前缀")
    ap.add_argument("--start", default="20240101", help="YYYYMMDD / YYYY-MM-DD")
    ap.add_argument("--end", default="20240901", help="YYYYMMDD / YYYY-MM-DD")
    ap.add_argument("--source", default="sina", choices=["sina", "akshare"])
    ap.add_argument("--out", type=Path, default=OUT_ROOT, help="parquet 输出目录")
    args = ap.parse_args()

    feed = DataFeed(source=args.source)
    logger.info("fetch %s [%s~%s] via %s", args.symbol, args.start, args.end, feed.source_name)
    df = feed.fetch(args.symbol, args.start, args.end)

    args.out.mkdir(parents=True, exist_ok=True)
    code = args.symbol[-6:]
    fp = args.out / f"{code}_daily.parquet"
    df.to_parquet(fp, index=False, engine="pyarrow")
    logger.info("saved %s rows=%d", fp, len(df))

    report = validate(df)
    print("\n=== 阶段一校验报告 ===")
    print(f"symbol        : {code}")
    print(f"row_count     : {report['row_count']}")
    print(f"columns       : {report['columns']}")
    print(f"date_range    : {report['date_range'][0]} ~ {report['date_range'][1]}")
    print(f"expected_days : {report['expected_trading_days']}")
    print(f"missing_days  : {report['missing_trading_days']}  {report['missing_examples']}")
    print(f"na_rate       : {report['overall_na_rate']}")
    print(f"parquet       : {fp} ({fp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
