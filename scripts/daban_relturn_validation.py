#!/usr/bin/env python
"""rel_turn（缩量涨停）维度落地前的三项关键检验。

1. **分年稳定性**：<0.5x 子集逐年是否都正？（防单年/单票主导）
2. **收益集中度**：Top10% 样本贡献了多少收益？（防"少数妖股"幻觉）
3. **结构拆解**：首板 vs 连板、成交额（流动性）—— 检验效应是否可执行、是否只在极端小票上

用法::

    venv/bin/python scripts/daban_relturn_validation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.limit_price import limit_ratio, limit_up_price  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

DAILY_DIR = _ROOT / "data" / "parquet" / "daily"
ALPHA = _ROOT / "data" / "daban" / "alpha_sources_20200101_20261019.parquet"


def consecutive_of(code: str, end_date: str, upto: int = 8) -> int:
    """信号日为止的连板数（含当日）。"""
    p = DAILY_DIR / f"{code}.parquet"
    if not p.exists():
        return 0
    try:
        df = pd.read_parquet(p, columns=["date", "close"])
    except Exception:
        return 0
    if df is None or df.empty:
        return 0
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
    df = df[df["date"] <= end_date].sort_values("date")
    if len(df) < 2:
        return 0
    ratio = limit_ratio(code, "")
    cl = df["close"].to_numpy()
    n = 0
    for i in range(len(df) - 1, max(len(df) - 1 - upto, 0), -1):
        prev = float(cl[i - 1])
        if prev <= 0:
            break
        if float(cl[i]) >= limit_up_price(prev, ratio) - 0.005:
            n += 1
        else:
            break
    return n


def main() -> int:
    if not ALPHA.exists():
        print(f"[fail] 缺 {ALPHA}（先跑 scripts/daban_alpha_sources.py）")
        return 1
    df = pd.read_parquet(ALPHA)
    df["year"] = df["signal_date"].astype(str).str[:4]
    df["is_low_turn"] = df["rel_turn"] < 0.5
    print(f"[info] 样本 {len(df):,}（过滤后口径）· 低换量子集 {int(df['is_low_turn'].sum()):,}")

    print("\n=== 1. 分年稳定性 ===")
    rows = []
    for year, g in df.groupby("year"):
        lo = g[g["is_low_turn"]]
        rows.append(
            {
                "年份": year,
                "全体样本": len(g),
                "全体均收益%": round(float(g["ret_net"].mean()) * 100, 3),
                "低换手样本": len(lo),
                "低换手胜率%": round(float((lo["ret_net"] > 0).mean()) * 100, 1) if len(lo) else 0,
                "低换手均收益%": round(float(lo["ret_net"].mean()) * 100, 3) if len(lo) else 0,
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 2. 收益集中度（Top10% 样本贡献占比）===")
    for label, sub in (("全体", df), ("低换手(<0.5x)", df[df["is_low_turn"]])):
        s = sub["ret_net"].sort_values(ascending=False)
        n = max(1, int(len(s) * 0.1))
        total = float(s.sum())
        top = float(s.head(n).sum())
        print(
            f"  {label:<16} 样本 {len(s):>6}  前10%贡献 {top / total * 100:>6.1f}% 的总收益"
            f"（Top{n}笔均收益 {float(s.head(n).mean()) * 100:.2f}%，其余 {float(s.iloc[n:].mean()) * 100:.2f}%）"
        )

    print("\n=== 3. 结构拆解：首板 vs 连板 ===")
    sub = df[df["is_low_turn"]].copy()
    cons = []
    for code, dt in zip(sub["code"].astype(str), sub["signal_date"].astype(str), strict=True):
        cons.append(consecutive_of(code, dt))
    sub["consecutive"] = cons
    rows = []
    for flag, g in sub.groupby(sub["consecutive"] <= 1):
        if len(g) < 20:
            continue
        rows.append(
            {
                "组": "首板" if flag else "连板(≥2)",
                "样本": len(g),
                "胜率%": round(float((g["ret_net"] > 0).mean()) * 100, 1),
                "均收益%": round(float(g["ret_net"].mean()) * 100, 3),
                "盈亏比": round(
                    float(
                        g[g["ret_net"] > 0]["ret_net"].mean()
                        / abs(g[g["ret_net"] <= 0]["ret_net"].mean())
                    ),
                    2,
                ),
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 4. 流动性检验：低换手子集的 T 日成交额分布 ===")
    _amt = pd.read_parquet(ALPHA, columns=["code", "signal_date", "ret_net"])
    print("（成交额需重算，此处用 entry 价位与相对换手近似）")
    for lo, hi, lab in ((0, 5, "<5元"), (5, 15, "5-15元"), (15, 40, "15-40元"), (40, 200, "≥40元")):
        g = df[(df["entry"] >= lo) & (df["entry"] < hi)]
        gl = g[g["is_low_turn"]]
        if len(g) < 50:
            continue
        print(
            f"  股价{lab:<9} 全部 {len(g):>6} 笔 均收益 {float(g['ret_net'].mean()) * 100:>6.3f}%"
            f"   低换手 {len(gl):>5} 笔 均收益 "
            f"{(float(gl['ret_net'].mean()) * 100) if len(gl) else 0:>6.3f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
