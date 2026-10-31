#!/usr/bin/env python3
"""**已废弃**（2026-10-29）：口径错误，勿调用；替代入口 `python -m tdxfeed.hfq_factor`。

下载后复权因子（hfq）到 data/hfq_factor/{code}.parquet —— 旧实现，只作历史留档。
- 旧逻辑把 baostock adjustflag=3 当后复权是错的：3 是**不复权**（实测与
  data/parquet/daily 的 close 逐日相等），2 才是前复权。所以本脚本算出的
  `hfq_factor = hfq_close / qfq_close` 实际是 **1/前复权因子**，不是后复权因子。
- 它命中已存在文件即 `return "cached"`，不负责更新；缺失文件也不该拿它补——
  `main()` 现在直接拒绝运行（退出码 1，在任何参数解析与写盘之前）。
- 2026-10-29 起 data/hfq_factor/ 已按真后复权重建：adjustflag=1（后复权）与
  adjustflag=3（不复权），`hfq_factor = hfq_close / raw_close`，schema 为
  code/date/hfq_factor/raw_close/hfq_close（`date` 是 date32[day]）。增量与补建
  都走 `venv/bin/python -m tdxfeed.hfq_factor`（缺失文件从 2020-01-01 起建表）。
- 函数体保留不删是仓库约定（退役脚本留文件便于回滚）；要回滚得连同 `main()`
  开头的拒绝一起删掉，并清楚它写出的表是错口径。
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

import pandas as pd  # noqa: E402
from _common import hfq_factor_dir, universe_path  # noqa: E402

from tdxfeed.parquet_io import write_table  # noqa: E402

OUT_DIR = hfq_factor_dir()
# OUT_DIR.mkdir() 随废弃一并移除：import 阶段不许有任何写盘动作（main() 在参数解析前
# 就退出，`--limit 1` 之类必须连目录都不碰）。回滚本脚本时在 fetch_one 开头自行补建。
ALL_STOCKS = universe_path()
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
    # 拒绝运行放在第一行：参数解析、baostock 登录、任何写盘都在它之后，全部不可达。
    print("已废弃：该脚本把 adjustflag=3（不复权）当作后复权，写出的表是错的。\n"
          "请改用: venv/bin/python -m tdxfeed.hfq_factor", file=sys.stderr)
    sys.exit(1)

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
