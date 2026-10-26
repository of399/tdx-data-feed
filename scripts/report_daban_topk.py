#!/usr/bin/env python
"""daban Top-K precision / lift 报告（基于 P0 全市场评估快照）。

口径：
  - 样本来自 data/double_up/daban_pattern_eval_*.parquet（已剔除 T 日开盘即涨停），
    默认主口径 label（T+1 收盘涨停，与训练目标一致）；
    --label buyable 使用 label_buyable（T+1 收盘涨停且开盘可买），报告中同时给出对照。
  - Top-K 为逐日截面排序取前 K；precision 为逐日命中率等权平均，
    lift = 汇总 precision / 全样本基准率。
  - 同时输出分年度稳定性、Top-K 连板构成与板块构成。

用法：
    venv/bin/python scripts/report_daban_topk.py
    venv/bin/python scripts/report_daban_topk.py --k 5,10,20
    venv/bin/python scripts/report_daban_topk.py \\
        --model live=data/double_up/d_signal_5d_model_daban.txt \\
        --model excl=data/double_up/d_signal_5d_model_daban_retrain_20261022_excl.txt

快照重建（缺失时）：
    venv/bin/python scripts/eval_daban_pattern.py --eval-only
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data/double_up/daban_pattern_eval_20261022.parquet"
DEFAULT_MODEL = ROOT / "data/double_up/d_signal_5d_model_daban.txt"
LABEL_COLS = {"buyable": "label_buyable", "raw": "label"}
META_COLS = ["date", "label", "label_buyable", "is_gem",
             "consecutive_boards", "is_limit_up_today", "is_new"]


def _auc(y: np.ndarray, s: np.ndarray) -> float:
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    sorted_s = s[order]
    ranks = np.empty(len(s), dtype=float)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i: j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _topk_stats(df: pd.DataFrame, k: int, label_col: str) -> dict:
    ordered = df.sort_values(["date", "score"], ascending=[True, False],
                             kind="mergesort")
    pos = ordered.groupby("date", sort=False).cumcount()
    picks = ordered[pos.to_numpy() < k]
    n_days = int(df["date"].nunique())
    n_picks = len(picks)
    hits = int(picks[label_col].sum())
    base = float(df[label_col].mean())
    day = picks.groupby("date")[label_col].agg(["sum", "size"])
    day_precision = float((day["sum"] / np.minimum(day["size"], k)).mean())
    pooled = hits / n_picks if n_picks else float("nan")
    return {
        "days": n_days,
        "n": len(df),
        "base_rate": base,
        "auc": _auc(df[label_col].to_numpy(), df["score"].to_numpy()),
        "k": k,
        "precision_day": day_precision,
        "precision_pooled": pooled,
        "lift": pooled / base if base > 0 else float("nan"),
        "hit_days": float((day["sum"] > 0).mean()) if n_days else float("nan"),
        "hits_per_day": hits / n_days if n_days else float("nan"),
        "consec_share": float((picks["consecutive_boards"] >= 1).mean())
        if n_picks else float("nan"),
        "mean_consec": float(picks["consecutive_boards"].mean())
        if n_picks else float("nan"),
        "limit_today_share": float(picks["is_limit_up_today"].mean())
        if n_picks else float("nan"),
        "main_share": float((picks["is_gem"] == 0).mean())
        if n_picks else float("nan"),
    }


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


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="daban Top-K precision / lift 报告")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="P0 评估快照 parquet")
    ap.add_argument("--model", action="append", default=[],
                    help="name=path，可重复；默认 live=现役 daban 模型")
    ap.add_argument("--k", default="5,10", help="Top-K 列表，如 5,10,20")
    ap.add_argument("--label", choices=sorted(LABEL_COLS), default="raw",
                    help="主口径标签；raw=训练目标（T+1 收盘涨停），buyable=次日开盘可买")
    ap.add_argument("--out", default="", help="markdown 输出路径；默认 docs/DABAN_TOPK_REPORT_<date>.md")
    ap.add_argument("--no-write", action="store_true", help="只打印摘要，不落盘")
    args = ap.parse_args()

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = ROOT / data_path
    if not data_path.exists():
        raise SystemExit(
            f"评估快照不存在：{data_path}\n"
            "先运行：venv/bin/python scripts/eval_daban_pattern.py --eval-only")

    k_list = [int(x) for x in args.k.split(",") if x.strip()]
    if not k_list or min(k_list) < 1:
        raise SystemExit("--k 必须是正整数列表")
    k_list = sorted(set(k_list))
    label_col = LABEL_COLS[args.label]
    models = {n: lgb.Booster(model_file=str(p)) for n, p in _parse_models(args.model).items()}
    feature_cols = sorted({c for m in models.values() for c in m.feature_name()})
    cols = list(dict.fromkeys(META_COLS + feature_cols))
    df = pd.read_parquet(data_path, columns=cols)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df = df.dropna(subset=["date", label_col])
    df["_year"] = df["date"].str.slice(0, 4)

    for name, model in models.items():
        need = [c for c in model.feature_name() if c not in df.columns]
        if need:
            raise SystemExit(f"模型 {name} 缺少特征列：{need}")
        df[f"score_{name}"] = model.predict(df[model.feature_name()].fillna(0))

    base_all = float(df[label_col].mean())
    lines = [
        f"# daban Top-K Precision / Lift 报告（{datetime.now():%Y-%m-%d}）",
        "",
        "## 口径",
        "",
        f"- 数据快照：`{data_path.relative_to(ROOT)}`",
        f"- 样本：{len(df):,} 行 / {df['date'].nunique()} 个交易日 / "
        f"{df['date'].min()} ~ {df['date'].max()}",
        f"- 标签：`{label_col}`（T+1 收盘涨停且 T+1 开盘可买）"
        if args.label == "buyable" else f"- 标签：`{label_col}`（T+1 收盘涨停）",
        f"- 基准率：{_pct(base_all)}",
        "- Top-K：逐日截面按模型分排序取前 K；precision 为逐日命中率等权平均，"
        "lift = 汇总 precision / 基准率",
        "- 样本已剔除 T 日开盘即涨停（实盘买不进）",
        "- OOS：三个模型的训练切分点均为 2025-06-01，快照起点 2025-06-03，"
        "全部为样本外；训练集负样本按 1:5 抽样，此处为全市场逐日截面",
        "",
    ]

    overview_header = "| 模型 | AUC | " + " | ".join(
        f"Top{k} | Top{k} lift" for k in k_list) + " |"
    overview_sep = "|---" * (2 + 2 * len(k_list)) + "|"
    overview = [overview_header, overview_sep]
    detail = ["| 模型 | K | 逐日 precision | 汇总 precision | lift | "
              "有命中交易日 | 日均命中数 |",
              "|---|---|---|---|---|---|---|"]

    overall: dict[str, dict[int, dict]] = {}
    for name in models:
        sub = df.rename(columns={f"score_{name}": "score"})
        stats = {k: _topk_stats(sub, k, label_col) for k in k_list}
        overall[name] = stats
        first = stats[k_list[0]]
        cells = [f"{name}", f"{first['auc']:.4f}"]
        for k in k_list:
            s = stats[k]
            cells += [_pct(s["precision_day"]), f"{s['lift']:.2f}x"]
        overview.append("| " + " | ".join(cells) + " |")
        for k in k_list:
            s = stats[k]
            detail.append(
                f"| {name} | {k} | {_pct(s['precision_day'])} | "
                f"{_pct(s['precision_pooled'])} | {s['lift']:.2f}x | "
                f"{_pct(s['hit_days'])} | {s['hits_per_day']:.2f} |")

    lines += ["## 模型总览", "", *overview, "", "## 分 K 明细", "", *detail, ""]

    alt_col = "label_buyable" if label_col == "label" else "label"
    alt_name = "次日开盘可买" if alt_col == "label_buyable" else "T+1 收盘涨停"
    alt_base = float(df[alt_col].mean())
    lines += [f"## 标签口径对照（{alt_name}：`{alt_col}`）", "",
              f"基准率 {_pct(alt_base)}；用于观察剔除次日一字板后的选股能力衰减。", "",
              "| 模型 | AUC | " + " | ".join(f"Top{k} | Top{k} lift" for k in k_list) + " |",
              "|---" * (2 + 2 * len(k_list)) + "|"]
    for name in models:
        sub = df.rename(columns={f"score_{name}": "score"})
        alt_stats = {k: _topk_stats(sub, k, alt_col) for k in k_list}
        cells = [name, f"{alt_stats[k_list[0]]['auc']:.4f}"]
        for k in k_list:
            s = alt_stats[k]
            cells += [_pct(s["precision_day"]), f"{s['lift']:.2f}x"]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## 分年度稳定性", "",
              "| 模型 | 年份 | 交易日 | 基准率 | "
              + " | ".join(f"Top{k} | Top{k} lift" for k in k_list) + " |",
              "|---" * (5 + 2 * len(k_list)) + "|"]
    for name in models:
        for year, sub in df.groupby("_year", sort=True):
            sub = sub.rename(columns={f"score_{name}": "score"})
            cells = [name, str(year), str(sub["date"].nunique()),
                     _pct(float(sub[label_col].mean()))]
            for k in k_list:
                s = _topk_stats(sub, k, label_col)
                cells += [_pct(s["precision_day"]), f"{s['lift']:.2f}x"]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    first_k = k_list[0]
    lines += [f"## Top{first_k} 构成", "",
              "| 模型 | 连板(≥1)占比 | 平均连板数 | 当日已涨停占比 | "
              "主板占比 | 创业板/科创占比 |",
              "|---|---|---|---|---|---|"]
    for name in models:
        s = overall[name][first_k]
        lines.append(
            f"| {name} | {_pct(s['consec_share'])} | {s['mean_consec']:.2f} | "
            f"{_pct(s['limit_today_share'])} | {_pct(s['main_share'])} | "
            f"{_pct(1 - s['main_share'])} |")
    lines.append("")

    lines += ["## 基准率参考", "",
              "| 连板数 | 样本数 | 基准率 |", "|---|---|---|"]
    grp = (df.assign(_consec=df["consecutive_boards"].clip(upper=5))
           .groupby("_consec")[label_col].agg(["size", "mean"]))
    for consec, row in grp.iterrows():
        label = "5+" if consec >= 5 else str(int(consec))
        lines.append(f"| {label} | {int(row['size']):,} | {_pct(row['mean'])} |")
    lines.append("")
    board = df.groupby("is_gem")[label_col].agg(["size", "mean"])
    lines += ["| 板块 | 样本数 | 基准率 |", "|---|---|---|",
              f"| 主板 | {int(board.loc[0, 'size']):,} | {_pct(board.loc[0, 'mean'])} |",
              f"| 创业板/科创 | {int(board.loc[1, 'size']):,} | {_pct(board.loc[1, 'mean'])} |",
              ""]

    lines += ["## 结论摘要", ""]
    for name in models:
        s = overall[name][first_k]
        lines.append(
            f"- **{name}**：Top{first_k} 命中 {_pct(s['precision_day'])}，"
            f"lift {s['lift']:.1f}x；Top{first_k} 中连板股占比 "
            f"{_pct(s['consec_share'])}（均值 {s['mean_consec']:.2f} 板）")
    report = "\n".join(lines) + "\n"

    out_path = Path(args.out) if args.out else (
        ROOT / "docs" / f"DABAN_TOPK_REPORT_{datetime.now():%Y%m%d}.md")
    if not args.no_write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"报告已写入：{out_path}")
    print(f"样本 {len(df):,} 行 / {df['date'].nunique()} 日，基准率 {_pct(base_all)}")
    for name in models:
        s = overall[name][first_k]
        print(f"  {name}: AUC={s['auc']:.4f}  Top{first_k}={_pct(s['precision_day'])}  "
              f"lift={s['lift']:.2f}x  连板占比={_pct(s['consec_share'])}")
    if args.no_write:
        print("\n" + report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
