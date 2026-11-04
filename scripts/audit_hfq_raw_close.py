#!/usr/bin/env python
"""只读审计：逐标的比 `data/hfq_factor` 的 raw_close 与同名日线表的收盘，产出差异清单。

产物 `data/.state/hfq_raw_close_mismatch.json`（gitignore），除此之外零写入。

    venv/bin/python scripts/audit_hfq_raw_close.py --limit 20     # 先小批量
    venv/bin/python scripts/audit_hfq_raw_close.py                # 全市场

为什么要有这份清单（KI-DATA-004 的取证侧）
----------------------------------------
300386 的 11-02/11-03 两行把 raw_close 写成了后复权值，因子当场塌成 1.0。当时的
结论只能停在「全市场扫描显示只有这一只」，而那个扫描判据是临时写的、没落盘——
「只有 1 只」和「N 只但我只认得 1 只」在证据上是一回事。这份清单把范围变成可复核的
工件：条目、按年分布、比较行数都在文件里，任何人事后都能重跑并对照。

判据与写入侧同源：容差、跳过无基准日期的规则都取自 `tdxfeed.hfq_factor.raw_close_mismatches`，
不在这里另立一套口径。审计覆盖**全历史**，而写入侧只比本次新增行——两个范围不同是
刻意的：全历史里两源本就有修正差，把它们当缺陷会让每次 rebase 都被拦住；但"到底差多少行"
是取证问题，取证要看全量。

2026-11-04 首跑（就是本文件当前那份产物）：比对 16,783,596 行，容差外 **2,339 行 / 461 只**，
按年全部落在 1991–2018（2,195 行在 2000 年及以前），**2019 年起 0 行**。所以「只有 300386
一只」这个说法此前是猜的，现在有了反例：历史段有 461 只两源不一致，近期段一只都没有。
写入侧只比新增行的范围选择，依据是这条按年分布，不是运气。

清单读法：`items` 里的行是「两源超出容差」，不是「本地错」。判断依据是日线表——
两个源没有第三者能裁定谁对，所以这份清单只负责给出范围与分布，不负责定罪。
`codes_without_baseline` / `codes_read_failed` 是「没验到」，不等于「干净」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from tdxfeed.hfq_factor import (  # noqa: E402
    RAW_VS_DAILY_ABS,
    RAW_VS_DAILY_REL,
    _daily_closes,
    raw_close_mismatches,
)
from tdxfeed.paths import (  # noqa: E402
    hfq_factor_codes,
    hfq_factor_path,
    state_dir,
)

OUT_NAME = "hfq_raw_close_mismatch.json"


def scan_code(code: str) -> tuple[list[dict], int]:
    """一只票的全历史比对。返回 (容差外的行, 有日线基准的比较行数)。

    只读两张表的 date/raw_close 与 date/close 两列——全市场 1678 万行留在一个
    DataFrame 里会打穿内存，逐票读完即弃。
    """
    df = pd.read_parquet(hfq_factor_path(code), columns=["date", "raw_close"])
    dates = [str(d) for d in df["date"]]
    closes = _daily_closes(code, dates)
    bad = raw_close_mismatches(df, closes)
    rows = [{"date": str(d), "raw_close": r, "daily_close": c,
             "rel_dev": round(abs(r - c) / c, 6) if c else None}
            for d, r, c in bad]
    return rows, sum(1 for x in dates if x in closes)


def build_report(codes: list[str]) -> dict:
    """逐票扫描并汇总。计数与条目都来自同一次扫描，不事后重算。"""
    items: dict[str, list[dict]] = {}
    years: Counter[str] = Counter()
    rows_compared = 0
    no_baseline_cnt = 0        # 没有日线基准（缺文件 / 日期不交）：这只票等于没被验过
    read_fail_cnt = 0          # 读不动：坏 parquet、缺 hfq 文件
    for i, code in enumerate(codes, 1):
        try:
            rows, n = scan_code(code)
        except Exception as e:                      # 单只坏文件不该终止整轮审计
            print(f"  ! {code}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
            read_fail_cnt += 1
            continue
        rows_compared += n
        if n == 0:
            no_baseline_cnt += 1
        if rows:
            items[code] = rows
            years.update(r["date"][:4] for r in rows)
        if i % 500 == 0:
            print(f"  [{i}/{len(codes)}] 差异标的={len(items)} 差异行="
                  f"{sum(len(v) for v in items.values())}", flush=True)
    total_rows = sum(len(v) for v in items.values())
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "全历史：data/hfq_factor 每张表 vs 同名日线表（data/parquet/daily）",
        "tolerance": {
            "abs": RAW_VS_DAILY_ABS,
            "rel": RAW_VS_DAILY_REL,
            "rule": "取大：|raw_close - daily_close| > max(abs, rel*|daily_close|) 即计入",
            "authority": "tdxfeed/hfq_factor.py（写入侧对账用的是同一条）",
        },
        "codes_scanned": len(codes),
        "codes_without_baseline": no_baseline_cnt,
        "codes_read_failed": read_fail_cnt,
        "rows_compared": rows_compared,
        "mismatch_codes": len(items),
        "mismatch_rows": total_rows,
        "mismatch_by_year": dict(sorted(years.items())),
        "note": (
            "条目=两源超出容差，不是本地错：日线与 baostock 是两个平行源，没有第三者裁定。"
            "写入侧（tdxfeed.hfq_factor.update_one）只比本次取数窗口的行，因为历史段的"
            "两源修正差会让每次 rebase 被拦住；本清单看全量是取证需要。"
            "codes_without_baseline / codes_read_failed 是「没验到」的行数，不等于「干净」。"
        ),
        "items": items,
    }


def write_atomic(path: Path, payload: dict) -> None:
    """tmp → os.replace（SEM001）：读者不会看到半截清单。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, help="只扫前 N 只（试跑）")
    ap.add_argument("--out", default=str(state_dir() / OUT_NAME),
                    help=f"清单落点（默认 data/.state/{OUT_NAME}）")
    args = ap.parse_args(argv)

    codes = hfq_factor_codes()
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("❌ 没有可扫描的后复权因子文件", file=sys.stderr)
        return 1
    print(f"扫描 {len(codes)} 只：raw_close vs 日线收盘（全历史）", flush=True)
    report = build_report(codes)
    out = Path(args.out)
    write_atomic(out, report)
    print(f"完成：比较 {report['rows_compared']} 行 / "
          f"差异标的 {report['mismatch_codes']} 只 / 差异行 {report['mismatch_rows']} 行")
    if report["mismatch_by_year"]:
        first, last = next(iter(report["mismatch_by_year"])), max(report["mismatch_by_year"])
        print(f"按年：{first} → {last}（分布见 {out.name} 的 mismatch_by_year）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
