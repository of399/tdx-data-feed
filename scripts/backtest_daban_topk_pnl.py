#!/usr/bin/env python
"""daban Top-K 逐笔 PnL 回测（T+1 开盘买入 → T+2 开盘卖出）。

信号来自 P0 全市场评估快照（模型逐日打分），交易价格来自 daily parquet：
  - T 日收盘出信号
  - T+1 开盘买入（T+1 开盘即涨停 / 停牌 → 买不进，剔除）
  - T+2 开盘卖出（A 股 T+1 制度下最早卖点）
  - 成本：佣金 2.5bp/边、印花税 5bp 卖出、滑点 5bp/边；前复权价格

用法：
    venv/bin/python scripts/backtest_daban_topk_pnl.py --model live=... --k 5
    venv/bin/python scripts/backtest_daban_topk_pnl.py \\
        --model live=data/double_up/d_signal_5d_model_daban.txt \\
        --model retrain=data/double_up/d_signal_5d_model_daban_retrain_20261022.txt \\
        --model excl=data/double_up/d_signal_5d_model_daban_retrain_20261022_excl.txt \\
        --k 5,10 --out docs/DABAN_TOPK_PNL_20261022.md
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.topk_pnl import aggregate, simulate_trade  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

DEFAULT_DATA = ROOT / "data/double_up/daban_pattern_eval_20261022.parquet"
DEFAULT_MODEL = ROOT / "data/double_up/d_signal_5d_model_daban.txt"

from _common import adj_factor_dir, daily_dir  # noqa: E402

DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
META_COLS = ["date", "code", "consecutive_boards", "label", "label_buyable"]


def _parse_models(items: list[str]) -> dict[str, Path]:
    if not items:
        items = [f"live={DEFAULT_MODEL}"]
    out: dict[str, Path] = {}
    for item in items:
        name, _, path = item.partition("=")
        p = Path(path or name)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise SystemExit(f"模型不存在：{p}")
        out[name or p.stem] = p
    return out


def _norm_dates(s: pd.Series) -> pd.Series:
    return parse_date_series(s).dt.strftime("%Y-%m-%d")


def load_prices(code6: str, cache: dict[str, pd.DataFrame | None]):
    if code6 in cache:
        return cache[code6]
    sym = exchange_prefix(code6) + code6.zfill(6)
    p = DAILY_DIR / f"{sym}.parquet"
    if not p.exists():
        cache[code6] = None
        return None
    df = pd.read_parquet(p, columns=["date", "open", "close", "volume"])
    df["date"] = _norm_dates(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    ap = ADJ_DIR / f"{code6.zfill(6)}.parquet"
    if ap.exists():
        try:
            adj = pd.read_parquet(ap)
            adj["date"] = _norm_dates(adj["date"])
            if "adj_factor" in adj.columns:
                adj["factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
            elif {"qfq_close", "raw_close"}.issubset(adj.columns):
                adj["factor"] = (pd.to_numeric(adj["qfq_close"], errors="coerce") /
                                 pd.to_numeric(adj["raw_close"], errors="coerce"))
            else:
                adj["factor"] = np.nan
            adj.loc[~np.isfinite(adj["factor"]) | (adj["factor"] <= 0), "factor"] = np.nan
            df = df.merge(adj[["date", "factor"]], on="date", how="left")
            df["adj_factor"] = df["factor"].ffill().bfill().fillna(1.0)
            df = df.drop(columns=["factor"])
        except Exception:
            df["adj_factor"] = 1.0
    else:
        df["adj_factor"] = 1.0
    cache[code6] = df
    return df


def _picks(df: pd.DataFrame, score_col: str, k: int,
           end: str | None) -> pd.DataFrame:
    sub = df[*META_COLS, score_col].copy()
    if end:
        sub = sub[sub["date"] <= end]
    sub = sub.sort_values(["date", score_col], ascending=[True, False],
                          kind="mergesort")
    return sub.groupby("date", sort=False).head(k).reset_index(drop=True)


def run_picks(picks: pd.DataFrame, score_col: str, *, k: int,
              exit_offset: int, fee_bps: float, stamp_bps: float,
              slip_bps: float, cache: dict[str, pd.DataFrame | None]) -> tuple[list[dict], Counter]:
    trades: list[dict] = []
    skips: Counter = Counter()
    idx_cache: dict[str, dict[str, int]] = {}
    for row in picks.itertuples(index=False):
        code = str(row.code).zfill(6)
        pdf = load_prices(code, cache)
        if pdf is None:
            skips["no_price"] += 1
            continue
        dmap = idx_cache.get(code)
        if dmap is None:
            dmap = {d: i for i, d in enumerate(pdf["date"])}
            idx_cache[code] = dmap
        idx = dmap.get(row.date)
        if idx is None:
            skips["no_signal_bar"] += 1
            continue
        r = simulate_trade(pdf, idx, code, exit_offset=exit_offset,
                           fee_bps=fee_bps, stamp_bps=stamp_bps,
                           slip_bps=slip_bps)
        if r["status"] != "ok":
            skips[r["reason"]] += 1
            continue
        r.update({
            "score": float(getattr(row, score_col)),
            "label": int(row.label),
            "label_buyable": int(row.label_buyable),
            "consecutive_boards": int(row.consecutive_boards),
        })
        trades.append(r)
    return trades, skips


def _pct(x, digits=2) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}%"


def _num(x, digits=3) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def _overview_row(name: str, k: int, s: dict) -> str:
    return (f"| {name} | {k} | {s['n_trades']} | {_pct(s['fill_rate'], 1)} | "
            f"{_pct(s['win_rate'], 1)} | {_pct(s['avg_trade_return'])} | "
            f"{_num(s['profit_factor'], 2)} | {_pct(s['total_return'], 1)} | "
            f"{_pct(s['annualized_return'], 1)} | {_num(s['sharpe'], 2)} | "
            f"{_pct(s['max_drawdown'], 1)} |")


def run_pool_baseline(df: pd.DataFrame, *, exit_offset: int, fee_bps: float,
                      stamp_bps: float, slip_bps: float, end: str | None,
                      cache: dict[str, pd.DataFrame | None]):
    """T 日已涨停全池等权，同执行口径（T+1 开盘买、T+2 开盘卖）。"""
    sub = df[META_COLS].copy()
    if end:
        sub = sub[sub["date"] <= end]
    sub = sub[sub["consecutive_boards"] >= 1].reset_index(drop=True)
    if sub.empty:
        return [], Counter(), 0, 0
    sub["score_pool"] = 0.0
    trades, skips = run_picks(sub, "score_pool", k=0, exit_offset=exit_offset,
                              fee_bps=fee_bps, stamp_bps=stamp_bps,
                              slip_bps=slip_bps, cache=cache)
    return trades, skips, int(sub["date"].nunique()), len(sub)


def _subset_stats(trades: list[dict], key: str) -> dict:
    if not trades:
        return {"n_trades": 0, "win_rate": None, "avg_trade_return": None,
                "profit_factor": None}
    df = pd.DataFrame(trades)
    net = df["ret_net"]
    wins = net[net > 0]
    losses = net[net <= 0]
    return {
        "n_trades": len(df),
        "win_rate": float((net > 0).mean()),
        "avg_trade_return": float(net.mean()),
        "profit_factor": (float(wins.sum() / abs(losses.sum()))
                          if len(losses) and losses.sum() < 0 else None),
        "key": key,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="daban Top-K T+1 开盘 PnL 回测")
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--k", default="5,10")
    ap.add_argument("--exit-offset", type=int, default=2,
                    help="卖出日偏移：2=T+2 开盘（默认，T+1 最早卖点）")
    ap.add_argument("--fee-bps", type=float, default=2.5)
    ap.add_argument("--stamp-bps", type=float, default=5.0)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--end", default="", help="信号日上限 YYYY-MM-DD")
    ap.add_argument("--clean-end", default="2026-09-15",
                    help="数据完整期上限（amount 缺口前），留空跳过该对照")
    ap.add_argument("--limit-days", type=int, default=0, help="冒烟：只取前 N 个信号日")
    ap.add_argument("--out", default=str(ROOT / "docs/DABAN_TOPK_PNL_20261022.md"))
    ap.add_argument("--trades-out", default=str(ROOT / "data/daban/daban_topk_pnl_trades_20261022.csv"))
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-pool-baseline", action="store_true",
                    help="跳过多日涨停全池等权对照（约多 20-30 秒）")
    args = ap.parse_args()

    if args.exit_offset < 2:
        raise SystemExit("--exit-offset 必须 >=2（A 股 T+1）")
    k_list = sorted({int(x) for x in args.k.split(",") if x.strip()})
    if not k_list or min(k_list) < 1:
        raise SystemExit("--k 必须是正整数列表")

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = ROOT / data_path
    if not data_path.exists():
        raise SystemExit(f"评估快照不存在：{data_path}")

    models = {n: lgb.Booster(model_file=str(p))
              for n, p in _parse_models(args.model).items()}
    feature_cols = sorted({c for m in models.values() for c in m.feature_name()})
    cols = list(dict.fromkeys(META_COLS + feature_cols))
    df = pd.read_parquet(data_path, columns=cols)
    df["date"] = _norm_dates(df["date"])
    df = df.dropna(subset=["date", "label", "label_buyable"])
    if args.limit_days:
        keep = sorted(df["date"].unique())[: args.limit_days]
        df = df[df["date"].isin(keep)]
    for name, model in models.items():
        need = model.feature_name()
        missing = [c for c in need if c not in df.columns]
        if missing:
            raise SystemExit(f"模型 {name} 缺特征：{missing}")
        df[f"score_{name}"] = model.predict(df[need].fillna(0))

    all_trades: list[dict] = []
    results: dict[str, dict[int, dict]] = {}
    skips_all: dict[str, dict[int, Counter]] = {}
    cache: dict[str, pd.DataFrame | None] = {}
    for name in models:
        score_col = f"score_{name}"
        results[name] = {}
        skips_all[name] = {}
        for k in k_list:
            picks = _picks(df, score_col, k, args.end or None)
            trades, skips = run_picks(
                picks, score_col, k=k, exit_offset=args.exit_offset,
                fee_bps=args.fee_bps, stamp_bps=args.stamp_bps,
                slip_bps=args.slip_bps, cache=cache)
            for t in trades:
                t.update({"model": name, "k": k})
            all_trades.extend(trades)
            results[name][k] = aggregate(trades, picks["date"].nunique(), k)
            skips_all[name][k] = skips
            print(f"[{name}] k={k} 信号日={picks['date'].nunique()} "
                  f"成交={len(trades)} 跳过={dict(skips)}", flush=True)

    pool_stats: dict | None = None
    pool_hit: float | None = None
    pool_gross: float | None = None
    pool_skips: Counter = Counter()
    if not args.no_pool_baseline:
        pool_trades, pool_skips, n_days, n_slots = run_pool_baseline(
            df, exit_offset=args.exit_offset, fee_bps=args.fee_bps,
            stamp_bps=args.stamp_bps, slip_bps=args.slip_bps,
            end=args.end or None, cache=cache)
        if pool_trades:
            pool_stats = aggregate(pool_trades, n_days, 1)
            pool_stats["fill_rate"] = len(pool_trades) / max(n_slots, 1)
            pool_df = pd.DataFrame(pool_trades)
            pool_hit = float(pool_df["label"].mean())
            pool_gross = float(pool_df["ret_gross"].mean())
            print(f"[pool] T日涨停全池 {len(pool_trades)}/{n_slots} 笔 "
                  f"均净={pool_stats['avg_trade_return'] * 100:.3f}% "
                  f"均毛={pool_gross * 100:.3f}% 跳过={dict(pool_skips)}", flush=True)

    if not all_trades:
        print("[warn] 没有任何可成交交易")
        return 1
    trades_df = pd.DataFrame(all_trades)
    trades_df["consecutive_bucket"] = trades_df["consecutive_boards"].clip(upper=2)
    trades_df["year"] = trades_df["entry_date"].str.slice(0, 4)

    # 基准：同区间上证指数买入持有（仅作参照，策略只持有隔夜）
    bench = None
    bp = DAILY_DIR / "sh000001.parquet"
    if bp.exists():
        b = pd.read_parquet(bp, columns=["date", "close"])
        b["date"] = _norm_dates(b["date"])
        b = b.sort_values("date")
        first, last = trades_df["signal_date"].min(), trades_df["signal_date"].max()
        b = b[(b["date"] >= first) & (b["date"] <= last)]
        if len(b) >= 2:
            bench = float(b["close"].iloc[-1] / b["close"].iloc[0] - 1)

    first_k = k_list[0]
    lines = [
        f"# daban Top-K T+1 开盘买入 PnL 回测（{datetime.now():%Y-%m-%d}）",
        "",
        "## 口径",
        "",
        f"- 信号快照：`{data_path.relative_to(ROOT)}`",
        f"- 信号日范围：{trades_df['signal_date'].min()} ~ {trades_df['signal_date'].max()}",
        "- 信号：T 日收盘按模型分排序取 Top-K；T+1 开盘买入；T+2 开盘卖出",
        "- 剔除：T+1 开盘即涨停（买不进）、停牌（open/volume<=0）、缺失 T+2 数据",
        f"- 成本：佣金 {args.fee_bps}bp/边、印花税 {args.stamp_bps}bp 卖出、"
        f"滑点 {args.slip_bps}bp/边",
        "- 价格：前复权（data/adj_factor）；组合按信号日等权，逐日复利",
        "- 样本为当前在市 A 股，存在幸存者偏差；未建模涨跌停排队与冲击成本",
        "",
        f"## 组合总览（K={first_k}）",
        "",
        "| 模型 | K | 笔数 | 成交率 | 胜率 | 平均净收益 | 盈亏比 | 累计 | "
        "年化 | Sharpe | 最大回撤 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in models:
        for k in k_list:
            lines.append(_overview_row(name, k, results[name][k]))
    if bench is not None:
        lines += ["", f"同区间上证指数买入持有：**{_pct(bench, 1)}**（仅参照，"
                      f"策略仅持有隔夜，非同期暴露）。"]

    if pool_stats is not None:
        lines += ["", "## 对照：T 日涨停全池等权（同执行口径）", "",
                  "| 口径 | 笔数 | 成交率 | 胜率 | 平均净收益 | 累计 | 最大回撤 | T+1涨停占比 |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| 全池等权 | {pool_stats['n_trades']} | "
                  f"{_pct(pool_stats['fill_rate'], 1)} | "
                  f"{_pct(pool_stats['win_rate'], 1)} | "
                  f"{_pct(pool_stats['avg_trade_return'])} | "
                  f"{_pct(pool_stats['total_return'], 1)} | "
                  f"{_pct(pool_stats['max_drawdown'], 1)} | "
                  f"{_pct(pool_hit, 2)} |",
                  "",
                  "> 全池 = 信号日所有 T 日收盘涨停股（consecutive_boards>=1），不做模型排序；"
                  "执行与成本假设与上表完全相同。用于区分亏损来自模型排序还是入场时点。",
                  f"> 全池平均毛收益（未计成本）{_pct(pool_gross)}：负期望在成本之前已经存在。"]

    lines += ["", "## Top-K 敏感性", "", "| 模型 | K | 总收益 | 年化 | 最大回撤 | Sharpe |",
              "|---|---|---|---|---|---|"]
    for name in models:
        for k in k_list:
            s = results[name][k]
            lines.append(f"| {name} | {k} | {_pct(s['total_return'], 1)} | "
                         f"{_pct(s['annualized_return'], 1)} | "
                         f"{_pct(s['max_drawdown'], 1)} | {_num(s['sharpe'], 2)} |")

    lines += ["", "## 跳过原因（买不进/无数据）", "",
              "| 模型 | K | open_limit | suspended | no_exit | no_signal_bar | no_price |",
              "|---|---|---|---|---|---|---|"]
    for name in models:
        for k in k_list:
            sk = skips_all[name][k]
            lines.append(f"| {name} | {k} | {sk.get('open_limit', 0)} | "
                         f"{sk.get('suspended', 0)} | {sk.get('no_exit', 0)} | "
                         f"{sk.get('no_signal_bar', 0)} | {sk.get('no_price', 0)} |")

    lines += ["", "## 分年（K=5）", "",
              "| 模型 | 年份 | 笔数 | 胜率 | 平均净收益 | 盈亏比 |",
              "|---|---|---|---|---|---|"]
    for name in models:
        sub = trades_df[(trades_df["model"] == name) & (trades_df["k"] == first_k)]
        for year, g in sub.groupby("year", sort=True):
            s = _subset_stats(g.to_dict("records"), year)
            lines.append(f"| {name} | {year} | {s['n_trades']} | "
                         f"{_pct(s['win_rate'], 1)} | {_pct(s['avg_trade_return'])} | "
                         f"{_num(s['profit_factor'], 2)} |")

    lines += ["", "## 命中 / 未命中分解（K=5，按 T+1 收盘是否涨停）", "",
              "| 模型 | T+1涨停 | 笔数 | 胜率 | 平均净收益 | 盈亏比 |",
              "|---|---|---|---|---|---|"]
    for name in models:
        sub = trades_df[(trades_df["model"] == name) & (trades_df["k"] == first_k)]
        for lab, g in sub.groupby("label", sort=True):
            s = _subset_stats(g.to_dict("records"), str(lab))
            lines.append(f"| {name} | {'是' if lab == 1 else '否'} | {s['n_trades']} | "
                         f"{_pct(s['win_rate'], 1)} | {_pct(s['avg_trade_return'])} | "
                         f"{_num(s['profit_factor'], 2)} |")

    lines += ["", "## 连板高度分解（K=5，信号日 consecutive_boards）", "",
              "| 模型 | 连板 | 笔数 | 胜率 | 平均净收益 | 盈亏比 |",
              "|---|---|---|---|---|---|"]
    for name in models:
        sub = trades_df[(trades_df["model"] == name) & (trades_df["k"] == first_k)]
        for cb, g in sub.groupby("consecutive_bucket", sort=True):
            label = {0: "0", 1: "1", 2: "2+"}.get(int(cb), str(cb))
            s = _subset_stats(g.to_dict("records"), label)
            lines.append(f"| {name} | {label} | {s['n_trades']} | "
                         f"{_pct(s['win_rate'], 1)} | {_pct(s['avg_trade_return'])} | "
                         f"{_num(s['profit_factor'], 2)} |")

    if args.clean_end:
        lines += ["", f"## 数据完整期对照（信号日 <= {args.clean_end}）", "",
                  "| 模型 | 笔数 | 胜率 | 平均净收益 | 总收益 | 年化 | 最大回撤 |",
                  "|---|---|---|---|---|---|---|"]
        for name in models:
            sub = trades_df[(trades_df["model"] == name)
                            & (trades_df["k"] == first_k)
                            & (trades_df["signal_date"] <= args.clean_end)]
            if sub.empty:
                continue
            s = aggregate(sub.to_dict("records"), sub["signal_date"].nunique(), first_k)
            lines.append(f"| {name} | {s['n_trades']} | {_pct(s['win_rate'], 1)} | "
                         f"{_pct(s['avg_trade_return'])} | {_pct(s['total_return'], 1)} | "
                         f"{_pct(s['annualized_return'], 1)} | {_pct(s['max_drawdown'], 1)} |")
        lines += ["", "> 2026-09-16 起 daily.amount 曾为 0，快照中的成交额类特征退化；"
                      "该对照期完全避开了这段数据缺口。"]

    lines += ["", "## 结论摘要", ""]
    for name in models:
        s = results[name][first_k]
        lines.append(f"- **{name}**：K={first_k} 成交 {s['n_trades']} 笔，"
                     f"胜率 {_pct(s['win_rate'], 1)}，平均净收益 "
                     f"{_pct(s['avg_trade_return'])}，累计 {_pct(s['total_return'], 1)}，"
                     f"最大回撤 {_pct(s['max_drawdown'], 1)}")
    if pool_stats is not None:
        lines.append(f"- **全池基准**：成交 {pool_stats['n_trades']} 笔，平均净收益 "
                     f"{_pct(pool_stats['avg_trade_return'])}，平均毛收益 "
                     f"{_pct(pool_gross)}，T+1 涨停占比 {_pct(pool_hit, 2)}")
        worse = [n for n in models
                 if results[n][first_k]["avg_trade_return"] <= pool_stats["avg_trade_return"]]
        if len(worse) == len(models):
            lines.append("- 三个模型的平均净收益均未超过全池等权：模型提高的是 T+1 涨停"
                         "命中率，未转化为交易收益；负期望来自 T+1 开盘买入这一入场时点。")
        elif worse:
            lines.append(f"- 平均净收益低于全池等权的模型：{', '.join(worse)}。")
    gross = {n: float(trades_df[(trades_df["model"] == n)
                               & (trades_df["k"] == first_k)]["ret_gross"].mean())
             for n in models}
    gross_txt = "、".join(f"{n} {_pct(v)}" for n, v in gross.items())
    lines.append(f"- 未计成本的 K={first_k} 平均毛收益：{gross_txt}；负期望不是交易成本造成的。")
    report = "\n".join(lines) + "\n"

    if not args.no_write:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        tp = Path(args.trades_out)
        tp.parent.mkdir(parents=True, exist_ok=True)
        trades_df.to_csv(tp, index=False, encoding="utf-8-sig")
        print(f"报告：{out}\n明细：{tp}")
    print(f"交易 {len(trades_df)} 笔；基准 {_pct(bench) if bench is not None else '-'}")
    for name in models:
        s = results[name][first_k]
        print(f"  {name}: K={first_k} 笔数={s['n_trades']} 胜率={_pct(s['win_rate'], 1)} "
              f"均净={_pct(s['avg_trade_return'])} 累计={_pct(s['total_return'], 1)} "
              f"回撤={_pct(s['max_drawdown'], 1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
