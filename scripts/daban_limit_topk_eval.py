#!/usr/bin/env python
"""daban 打板口径（T 日涨停价买入 → T+1 开盘卖）的模型 Top-K 评估。

在“T 日收盘封死涨停”的样本内，比较：
- 全池等权
- 模型 Top-K（live / retrain / excl）
- 叠加板块过滤：eod（T 日收盘值，含未来信息）/ lag1（T-1 值，可得）

用法::

    venv/bin/python scripts/daban_limit_topk_eval.py \
        --trades data/daban/unfiltered_limit_20261022/daban_backtest_20200101_20261019.csv \
        --out docs/DABAN_LIMIT_TOPK_20261022.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

SNAPSHOT = ROOT / "data/double_up/daban_pattern_eval_20261022.parquet"
DEFAULT_TRADES = ROOT / "data/daban/unfiltered_limit_20261022/daban_backtest_20200101_20261019.csv"
SECTOR = ROOT / "data/factors/sector_features.parquet"
MODELS = {
    "live": ROOT / "data/double_up/d_signal_5d_model_daban.txt",
    "retrain": ROOT / "data/double_up/d_signal_5d_model_daban_retrain_20261022.txt",
    "excl": ROOT / "data/double_up/d_signal_5d_model_daban_retrain_20261022_excl.txt",
}
META = ["date", "code", "consecutive_boards", "label", "label_buyable"]


def metrics(df: pd.DataFrame, k: int) -> dict:
    if df.empty:
        return {"n": 0, "days": 0, "fill": 0.0, "win": None, "avg": None,
                "daily": None, "total": None, "mdd": None}
    net = df["ret_net"].astype(float)
    daily = df.groupby("signal_date")["ret_net"].mean().sort_index()
    eq = (1 + daily).cumprod()
    n_days = df["signal_date"].nunique()
    return {
        "n": len(df), "days": n_days, "fill": len(df) / max(n_days * k, 1),
        "win": float((net > 0).mean()), "avg": float(net.mean()),
        "daily": float(daily.mean()), "total": float(eq.iloc[-1] - 1),
        "mdd": float((eq / eq.cummax() - 1).min()),
    }


def pct(x, digits=2):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}"


def row(label: str, m: dict) -> str:
    return (f"| {label} | {m['n']} | {m['days']} | {pct(m['win'], 1)} | "
            f"{pct(m['avg'], 3)} | {pct(m['daily'], 3)} | {pct(m['total'], 1)} | "
            f"{pct(m['mdd'], 1)} |")


HEADER = ("| 方案 | 笔数 | 信号日 | 胜率% | 均净% | 日均% | 累计% | 最大回撤% |\n"
          "|---|---:|---:|---:|---:|---:|---:|---:|")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default=str(DEFAULT_TRADES))
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--sector", default=str(SECTOR))
    ap.add_argument("--k", default="3,5,10")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    trade = pd.read_csv(args.trades, dtype={"code": str, "signal_date": str})
    trade["code"] = trade["code"].str.zfill(6)
    trade["signal_date"] = trade["signal_date"].astype(str)

    names = list(MODELS)
    boosters = {n: lgb.Booster(model_file=str(p)) for n, p in MODELS.items()}
    feats = boosters[names[0]].feature_name()
    snap = pd.read_parquet(args.snapshot, columns=list(dict.fromkeys(META + feats)))
    snap["date"] = parse_date_series(snap["date"]).dt.strftime("%Y%m%d")
    snap["code"] = snap["code"].astype(str).str.zfill(6)
    for n, b in boosters.items():
        snap[f"score_{n}"] = b.predict(snap[feats])

    df = trade.merge(snap[["date", "code", "consecutive_boards"]
                          + [f"score_{n}" for n in names]],
                     left_on=["signal_date", "code"], right_on=["date", "code"], how="left")
    df = df.drop(columns=["date"])
    print(f"[info] 交易 {len(trade)} · 快照区间内可打分 {df['consecutive_boards'].notna().sum()}")

    sec = pd.read_parquet(args.sector)
    sec["date"] = sec["date"].astype(str)
    sec["industry_code"] = sec["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
    df["industry_code"] = df["code"].map(load_industry_map())
    dates = sorted(sec["date"].unique())
    prev = {d: dates[i - 1] for i, d in enumerate(dates) if i > 0}
    df["prev_date"] = df["signal_date"].map(prev)
    cols = ["date", "industry_code", "sec", "limit_up_n"]
    df = df.merge(sec[cols], left_on=["signal_date", "industry_code"],
                  right_on=["date", "industry_code"], how="left")
    df = df.drop(columns=["date"])
    lag = df[["signal_date", "prev_date", "industry_code"]].merge(
        sec[cols], left_on=["prev_date", "industry_code"], right_on=["date", "industry_code"], how="left")
    df["sec_lag1"] = lag["sec"].to_numpy()
    df["lu_lag1"] = lag["limit_up_n"].to_numpy()

    df = df[df["signal_date"] >= "20250601"].copy()
    print(f"[info] 评估期 2025-06-01 起: {len(df)} 笔 · {df['signal_date'].nunique()} 日")

    ks = [int(x) for x in args.k.split(",")]
    lines = [f"# daban 打板口径 Top-K 评估（{pd.Timestamp.now():%Y-%m-%d}）", "",
             "- 样本：T 日收盘封死涨停（未过滤），买入 = T 日涨停价，卖出 = T+1 开盘，成本 0.30%",
             "- 评估期：2025-06-01 起（模型样本外；快照覆盖上限）",
             "- ⚠ 该样本的成交假设不可执行：封死的涨停价上买不到，详见 `DABAN_SALVAGE_20261022.md`",
             "- 过滤 eod = T 日收盘 sec/limit_up_n（未来信息）；lag1 = T-1 值（可得）", ""]

    variants = (("无过滤", None, None),
                ("eod sec>=60&lu>=2", "sec", "limit_up_n"),
                ("lag1 sec>=60&lu>=2", "sec_lag1", "lu_lag1"))
    for label, scol, lcol in variants:
        sub = df if scol is None else df[(df[scol] >= 60) & (df[lcol] >= 2)]
        sub = sub[sub["consecutive_boards"].notna()]
        lines.append(f"## {label}（全池 {len(sub)} 笔）")
        lines.append(HEADER)
        lines.append(row("全池等权", metrics(sub, 1)))
        for n in names:
            for k in ks:
                picks = (sub.sort_values(["signal_date", f"score_{n}"], ascending=[True, False],
                                         kind="mergesort")
                         .groupby("signal_date", sort=False).head(k))
                lines.append(row(f"{n} Top{k}", metrics(picks, k)))
        lines.append("")

    text = "\n".join(lines)
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"[ok] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
