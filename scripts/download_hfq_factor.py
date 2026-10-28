#!/usr/bin/env python3
"""下载后复权因子（hfq）到 data/hfq_factor/{code}.parquet。

【背景】
- data/adj_factor/{code}.parquet 当前 5 列：code, date, adj_factor, qfq_close, raw_close
- 实际：`raw_close` 来自 baostock adjustflag=3 = **后复权**，并非真 raw（真 raw 需 adjustflag=1）
- 本脚本输出 data/hfq_factor/，列：code, date, hfq_factor (= hfq_close / qfq_close)
- 保留 adj_factor 现状；本脚本**只新增**，不破坏 v2

数据源：baostock（adjustflag=2 qfq / adjustflag=3 hfq）

用法:
    venv/bin/python scripts/download_hfq_factor.py --limit 100
    venv/bin/python scripts/download_hfq_factor.py --codes-file scripts/missing_adj.txt
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

warnings = __import__("warnings")
warnings.filterwarnings("ignore")
socket.setdefaulttimeout(30)

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from _common import hfq_factor_dir  # noqa: E402

from tdxfeed.parquet_io import write_table  # noqa: E402

OUT_DIR = hfq_factor_dir()
OUT_DIR.mkdir(parents=True, exist_ok=True)
ALL_STOCKS = Path("/home/jiuben/tdx-data-feed/data/all_a_stocks.json")
LOG = Path("/tmp/hfq_factor.log")

START_DATE = "2020-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hfq_factor")


def to_baostock_code(short: str) -> str:
    if short.startswith(("60", "68", "90", "11", "13")):
        return f"sh.{short}"
    if short.startswith(("83", "87", "43")):
        return f"bj.{short}"
    return f"sz.{short}"


def fetch_one(bs, code: str, max_retries: int = 3) -> str:
    """下载单只 hfq 因子。"""
    fp = OUT_DIR / f"{code}.parquet"
    if fp.exists():
        return "cached"

    bs_code = to_baostock_code(code)
    last_err = None
    for attempt in range(max_retries):
        try:
            # qfq (adjustflag=2)
            rs_q = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=START_DATE, end_date=END_DATE,
                frequency="d", adjustflag="2",
            )
            data_q = []
            while rs_q.error_code == "0" and rs_q.next():
                data_q.append(rs_q.get_row_data())
            # hfq (adjustflag=3)
            rs_h = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=START_DATE, end_date=END_DATE,
                frequency="d", adjustflag="3",
            )
            data_h = []
            while rs_h.error_code == "0" and rs_h.next():
                data_h.append(rs_h.get_row_data())
            if not data_q or not data_h:
                last_err = "empty"
                time.sleep(2 ** attempt)
                continue

            df_q = pd.DataFrame(data_q, columns=["date", "qfq_close"])
            df_h = pd.DataFrame(data_h, columns=["date", "hfq_close"])
            merged = df_q.merge(df_h, on="date")
            merged["qfq_close"] = pd.to_numeric(merged["qfq_close"], errors="coerce")
            merged["hfq_close"] = pd.to_numeric(merged["hfq_close"], errors="coerce")
            merged = merged.dropna(subset=["qfq_close", "hfq_close"])
            merged = merged[merged["qfq_close"] > 0]
            if len(merged) == 0:
                last_err = "all_invalid"
                time.sleep(2 ** attempt)
                continue

            # hfq_factor = hfq / qfq（向后复权口径转换）
            merged["hfq_factor"] = (merged["hfq_close"] / merged["qfq_close"]).round(6)
            merged["code"] = code
            merged["date"] = pd.to_datetime(merged["date"], errors="coerce")
            merged = merged.dropna(subset=["date"])
            merged = merged[["code", "date", "hfq_factor", "qfq_close", "hfq_close"]]

            # v2 写入：date date32 + 原子替换（列序由 write_table 按 df 顺序保留）
            write_table(fp, merged[["code", "hfq_factor", "qfq_close", "hfq_close", "date"]])
            return "ok"
        except Exception as e:
            last_err = f"{type(e).__name__}:{str(e)[:60]}"
            time.sleep(2 ** attempt + 1)
    return f"fail:{last_err}"


def main() -> None:
    ap = argparse.ArgumentParser(description="下载后复权因子")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 只")
    ap.add_argument("--codes-file", help="代码列表文件")
    args = ap.parse_args()

    LOG.unlink(missing_ok=True)

    if args.codes_file:
        codes = [s.strip() for s in Path(args.codes_file).read_text().splitlines() if s.strip()]
        codes = [c.zfill(6) for c in codes]
    else:
        with open(ALL_STOCKS) as f:
            name_map = json.load(f)
        codes = sorted(name_map.keys())

    if args.limit:
        codes = codes[: args.limit]

    import baostock as bs
    lg = bs.login()
    log.info("baostock login: %s", lg.error_msg)
    if lg.error_code != "0":
        sys.exit(1)

    log.warning("目标 %d 只", len(codes))
    counters: dict[str, int] = {}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        # 跳过北交所 920（baostock 无数据）
        if code.startswith("920"):
            counters["skip_bj"] = counters.get("skip_bj", 0) + 1
            continue
        result = fetch_one(bs, code)
        counters[result] = counters.get(result, 0) + 1
        if i % 100 == 0 or i == len(codes):
            el = time.time() - t0
            log.info("  [%d/%d] status=%s el=%.0fs", i, len(codes), counters, el)
        time.sleep(0.05)

    bs.logout()
    log.warning("完成 counters=%s", counters)


if __name__ == "__main__":
    main()
