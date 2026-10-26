#!/usr/bin/env python
"""3+ 连板日内接力（T+1 开盘买 → T+1 收盘卖）稳健性检验。

输入 data/daban/relay_hold1_*/daban_backtest_*.csv（chase --hold 1 全历史明细）。
输出：成本敏感度、分高度、月度、市场中性 alpha（等权全市场 open→close 为基准）、
容量与尾部，写入 docs/DABAN_RELAY3_ROBUSTNESS_20261022.md。

用法::
    venv/bin/python scripts/daban_relay3_robustness.py
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.scanner import daily_path, load_universe  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

TRADES = ROOT / "data/daban/relay_hold1_20261022/daban_backtest_20200101_20261019.csv"
DAILY_DIR = ROOT / "data/parquet/daily"
OUT = ROOT / "docs/DABAN_RELAY3_ROBUSTNESS_20261022.md"


def max_dd(daily: pd.Series) -> float:
    log = np.log1p(daily).cumsum()
    return float(np.expm1((log - log.cummax()).min()))


def ann(daily: pd.Series) -> float:
    years = max(len(daily) / 243.0, 1e-9)
    return float(np.exp(np.log1p(daily).sum() / years) - 1)


def summary(daily: pd.Series, label: str) -> dict:
    t = float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))) if len(daily) > 2 else 0.0
    return {
        "口径": label,
        "交易日": len(daily),
        "日均%": round(float(daily.mean()) * 100, 3),
        "年化%": round(ann(daily) * 100, 1),
        "最大回撤%": round(max_dd(daily) * 100, 2),
        "Sharpe": round(float(daily.mean() / daily.std(ddof=1) * np.sqrt(243)), 2) if daily.std(ddof=1) else 0.0,
        "t值": round(t, 2),
    }


def market_intraday() -> pd.Series:
    universe = load_universe()
    codes = list(universe.keys())

    def one(code: str):
        p = daily_path(code, DAILY_DIR)
        if p is None:
            return None
        try:
            df = pd.read_parquet(p, columns=["date", "open", "close"])
        except Exception:
            return None
        if df is None or df.empty:
            return None
        df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
        df = df[(df["open"] > 0) & (df["close"] > 0)]
        if df.empty:
            return None
        return pd.DataFrame({"date": df["date"], "r": df["close"] / df["open"] - 1.0})

    parts = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for part in pool.map(one, codes):
            if part is not None:
                parts.append(part)
    allr = pd.concat(parts, ignore_index=True)
    return allr.groupby("date")["r"].mean().sort_index()


def ols_alpha(y: pd.Series, x: pd.Series) -> tuple[float, float, float, float]:
    j = pd.concat([y.rename("y"), x.rename("x")], axis=1, join="inner").dropna()
    X = np.column_stack([np.ones(len(j)), j["x"].to_numpy()])
    b = np.linalg.lstsq(X, j["y"].to_numpy(), rcond=None)[0]
    resid = j["y"].to_numpy() - X @ b
    n, k = X.shape
    s2 = resid @ resid / (n - k)
    cov = s2 * np.linalg.inv(X.T @ X)
    se_a, se_b = np.sqrt(cov[0, 0]), np.sqrt(cov[1, 1])
    return float(b[0]), float(b[0] / se_a), float(b[1]), float(b[1] / se_b)


def main() -> int:
    df = pd.read_csv(TRADES, dtype={"code": str})
    for c in ("consecutive",):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    g = df[df["consecutive"] >= 3].copy()
    g["entry_date"] = g["entry_date"].astype(str)
    g = g[g["ret_gross"].notna() & g["ret_net"].notna()]
    print(f"[info] 3+ 样本 {len(g)} 笔 / {g.entry_date.nunique()} 天")

    lines = ["# 3+ 连板日内接力稳健性（2020-2026）", "",
             f"样本：{len(g)} 笔，{g.entry_date.nunique()} 个交易日；"
             "口径 T 日收盘封死涨停且连板≥3 → T+1 开盘买入（开盘涨停剔除）→ T+1 收盘卖出；"
             "gross 为复权价收益，net 扣 0.30% 双边成本。", ""]

    # 1) 成本敏感度
    lines += ["## 成本敏感度", "",
              "| 双边成本 | 均收益% | 日均% | 年化% | 最大回撤% | t值 |",
              "|---|---|---|---|---|---|"]
    for cost in (0.0010, 0.0020, 0.0030, 0.0040, 0.0050):
        net = g["ret_gross"] - cost
        daily = net.groupby(g["entry_date"]).mean()
        t = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))
        lines.append(f"| {cost:.2%} | {net.mean() * 100:.3f} | {daily.mean() * 100:.3f} | "
                     f"{ann(daily) * 100:.1f} | {max_dd(daily) * 100:.2f} | {t:.2f} |")
    lines.append("")

    # 2) 分连板高度
    lines += ["## 分连板高度（成本 0.30%）", "",
              "| 连板 | 样本 | 日均笔数 | 均收益% | 胜率% | 日均% | t值 | 最大回撤% |",
              "|---|---|---|---|---|---|---|---|"]
    for lab, sub in [("3", g[g.consecutive == 3]), ("4", g[g.consecutive == 4]),
                     ("5+", g[g.consecutive >= 5]), ("3+", g)]:
        d = sub.groupby("entry_date")["ret_net"].mean()
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
        lines.append(f"| {lab} | {len(sub)} | {len(sub) / g.entry_date.nunique():.1f} | "
                     f"{sub.ret_net.mean() * 100:.3f} | {(sub.ret_net > 0).mean() * 100:.1f} | "
                     f"{d.mean() * 100:.3f} | {t:.2f} | {max_dd(d) * 100:.2f} |")
    lines.append("")

    # 3) 月度
    g["month"] = g["entry_date"].str[:6]
    m = g.groupby("month").agg(n=("ret_net", "size"), avg=("ret_net", "mean"),
                               win=("ret_net", lambda s: (s > 0).mean()))
    pos = (m["avg"] > 0).sum()
    lines += ["## 月度稳定性", "",
              f"共 {len(m)} 个月，正收益 {pos} 个月（{pos / len(m) * 100:.0f}%）；"
              f"最差月 {m['avg'].min() * 100:.2f}%（{m['avg'].idxmin()}），"
              f"最好月 {m['avg'].max() * 100:.2f}%（{m['avg'].idxmax()}）。", "",
              "| 年月 | 笔数 | 均收益% | 胜率% |", "|---|---|---|---|"]
    for idx, row in m.iterrows():
        lines.append(f"| {idx} | {int(row.n)} | {row.avg * 100:.2f} | {row.win * 100:.1f} |")
    lines.append("")

    # 4) 市场中性 alpha（等权全市场日内收益为基准）
    print("[info] 构建等权全市场日内收益基准 ...")
    mkt = market_intraday()
    daily = g.groupby("entry_date")["ret_net"].mean()
    # 市场日期是 YYYYMMDD，对齐
    alpha, t_a, beta, t_b = ols_alpha(daily, mkt)
    lines += ["## 市场中性（基准 = 全市场等权 open→close）", "",
              f"alpha（日均）= {alpha * 100:.3f}%，t = {t_a:.2f}；"
              f"beta = {beta:.2f}（t = {t_b:.2f}）；年化 alpha ≈ {(np.expm1(alpha * 243)) * 100:.1f}%。", ""]

    # 5) 容量与尾部
    per_day = g.groupby("entry_date").size()
    tail1 = (g.ret_net <= -0.095).mean()
    tail2 = (g.ret_net <= -0.15).mean()
    lines += ["## 容量与尾部", "",
              f"- 每日成交笔数：均值 {per_day.mean():.2f}，中位 {per_day.median():.0f}，"
              f"P90 {per_day.quantile(0.9):.0f}，最大 {per_day.max()}",
              f"- 单笔 ≤ -9.5%：{(g.ret_net <= -0.095).sum()} 笔（{tail1 * 100:.1f}%）",
              f"- 单笔 ≤ -15%：{(g.ret_net <= -0.15).sum()} 笔（{tail2 * 100:.1f}%）",
              f"- 单笔最差：{g.ret_net.min() * 100:.2f}%，最好：{g.ret_net.max() * 100:.2f}%",
              "- 开盘涨停买不进（2025-06+ 快照口径）：125/847 = 14.8%", ""]

    # 6) 分年
    lines += ["## 分年", "", "| 年份 | 样本 | 均收益% | 胜率% | 日均% | t值 |",
              "|---|---|---|---|---|---|"]
    for year, sub in g.groupby(g.entry_date.str[:4]):
        d = sub.groupby("entry_date")["ret_net"].mean()
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 else 0.0
        lines.append(f"| {year} | {len(sub)} | {sub.ret_net.mean() * 100:.3f} | "
                     f"{(sub.ret_net > 0).mean() * 100:.1f} | {d.mean() * 100:.3f} | {t:.2f} |")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:40]))
    print(f"\n[ok] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
