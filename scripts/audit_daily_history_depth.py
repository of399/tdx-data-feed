#!/usr/bin/env python
"""只读审计：每只沪深 A 股「本地起始日 vs 源探测窗首行」，产出待回填清单。

零写入（除自己那份产物 data/audit/history_depth_YYYYMMDD.json，gitignore）。

    venv/bin/python scripts/audit_daily_history_depth.py --limit 20      # 先小批量
    venv/bin/python scripts/audit_daily_history_depth.py --workers 2     # 全量

并发别贪多：2026-10-26 用 --workers 4 跑到 ~3400 请求（≈6.4 req/s）被腾讯 WAF 按 IP
拦成 HTTP 501，整轮 unknown。默认 2 是把速率压在阈值下，不是提速参数。

为什么探测而不直接下结论（§0 第 2 条）：本地行数像被钉住 ≠ 被截断。112 只老代码段
候选里 sh600925/600930/600938/601022 是真的 2022 年后上市——向源要 1990~2000 段回 0 行。
所以分类同时看两个数：本地首行、探测窗内首行；请求被拒单独计 unknown，
unknown 比例超阈值就退出码 1——审计自己不可信时，回填范围就是错的。

探测窗 [1990-01-01, 2000-12-31] 单页 1024 行，一标的一次请求。因此 source_first 是
「窗内最早**可见**」，不是上市日：1991 年上市的票在窗内有 ~2500 行，1024 行只够回到
1996 年末。真实最早一日由任务 4 的回填在翻页时给出，这里只负责判「要不要补」。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from tdxfeed.history_paging import PAGE_CAP, PageRequestError, http_page  # noqa: E402
from tdxfeed.symbols import a_share_symbols, sym_of_a_share  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import daily_dir  # noqa: E402

DAILY = daily_dir()
UNIVERSE = ROOT / "data" / "all_a_stocks.json"
TARGET_DEFAULT = 20000101
PROBE_END = "2000-12-31"   # 窗右端：窗内有行就说明源能给到 2000 年之前
PROBE_COUNT = PAGE_CAP     # 1024：实测 2000 可用、2100 起 param error，取安全阈值
FAILURE_RATIO_MAX = 0.1    # 超过就说明通道坏了，不是「这些票真的没有历史」


def plan_gaps(local_min, local_max, source_first, target: int) -> dict:
    """分类：ok / extend / listed_after_target / unknown。source_first=-1 表示请求被拒。"""
    if source_first == -1:
        return {"state": "unknown", "missing_days_est": 0}
    if source_first is None:
        return {"state": "listed_after_target", "missing_days_est": 0}
    if local_min is not None and local_min <= target:
        return {"state": "ok", "missing_days_est": 0}
    if source_first >= target:
        return {"state": "listed_after_target", "missing_days_est": 0}
    end = local_min if local_min is not None else int(
        dt.date.today().strftime("%Y%m%d"))
    days = (pd.Timestamp(str(end)) - pd.Timestamp(str(source_first))).days
    return {"state": "extend", "missing_days_est": max(0, int(days * 240 / 365))}


def local_span(sym: str):
    """本地日线的 [首行, 末行]（int YYYYMMDD）；文件缺失或空返回 (None, None)。"""
    p = DAILY / f"{sym}.parquet"
    if not p.exists():
        return None, None
    df = pd.read_parquet(p, columns=["date"])
    if df.empty:
        return None, None
    col = df["date"]
    # v2 schema 是 date32 → object(datetime.date)；老文件可能是 int YYYYMMDD
    d = (pd.to_datetime(col.astype(str), format="%Y%m%d")
         if col.dtype.kind in "iu" else pd.to_datetime(col))
    return int(d.min().strftime("%Y%m%d")), int(d.max().strftime("%Y%m%d"))


def probe_first_window(sym: str) -> list:
    """探测窗内的原始行。空列表 = 窗内真没数据；PageRequestError = 请求被拒。"""
    return http_page(sym, PROBE_END, PROBE_COUNT, "")


def audit_one(sym: str, target: int, probe=None) -> dict:
    probe = probe if probe is not None else probe_first_window
    lo, hi = local_span(sym)
    if lo is not None and lo <= target:
        # 本地已经在 target 之前，不用问源。实测 universe 5221 只里 747 只属于这一类
        # （最早 19901219＝深发展），占 14.3%——这条跳过省下的是 14% 请求，不是大半。
        return {"state": "ok", "missing_days_est": 0, "symbol": sym,
                "local_min": lo, "local_max": hi, "source_first": None,
                "error": None, "probed": False}
    err = None
    try:
        rows = probe(sym)
        first = int(str(rows[0][0]).replace("-", "")) if rows else None
    except PageRequestError as e:
        first, err = -1, f"PageRequestError:{str(e)[:120]}"
    except Exception as e:            # 网络/解析异常一律 unknown，不掩盖
        first, err = -1, f"{type(e).__name__}:{str(e)[:120]}"
    r = plan_gaps(lo, hi, first, target)
    r.update({"symbol": sym, "local_min": lo, "local_max": hi,
              "source_first": first, "error": err, "probed": True})
    return r


def summarize(rows: list[dict], target: int) -> dict:
    by_state: dict[str, int] = {}
    missing = 0
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        missing += r["missing_days_est"]
    return {
        "target": target,
        "probe_window": {"from": 19900101, "to": int(PROBE_END.replace("-", "")),
                         "count": PROBE_COUNT},
        "total": len(rows),
        "by_state": by_state,
        "missing_days_total": missing,
        "request_failure_ratio": round(
            by_state.get("unknown", 0) / max(len(rows), 1), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--interval", type=float, default=0.6,
                    help="每次探测后的休眠；1 并发 + 0.6s ≈ 1.2 req/s，"
                         "是上次触发 WAF 速率（6.4 req/s）的 1/5")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT)
    args = ap.parse_args()

    with open(UNIVERSE, encoding="utf-8") as fh:
        syms = a_share_symbols(json.load(fh))
    if args.limit:
        syms = syms[: args.limit]
    print(f"待审计 {len(syms)} 只 target={args.target} "
          f"探测窗 1990-01-01~{PROBE_END} 单页 {PROBE_COUNT}", flush=True)

    def one(sym):
        # 节流必须落在 worker 里：ex.map 一开始就把全部符号排进队列，
        # 主线程 sleep 只是让「取结果」变慢，单 worker 仍会按自己的最快速度砸源。
        time.sleep(args.interval)
        return audit_one(sym, args.target)

    rows, done = [], 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(one, syms):
            rows.append(r)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(syms)}", flush=True)

    summary = summarize(rows, args.target)
    out = ROOT / "data" / "audit" / f"history_depth_{dt.date.today():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({**summary, "rows": rows}, fh, ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    for r in rows:
        if r["state"] == "unknown":
            print(f"  unknown {r['symbol']}: {r['error']}", flush=True)
    print(f"产物 → {out}", flush=True)
    return 1 if summary["request_failure_ratio"] > FAILURE_RATIO_MAX else 0


if __name__ == "__main__":
    sys.exit(main())
