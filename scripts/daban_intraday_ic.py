#!/usr/bin/env python
"""标签换为 T+1 开盘→收盘日内收益后，29 维特征还有没有横截面预测力？

这是"重训是否值得"的最便宜筛子：不训练，只算 OOS 快照上的 rank IC 与分组收益。
对照组：同一批特征对原标签（T+1 收盘涨停）的 IC。

口径：
- 信号日 T（快照 date），T+1 开盘买入、T+1 收盘卖出
- 剔除 T+1 开盘即涨停（买不进）
- 收益 sanity 截断 ±35%
- universes: 全市场 / 连板≥1 / 连板≥2 / 连板≥3

用法::

    venv/bin/python scripts/daban_intraday_ic.py
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.scanner import daily_path, load_universe  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

SNAP = ROOT / "data/double_up/daban_pattern_eval_20261022.parquet"
DAILY_DIR = ROOT / "data/parquet/daily"
OUT = ROOT / "docs/DABAN_INTRADAY_IC_20261022.md"
MODELS = {
    "live": ROOT / "data/double_up/d_signal_5d_model_daban.txt",
    "retrain": ROOT / "data/double_up/d_signal_5d_model_daban_retrain_20261022.txt",
    "excl": ROOT / "data/double_up/d_signal_5d_model_daban_retrain_20261022_excl.txt",
}
COST = 0.003


def limit_ratio_of(code: str, is_st: bool) -> float:
    if code[:3] in ("300", "301", "688", "689"):
        return 0.20
    if code[:2] in ("83", "87", "88", "92", "43", "82", "89", "40", "42", "41",
                    "44", "45", "46", "47", "48"):
        return 0.30
    return 0.05 if is_st else 0.10


def load_next_day(code: str) -> pd.DataFrame | None:
    p = daily_path(code, DAILY_DIR)
    if p is None:
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "open", "close"])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    df["next_date"] = df["date"].shift(-1)
    df["next_open"] = df["open"].shift(-1)
    df["next_close"] = df["close"].shift(-1)
    out = df[["date", "close", "next_open", "next_close"]].dropna()
    out.columns = ["date", "close", "next_open", "next_close"]
    out["code"] = code
    return out


def ic_series(df: pd.DataFrame, cols: list[str], y: str) -> pd.DataFrame:
    """逐日 rank IC（Spearman），返回 index=date, columns=cols。"""
    d = df[["date", y, *cols]]
    r = d.groupby("date")[cols].rank()
    ry = d.groupby("date")[y].rank()
    r_dm = r - r.groupby(d["date"]).transform("mean")
    y_dm = ry - ry.groupby(d["date"]).transform("mean")
    num = r_dm.mul(y_dm, axis=0).groupby(d["date"]).sum()
    den = np.sqrt((r_dm ** 2).groupby(d["date"]).sum().mul(
        (y_dm ** 2).groupby(d["date"]).sum(), axis=0))
    return num.div(den, axis=0)


def ic_stats(ic: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows = []
    for c in cols:
        s = ic[c].dropna()
        sd = s.std(ddof=1)
        rows.append({
            "feature": c,
            "ic": float(s.mean()),
            "ic_t": float(s.mean() / (sd / np.sqrt(len(s)))) if sd else 0.0,
            "pos_day": float((s > 0).mean() * 100),
        })
    return pd.DataFrame(rows).sort_values("ic", key=lambda x: x.abs(), ascending=False)


def decile_means(df: pd.DataFrame, cols: list[str], y: str) -> pd.DataFrame:
    out = {}
    for c in cols:
        d = df[["date", c, y]].dropna()
        r = d.groupby("date")[c].rank(pct=True)
        dec = np.ceil(r * 10).clip(1, 10).astype(int)
        m = d.groupby([d["date"], dec])[y].mean().groupby(level=1).mean()
        out[c] = m
    return pd.DataFrame(out).T


def universe_report(df: pd.DataFrame, cols: list[str], label: str) -> list[str]:
    lines = [f"### {label}", "",
             f"样本 {len(df):,} 行 / {df.date.nunique()} 天；"
             f"日内收益均值 {df.ret.mean() * 100:+.3f}%（gross），"
             f"中位 {df.ret.median() * 100:+.3f}%，"
             f"胜率 {(df.ret > 0).mean() * 100:.1f}%；"
             f"原标签（次日收盘涨停）均值 {df.label.mean() * 100:.2f}%。", ""]

    ic = ic_series(df, cols, "ret")
    st = ic_stats(ic, cols)
    lab_ic = ic_stats(ic_series(df, cols, "label"), cols).set_index("feature")["ic"]
    st["ic_label"] = st["feature"].map(lab_ic)
    lines += ["| 特征 | IC(日内收益) | t | 正IC天数% | IC(次日涨停) |",
              "|---|---|---|---|---|"]
    for r in st.head(12).itertuples(index=False):
        lines.append(f"| {r.feature} | {r.ic:+.4f} | {r.ic_t:+.2f} | {r.pos_day:.1f} | {r.ic_label:+.4f} |")
    lines.append("")

    top = st.head(5)["feature"].tolist()
    dm = decile_means(df, top, "ret")
    lines += ["| 特征 | D1(低)% | D10(高)% | D10-D1% | D10 扣成本% | D1-D10% |",
              "|---|---|---|---|---|---|"]
    for c, row in dm.iterrows():
        lines.append(f"| {c} | {row[1] * 100:+.3f} | {row[10] * 100:+.3f} | "
                     f"{(row[10] - row[1]) * 100:+.3f} | {(row[10] - COST) * 100:+.3f} | "
                     f"{(row[1] - row[10]) * 100:+.3f} |")
    lines.append("")

    # 现役/候选模型的评分 IC 与 Top-k（净收益 = gross - 0.30%）
    lines += ["| 模型 | 评分IC | IC t | Top3净% | Top5净% | Top10净% |",
              "|---|---|---|---|---|---|"]
    for name in MODELS:
        s = ic_series(df, [f"score_{name}"], "ret")[f"score_{name}"].dropna()
        sd = s.std(ddof=1)
        t = s.mean() / (sd / np.sqrt(len(s))) if sd else 0.0
        vals = []
        for k in (3, 5, 10):
            topk = df.sort_values(["date", f"score_{name}"], ascending=[True, False]).groupby("date").head(k)
            vals.append(f"{(topk.ret.mean() - COST) * 100:+.3f}")
        lines.append(f"| {name} | {s.mean():+.4f} | {t:+.2f} | " + " | ".join(vals) + " |")
    lines.append("")
    return lines


def main() -> int:
    universe = load_universe()
    codes = list(universe.keys())
    print(f"[info] 读取 {len(codes)} 只日线 ...")
    parts = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for part in pool.map(load_next_day, codes):
            if part is not None:
                parts.append(part)
    px = pd.concat(parts, ignore_index=True)
    print(f"[info] 价格表 {len(px):,} 行")

    snap = pd.read_parquet(SNAP)
    snap["code"] = snap["code"].astype(str).str.zfill(6)
    snap["date"] = snap["date"].astype(str).str.replace("-", "", regex=False)
    px["d"] = px["date"].str.replace("-", "", regex=False)
    snap = snap.merge(px[["d", "code", "close", "next_open", "next_close"]],
                      left_on=["date", "code"], right_on=["d", "code"], how="left")
    snap = snap.drop(columns=["d"])

    snap["ret"] = snap["next_close"] / snap["next_open"] - 1.0
    ratio = np.array([limit_ratio_of(c, bool(s)) for c, s in zip(snap.code, snap.is_st, strict=True)])
    limit_px = np.round(snap["close"].to_numpy() * (1 + ratio), 2)
    unbuyable = snap["next_open"].to_numpy() >= limit_px - 0.005
    snap["buyable"] = ~unbuyable

    before = len(snap)
    # 与 chase 回测同一合理性闸门：开盘→收盘不可能超过当日涨跌幅限制
    plausible = snap.ret.ge(-ratio - 0.01) & snap.ret.le(2 * ratio + 0.01)
    snap = snap[snap.buyable & snap.ret.notna() & snap.next_open.gt(0)
                & snap["close"].gt(0) & plausible & snap.ret.abs().le(0.60)].copy()
    print(f"[info] 可买且价格有效 {before:,} → {len(snap):,} 行（剔除开盘涨停/异常）")

    mdls = {}
    for name, path in MODELS.items():
        m = lgb.Booster(model_file=str(path))
        mdls[name] = m
        snap[f"score_{name}"] = m.predict(snap[m.feature_name()])
    feat_cols = mdls["live"].feature_name()

    print("[info] 计算 IC 分组 ...")
    lines = ["# daban 日内收益标签的可预测性（OOS 快照，2025-06 ~ 2026-10）", "",
             "> 问题：把训练目标从「T+1 收盘涨停」换成「T+1 开盘→收盘日内收益」，29 维特征还有没有横截面预测力？",
             "> 这是重训前的免费筛子：只算 rank IC / 分组收益，不训练。",
             "",
             f"样本：`daban_pattern_eval_20261022.parquet`，{len(snap):,} 行可买样本，{snap.date.nunique()} 天。",
             "剔除 T+1 开盘即涨停（买不进），以及超出当日涨跌幅限制的异常价（与回测同一合理性闸门）。",
             "IC = 逐日 Spearman 秩相关（特征值 vs T+1 开盘→收盘收益），t = 日均 IC / (日 IC 标准差/√天数)。",
             ""]
    for label, sub in [
        ("全市场（可买）", snap),
        ("连板 ≥1（涨停池）", snap[snap.consecutive_boards >= 1]),
        ("连板 ≥2", snap[snap.consecutive_boards >= 2]),
        ("连板 ≥3", snap[snap.consecutive_boards >= 3]),
    ]:
        if len(sub):
            lines += universe_report(sub, feat_cols, label)

    lines += ["## 读法", "",
              "- 若所有特征对日内收益的 |IC| 都 <0.02、t <2，且 D10-D1 扣成本后为负 → 29 维日频特征无法预测日内接力收益，重训不值得做。",
              "- 若个别特征 |IC| ≥0.03 且分组单调 → 换标签重训有依据，先做单特征策略验证。",
              "- `IC(次日涨停)` 列是原标签的对照：特征对原标签越有效、对日内收益越无效，越说明旧模型学的是不可交易的隔夜溢价。",
              ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:80]))
    print(f"\n[ok] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
