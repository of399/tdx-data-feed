#!/usr/bin/env python
"""Sec（板块强度）过滤的增益验证 —— 不重训模型，纯信号层检验。

问题：把 §A.1 的板块强度 Sec 作为打板过滤器（推理期 gate）或作为模型特征，
到底有没有价值？

做法：
    1. 读打板回测明细（data/daban/daban_backtest_*.csv）
       每笔 = T 日收盘封死涨停 → 涨停价买入 → T+1 开盘卖出
    2. 读板块特征（data/factors/sector_features.csv）
       date, industry_code, sec（当日口径，入场前可知，无泄漏）
    3. join (signal_date, code) → industry_code → 当日 sec
    4. 按 sec / 板块涨停家数分组，比较胜率/平均收益/盈亏比

用法::

    venv/bin/python scripts/daban_sec_ablation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.sector import load_industry_map  # noqa: E402

BACKTEST_CSV = _ROOT / "data" / "daban" / "daban_backtest_20200101_20261019.csv"
SECTOR_CSV = _ROOT / "data" / "factors" / "sector_features.csv"


def group_stats(df: pd.DataFrame, label: str) -> dict:
    net = df["ret_net"]
    wins = df[net > 0]
    losses = df[net <= 0]
    avg_win = float(wins["ret_net"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["ret_net"].mean()) if len(losses) else 0.0
    log_eq = np.log1p(net).cumsum()
    dd = float(np.expm1(float((log_eq - log_eq.cummax()).min())))
    return {
        "组": label,
        "样本": len(df),
        "胜率%": round(float((net > 0).mean()) * 100, 1),
        "均收益%": round(float(net.mean()) * 100, 3),
        "均盈%": round(avg_win * 100, 2),
        "均亏%": round(avg_loss * 100, 2),
        "盈亏比": round(avg_win / abs(avg_loss), 2) if avg_loss else 0.0,
        "最大回撤%": round(dd * 100, 1),
    }


def main() -> int:
    bt_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BACKTEST_CSV
    if not bt_path.exists():
        print(f"[fail] 回测明细不存在: {bt_path}（先跑 scripts/daban_backtest.py --mode limit）")
        return 1
    if not SECTOR_CSV.exists():
        print(f"[fail] 板块特征不存在: {SECTOR_CSV}（先跑 scripts/daban_sector_features.py）")
        return 1

    trades = pd.read_csv(bt_path, dtype={"code": str})
    trades["code"] = trades["code"].str.zfill(6)
    trades["signal_date"] = trades["signal_date"].astype(str)

    sector = pd.read_csv(SECTOR_CSV, dtype={"industry_code": str})
    sector["date"] = sector["date"].astype(str)
    sector["industry_code"] = sector["industry_code"].str.replace(r"\.0$", "", regex=True)

    ind_map = load_industry_map()
    trades["industry_code"] = trades["code"].map(ind_map)
    merged = trades.merge(
        sector[
            [
                "date",
                "industry_code",
                "sec",
                "limit_up_n",
                "sector_pct",
                "rank_pct",
                "member_n",
            ]
        ],
        left_on=["signal_date", "industry_code"],
        right_on=["date", "industry_code"],
        how="left",
    )
    hit = merged["sec"].notna()
    print(
        f"[info] 交易 {len(merged):,} 笔 · 命中板块特征 {int(hit.sum()):,} 笔 "
        f"({hit.mean() * 100:.1f}%)"
    )
    if hit.sum() == 0:
        print("[fail] 未命中任何板块特征（日期或行业码对齐问题）")
        return 1
    df = merged[hit].copy()

    print("\n=== A. sec 四分位分组 ===")
    df["sec_q"] = pd.qcut(df["sec"], 4, labels=["Q1最低", "Q2", "Q3", "Q4最高"])
    rows = [group_stats(g, str(k)) for k, g in df.groupby("sec_q", observed=True)]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== B. sec 绝对门槛 ===")
    rows = []
    for thr in (0, 40, 50, 60, 70, 80):
        sub = df[df["sec"] >= thr]
        if len(sub) < 100:
            continue
        rows.append(group_stats(sub, f"sec>={thr}"))
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== C. 板块涨停家数（板块共振）四分位 ===")
    df["lu_q"] = pd.cut(
        df["limit_up_n"], bins=[-0.1, 0, 1, 2, 100], labels=["0家", "1家", "2家", "≥3家"]
    )
    rows = [group_stats(g, str(k)) for k, g in df.groupby("lu_q", observed=True)]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== D. 组合过滤（sec 门槛 × 板块共振）===")
    rows = []
    for sec_thr in (50, 60, 70):
        for lu_min in (1, 2, 3):
            sub = df[(df["sec"] >= sec_thr) & (df["limit_up_n"] >= lu_min)]
            if len(sub) < 200:
                continue
            rows.append(group_stats(sub, f"sec>={sec_thr} & 共振>={lu_min}"))
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== E. 分年（sec>=60 过滤前后）===")
    df["year"] = df["signal_date"].str[:4]
    rows = []
    for year, g in df.groupby("year"):
        base = group_stats(g, f"{year} 全样本")
        filt = group_stats(g[g["sec"] >= 60], f"{year} sec>=60")
        rows.append([base, filt])
    flat = [r for pair in rows for r in pair]
    print(pd.DataFrame(flat).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
