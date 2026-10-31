#!/usr/bin/env python
"""收益来源分析：归因 + 隔夜溢价之外的候选来源。

A. 归因：过滤前 vs 过滤后（每笔期望差 = 板块过滤贡献）
B. 退出时点/持有期分解：T+1/T+2/T+3 的 开盘/收盘/最高（最高=理论上限）
C. 分组因子扫描：T 日相对换手、板块共振家数、板块 Sec

用法::

    venv/bin/python scripts/daban_alpha_sources.py --start 20200101 --end 20261019
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from _common import daily_dir, universe_path  # noqa: E402

from tdxfeed.daban.filter import SectorFilter, load_sector_features  # noqa: E402
from tdxfeed.daban.limit_price import limit_ratio, limit_up_price  # noqa: E402
from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

DAILY_DIR = daily_dir()
COST = 0.0030
COLS = ["date", "open", "high", "low", "close", "volume"]


def load_daily(code: str) -> pd.DataFrame | None:
    from tdxfeed.daban.limit_price import exchange_prefix

    p = DAILY_DIR / f"{exchange_prefix(code)}{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=COLS)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
    return df.sort_values("date").reset_index(drop=True)


def scan_one(code: str, name: str, start: str, end: str) -> list[dict]:
    """枚举某只股票所有 T 日封板样本及其后 3 日价格路径。"""
    df = load_daily(code)
    if df is None or len(df) < 10:
        return []
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    if len(df) < 6:
        return []
    ratio = limit_ratio(code, name)
    op, hi, _lo, cl, vol = (
        df[c].to_numpy() for c in ("open", "high", "low", "close", "volume")
    )
    dates = df["date"].tolist()
    out: list[dict] = []
    for i in range(2, len(df) - 3):
        prev_c, cur_c = float(cl[i - 1]), float(cl[i])
        if prev_c <= 0 or cur_c <= 0:
            continue
        lp = limit_up_price(prev_c, ratio)
        if cur_c < lp - 0.005 or float(op[i]) >= lp - 0.005:
            continue
        v5 = float(np.mean(vol[max(0, i - 5) : i])) if i >= 5 else 0.0
        row = {
            "code": code,
            "signal_date": dates[i],
            "entry": lp,
            "rel_turn": float(vol[i]) / v5 if v5 > 0 else 0.0,
        }
        for k in (1, 2, 3):
            j = i + k
            if j >= len(df):
                break
            row[f"t{k}_open"] = float(op[j])
            row[f"t{k}_close"] = float(cl[j])
            row[f"t{k}_high"] = float(hi[j])
        if "t1_open" in row:
            out.append(row)
    return out


def stat_row(s: pd.Series, label: str, total: int) -> dict:
    s = s.dropna()
    if s.empty:
        return {
            "方案": label,
            "样本": 0,
            "覆盖率%": 0.0,
            "胜率%": 0.0,
            "均收益%": 0.0,
            "盈亏比": 0.0,
        }
    w, l = s[s > 0], s[s <= 0]
    return {
        "方案": label,
        "样本": len(s),
        "覆盖率%": round(len(s) / max(total, 1) * 100, 1),
        "胜率%": round(float((s > 0).mean()) * 100, 1),
        "均收益%": round(float(s.mean()) * 100, 3),
        "盈亏比": round(float(w.mean() / abs(l.mean())), 2) if len(l) else 0.0,
    }


def group_scan(df: pd.DataFrame, factor: str, bins: list, labels: list[str]) -> None:
    d = df.copy()
    d["_g"] = pd.cut(d[factor], bins=bins, labels=labels)
    rows = []
    for k, g in d.groupby("_g", observed=True):
        if len(g) < 80:
            continue
        w = g[g["ret_net"] > 0]["ret_net"].mean()
        l = abs(g[g["ret_net"] <= 0]["ret_net"].mean())
        rows.append(
            {
                "分组": str(k),
                "样本": len(g),
                "胜率%": round(float((g["ret_net"] > 0).mean()) * 100, 1),
                "均收益%": round(float(g["ret_net"].mean()) * 100, 3),
                "盈亏比": round(float(w / l), 2) if l else 0.0,
            }
        )
    if rows:
        print(f"\n--- 按 {factor} 分组 ---")
        print(pd.DataFrame(rows).to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="收益来源归因与候选拓展")
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default="20261019")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="抽样股票数（0=全量）")
    ap.add_argument("--no-filter", action="store_true", help="不做阶段0 过滤（归因对照）")
    args = ap.parse_args()

    universe = json.loads(universe_path().read_text(encoding="utf-8"))
    codes = [str(c).zfill(6) for c in universe]
    if args.limit:
        codes = codes[: args.limit]
    print(f"[info] 股票池 {len(codes)} 只 · {args.start}~{args.end}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for part in pool.map(
            lambda c: scan_one(c, universe.get(c, ""), args.start, args.end), codes
        ):
            rows.extend(part)
    if not rows:
        print("[warn] 无封板样本")
        return 1
    df = pd.DataFrame(rows)
    print(f"[info] 封板样本 {len(df):,} 条 / {df['signal_date'].nunique()} 个交易日")

    ind_map = load_industry_map()
    df["industry_code"] = df["code"].map(ind_map)
    sector = load_sector_features()

    flt = SectorFilter.load()
    if args.no_filter:
        flt = SectorFilter(min_sec=0, min_limitup=0)
    before = len(df)
    df = flt.apply(df, sector)
    for k in (1, 2, 3):
        for field in ("open", "close", "high"):
            col = f"t{k}_{field}"
            if col in df.columns:
                df[f"ret_{col}"] = df[col] / df["entry"] - 1.0 - COST
    df["ret_net"] = df["ret_t1_open"]

    print("\n=== A. 归因：过滤贡献 ===")
    print(f"过滤前 {before:,} 笔 → 过滤后 {len(df):,} 笔")
    print(
        f"  过滤后每笔均收益 {df['ret_net'].mean() * 100:.3f}%  "
        f"胜率 {(df['ret_net'] > 0).mean() * 100:.1f}%"
    )

    print("\n=== B. 退出时点/持有期分解（隔夜溢价之外的候选）===")
    rows_b = []
    for k in (1, 2, 3):
        for field in ("open", "close", "high"):
            col = f"ret_t{k}_{field}"
            if col in df.columns:
                rows_b.append(stat_row(df[col], f"T+{k} {field}卖", len(df)))
    print(pd.DataFrame(rows_b).to_string(index=False))
    print("  注：high = 当日最高价，属理论上限（实际能否成交另说）")

    print("\n=== C. 分组因子扫描 ===")
    group_scan(
        df,
        "rel_turn",
        [0, 0.5, 1, 1.5, 2, 3, 100],
        ["<0.5x", "0.5-1x", "1-1.5x", "1.5-2x", "2-3x", ">3x"],
    )
    if "limit_up_n" in df.columns:
        group_scan(df, "limit_up_n", [-0.1, 1, 2, 3, 100], ["1家", "2家", "3家", "≥4家"])
    if "sec" in df.columns:
        group_scan(df, "sec", [-0.1, 40, 60, 80, 200], ["<40", "40-60", "60-80", "≥80"])

    out = _ROOT / "data" / "daban" / f"alpha_sources_{args.start}_{args.end}.parquet"
    df.to_parquet(out, index=False)
    print(f"\n[ok] 明细 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
