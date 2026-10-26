#!/usr/bin/env python
"""板块过滤的时点检验 + OOS 阈值选择（打板 limit 口径）。

两个变体：
- eod  ：用信号日 T 收盘后的 sec / limit_up_n（回测常用，但盘中买入时不可得）
- lag1 ：用 T-1 的 sec / limit_up_n（T 日开盘前完全可得，无未来信息）

流程：2020-2024 选阈值（n>=500），2025-2026 评估；并给出逐年稳定性。

用法::

    venv/bin/python scripts/daban_filter_oos.py \
        --trades data/daban/unfiltered_limit_20261022/daban_backtest_20200101_20261019.csv \
        --out docs/DABAN_FILTER_OOS_20261022.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.sector import load_industry_map  # noqa: E402

DEFAULT_TRADES = ROOT / "data/daban/unfiltered_limit_20261022/daban_backtest_20200101_20261019.csv"
SECTOR = ROOT / "data/factors/sector_features.parquet"
SEC_GRID = (0, 40, 50, 60, 70, 80)
LU_GRID = (0, 1, 2, 3, 4)


def load_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str, "signal_date": str})
    df["code"] = df["code"].str.zfill(6)
    df["signal_date"] = df["signal_date"].astype(str)
    df["industry_code"] = df["code"].map(load_industry_map())
    return df


def attach_sector(trades: pd.DataFrame, sector: pd.DataFrame) -> pd.DataFrame:
    s = sector.copy()
    s["date"] = s["date"].astype(str)
    s["industry_code"] = s["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
    cols = ["date", "industry_code", "sec", "limit_up_n"]
    dates = sorted(s["date"].unique())
    prev = {d: dates[i - 1] for i, d in enumerate(dates) if i > 0}
    t = trades.copy()
    t["prev_date"] = t["signal_date"].map(prev)
    eod = t.merge(s[cols], left_on=["signal_date", "industry_code"],
                  right_on=["date", "industry_code"], how="left")
    eod = eod.drop(columns=["date"])
    lag = t[["signal_date", "prev_date", "industry_code"]].merge(
        s[cols], left_on=["prev_date", "industry_code"],
        right_on=["date", "industry_code"], how="left")
    eod["sec_lag1"] = lag["sec"].to_numpy()
    eod["lu_lag1"] = lag["limit_up_n"].to_numpy()
    return eod


def metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "days": 0, "per_day": 0.0, "win": None, "avg": None,
                "median": None, "t": None, "daily": None, "total": None, "mdd": None}
    net = df["ret_net"].astype(float)
    daily = df.groupby("signal_date")["ret_net"].mean().sort_index()
    eq = (1 + daily).cumprod()
    sd = net.std(ddof=1)
    return {
        "n": len(df),
        "days": df["signal_date"].nunique(),
        "per_day": len(df) / max(df["signal_date"].nunique(), 1),
        "win": float((net > 0).mean()),
        "avg": float(net.mean()),
        "median": float(net.median()),
        "t": float(net.mean() / (sd / np.sqrt(len(net)))) if len(net) > 2 and sd > 0 else None,
        "daily": float(daily.mean()),
        "total": float(eq.iloc[-1] - 1) if len(eq) else 0.0,
        "mdd": float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0,
    }


def subset(df: pd.DataFrame, variant: str, ms: int, ml: int) -> pd.DataFrame:
    sec = "sec" if variant == "eod" else "sec_lag1"
    lu = "limit_up_n" if variant == "eod" else "lu_lag1"
    keep = df[sec].notna()
    if ms > 0:
        keep &= df[sec] >= ms
    if ml > 0:
        keep &= df[lu] >= ml
    return df[keep]


def pct(x: float | None, digits: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}"


def row(label: str, m: dict) -> str:
    t = "—" if m["t"] is None else f"{m['t']:.1f}"
    return (f"| {label} | {m['n']} | {m['days']} | {m['per_day']:.1f} | "
            f"{pct(m['win'], 1)} | {pct(m['avg'], 3)} | {pct(m['median'], 2)} | "
            f"{t} | {pct(m['daily'], 3)} | {pct(m['total'], 1)} | {pct(m['mdd'], 1)} |")


HEADER = ("| 方案 | 笔数 | 信号日 | 笔/日 | 胜率% | 均净% | 中位% | t | 日均% | 累计% | 最大回撤% |\n"
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default=str(DEFAULT_TRADES))
    ap.add_argument("--sector", default=str(SECTOR))
    ap.add_argument("--select", default="20200101,20241231")
    ap.add_argument("--eval", default="20250101,20261019")
    ap.add_argument("--min-n", type=int, default=500)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tr = load_trades(Path(args.trades))
    tr = attach_sector(tr, pd.read_parquet(args.sector))
    print(f"[info] 交易 {len(tr)} 笔 · 板块对齐失败 {int(tr['sec'].isna().sum())} 笔")
    sel0, sel1 = args.select.split(",")
    ev0, ev1 = args.eval.split(",")
    sel = tr[(tr["signal_date"] >= sel0) & (tr["signal_date"] <= sel1)]
    ev = tr[(tr["signal_date"] >= ev0) & (tr["signal_date"] <= ev1)]
    print(f"[info] 选择期 {sel0}-{sel1}: {len(sel)} 笔 · 评估期 {ev0}-{ev1}: {len(ev)} 笔")

    lines = [f"# 板块过滤时点检验与 OOS 阈值选择（{pd.Timestamp.now():%Y-%m-%d}）", "",
             f"- 底表：`{args.trades}`（未过滤 limit 口径，T 日涨停价买入 → T+1 开盘卖，成本 0.30%）",
             f"- 选择期：{sel0}-{sel1}；评估期：{ev0}-{ev1}；选择约束 n≥{args.min_n}",
             "- `eod` = 信号日 T 收盘值（盘中买入时不可得，含未来信息）",
             "- `lag1` = T-1 值（T 日开盘前可得，无未来信息）", ""]

    best: dict[str, tuple] = {}
    for variant in ("eod", "lag1"):
        lines.append(f"## {variant} 网格（选择期）")
        lines.append(HEADER)
        lines.append(row("无过滤", metrics(sel)))
        cand = []
        for ms in SEC_GRID:
            for ml in LU_GRID:
                if ms == 0 and ml == 0:
                    continue
                m = metrics(subset(sel, variant, ms, ml))
                lines.append(row(f"sec≥{ms} & lu≥{ml}", m))
                if m["n"] >= args.min_n:
                    cand.append((m["avg"], ms, ml, m))
        if cand:
            cand.sort(key=lambda x: x[0], reverse=True)
            best[variant] = cand[0]
        lines.append("")

    lines.append("## 选择结果与 OOS 评估")
    lines.append(HEADER)
    lines.append(row("无过滤 (eval)", metrics(ev)))
    for variant in ("eod", "lag1"):
        if variant in best:
            _, ms, ml, _ = best[variant]
            lines.append(row(f"{variant} 选择 sec≥{ms}&lu≥{ml} (eval)",
                             metrics(subset(ev, variant, ms, ml))))
    lines.append(row("现行口径 eod sec≥60&lu≥2 (eval)", metrics(subset(ev, "eod", 60, 2))))
    lines.append(row("同阈值 lag1 sec≥60&lu≥2 (eval)", metrics(subset(ev, "lag1", 60, 2))))
    lines.append("")

    lines.append("## 逐年稳定性")
    lines.append(HEADER)
    for label, variant, ms, ml in (
        ("无过滤", "eod", 0, 0),
        ("eod sec≥60&lu≥2", "eod", 60, 2),
        ("lag1 sec≥60&lu≥2", "lag1", 60, 2),
    ):
        lines.append(f"| **{label}** | | | | | | | | | | |")
        for year in [str(y) for y in range(2020, 2027)]:
            sub = subset(tr[tr["signal_date"].str.startswith(year)], variant, ms, ml)
            lines.append(row(f"{label} {year}", metrics(sub)))
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
