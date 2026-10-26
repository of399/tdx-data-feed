"""P0-2 横盘蓄势 × daban 联合评估（2026-10-22）。

问题：给 daban 模型加「横盘蓄势」前置过滤，能否提高每交易日 top-k 的命中率？

口径（与训练/实盘一致）：
  - OOS: date >= 2025-06-01（模型训练切分点），需 T+1 标签可得
  - 可交易: 剔除 T 日开盘即涨停（is_open_limit_up，实盘买不进）
  - 评分: 现役 daban 模型 29 维特征（compute_window_features_extended）
  - 标签: close[t+1]/close[t] - 1 >= limit（次日收盘涨停，含一字板）
  - 附加 label_buyable: 次日非开盘涨停（开盘可买入的涨停）

横盘定义（先验固定，不按结果调参）：
  宽: 无涨停 + |ret_10d|<=5% + |ret_20d|<=8% + vol_10d<=3%
      + close_pos_20d∈[0.25,0.75] + range_20d<=18% + max_dd_20d>=-8%
  严: 无涨停 + |ret_10d|<=3% + |ret_20d|<=5% + vol_10d<=2%
      + close_pos_20d∈[0.35,0.65] + range_20d<=12% + max_dd_20d>=-5%

验收线：横盘子集 top-k 命中率 >= 全市场 top-k 的 1.2 倍。

明细落盘含全部 29 维模型特征 → 之后换模型可 --eval-only 直接重打分，无需重扫。

用法：
    venv/bin/python scripts/eval_daban_pattern.py --limit 30 --workers 4   # 冒烟
    venv/bin/python scripts/eval_daban_pattern.py --workers 18             # 全市场 OOS
    venv/bin/python scripts/eval_daban_pattern.py --eval-only --models "live=...,new=..."  # 复用明细
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tdxfeed"))
sys.path.insert(0, str(ROOT / "scripts"))

from features_5d import (  # noqa: E402
    compute_window_features_extended,
    get_limit,
    is_limit_up_ret,
    is_open_limit_up,
)
from train_daban_model import SPLIT_DATE, iter_stock_files  # noqa: E402

MODEL_DIR = ROOT / "data" / "double_up"          # 模型仍读生产目录（只读）
OUT_DIR = ROOT / "data" / "daban"                 # 产物写本线专属目录（不再污染 data/double_up）
DEFAULT_MODELS = f"live={MODEL_DIR / 'd_signal_5d_model_daban.txt'}"


def process_stock(item: tuple) -> pd.DataFrame | None:
    code, fp, start_date, feat_cols = item
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    if len(df) < 40:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    dates = df["date"].astype(str).str.slice(0, 10).to_numpy()
    close = df["close"].to_numpy(dtype=float)
    openp = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    amount = df["amount"].to_numpy(dtype=float)
    n = len(df)

    start_idx = int(np.searchsorted(dates, start_date))
    t0 = max(start_idx, 20)
    if t0 >= n - 1:
        return None

    limit_val = get_limit(code)
    rows: list[dict] = []

    for t in range(t0, n - 1):
        c = close[t]
        if c <= 0 or close[t - 1] <= 0 or close[t + 1] <= 0:
            continue
        if is_open_limit_up(openp[t], close[t - 1], limit_val):
            continue  # T 日买不进，剔除

        label = int(is_limit_up_ret(close[t + 1] / c - 1.0, limit_val))
        label_buyable = int(label and not is_open_limit_up(openp[t + 1], c, limit_val))

        feat = compute_window_features_extended(df, code, sig_idx=t)
        if feat is None:
            continue

        cw = close[t - 19: t + 1]
        hw = high[t - 19: t + 1]
        lw = low[t - 19: t + 1]
        aw = amount[t - 19: t + 1]
        pct = np.diff(cw) / cw[:-1]
        hi, lo = float(hw.max()), float(lw.min())
        prev_c = cw[:-1]

        row = {
            "date": dates[t],
            "code": code,
            "label": label,
            "label_buyable": label_buyable,
            "ret_10d": c / close[t - 10] - 1.0,
            "ret_20d": c / close[t - 20] - 1.0,
            "vol_10d": float(np.std(np.diff(close[t - 10: t + 1]) / close[t - 10: t])),
            "vol_20d": float(np.std(pct)),
            "close_pos_20d": (c - lo) / (hi - lo) if hi > lo else 0.5,
            "range_20d": hi / lo - 1.0 if lo > 0 else 0.0,
            "max_dd_20d": float(pct.min()),
            "ma20_dist": c / float(np.mean(cw)) - 1.0,
            "amount_ratio_20": amount[t] / float(np.mean(aw)) if np.mean(aw) > 0 else 0.0,
            "amp_20": float(np.mean((hw[1:] - lw[1:]) / prev_c)),
            "up_days_20": int((pct > 0).sum()),
            "amount": amount[t],
        }
        row.update({k: feat.get(k, 0.0) for k in feat_cols})
        rows.append(row)

    if not rows:
        return None
    return pd.DataFrame(rows)


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


def subset_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    nolimit = df["is_limit_up_today"] == 0
    loose = (
        nolimit
        & (df["ret_10d"].abs() <= 0.05)
        & (df["ret_20d"].abs() <= 0.08)
        & (df["vol_10d"] <= 0.03)
        & df["close_pos_20d"].between(0.25, 0.75)
        & (df["range_20d"] <= 0.18)
        & (df["max_dd_20d"] >= -0.08)
    )
    strict = (
        nolimit
        & (df["ret_10d"].abs() <= 0.03)
        & (df["ret_20d"].abs() <= 0.05)
        & (df["vol_10d"] <= 0.02)
        & df["close_pos_20d"].between(0.35, 0.65)
        & (df["range_20d"] <= 0.12)
        & (df["max_dd_20d"] >= -0.05)
    )
    return {
        "全市场": pd.Series(True, index=df.index),
        "无涨停(对照)": nolimit,
        "横盘(宽)": loose,
        "横盘(严)": strict,
    }


def evaluate(df: pd.DataFrame, k_list: tuple[int, ...], label_col: str,
             score_col: str) -> None:
    masks = subset_masks(df)
    y = df[label_col].to_numpy()
    s = df[score_col].to_numpy()
    print(f"\nOOS 样本 {len(df):,} · 交易日 {df['date'].nunique()} · 正样本率 "
          f"{df[label_col].mean() * 100:.2f}% · 全市场 AUC {_auc(y, s):.4f}")
    print(f"标签口径: {label_col} · 评分: {score_col}")

    results: dict[str, dict[int, float]] = {}
    for name, mask in masks.items():
        sub = df[mask]
        if sub.empty:
            continue
        sub = sub.sort_values(["date", score_col], ascending=[True, False], kind="mergesort")
        sub = sub.assign(_rank=sub.groupby("date", sort=False).cumcount())
        sizes = sub.groupby("date", sort=False).size()
        auc = _auc(sub[label_col].to_numpy(), sub[score_col].to_numpy())
        base = float(sub[label_col].mean())
        print(f"{'=' * 72}")
        print(f"{name}: n={len(sub):,} · 日均候选 {sizes.mean():.0f} "
              f"(最少 {sizes.min()}) · 基准命中率 {base * 100:.2f}% · AUC {auc:.4f}")
        results[name] = {}
        for k in k_list:
            top = sub[sub["_rank"] < k]
            prec = float(top[label_col].mean())
            day_prec = float(top.groupby("date", sort=False)[label_col].mean().mean())
            full_days = int((sizes >= k).sum())
            results[name][k] = day_prec
            print(f"  top{k:<3} 日均命中 {day_prec * 100:6.2f}% "
                  f"(池化 {prec * 100:6.2f}%) · 覆盖 {full_days}/{len(sizes)} 天 "
                  f"· lift {prec / base if base > 0 else float('nan'):.2f}x")

    print(f"\n{'=' * 72}")
    print("对照：横盘子集 top-k / 全市场 top-k（日均命中率）")
    all_hits = results.get("全市场", {})
    for name in ("无涨停(对照)", "横盘(宽)", "横盘(严)"):
        if name not in results:
            continue
        parts = []
        for k in k_list:
            a, b = results[name][k], all_hits[k]
            parts.append(f"top{k} {b * 100:.2f}%→{a * 100:.2f}% ({a / b if b else float('nan'):.2f}x)")
        print(f"  {name:<10} " + " · ".join(parts))
    print("  验收线: 横盘 top-k >= 1.2x 全市场")


def parse_models(spec: str) -> dict[str, Path]:
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, path = part.split("=", 1)
        else:
            name, path = Path(part).stem, part
        out[name.strip()] = Path(path.strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=SPLIT_DATE, help="OOS 起始日")
    ap.add_argument("--limit", type=int, default=0, help="股票数(0=全量，冒烟用)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--models", default=DEFAULT_MODELS,
                    help="逗号分隔 name=path；缺失文件自动跳过")
    ap.add_argument("--out", default=str(OUT_DIR / "daban_pattern_eval_20261022.parquet"))
    ap.add_argument("--eval-only", action="store_true", help="跳过扫描，直接读 --out 评估")
    ap.add_argument("--topk", default="10,20,50")
    ap.add_argument("--score-col", default="", help="P0-2 主评估用的 score 列(默认第一个模型)")
    args = ap.parse_args()
    k_list = tuple(int(x) for x in args.topk.split(","))

    models = {n: p for n, p in parse_models(args.models).items() if p.exists()}
    missing = [n for n, p in parse_models(args.models).items() if not p.exists()]
    for n in missing:
        print(f"[WARN] 模型不存在，跳过: {n}")
    if not models:
        print("无可用模型", file=sys.stderr)
        return 1
    feat_cols = lgb.Booster(model_file=str(next(iter(models.values())))).feature_name()

    out_fp = Path(args.out)
    if args.eval_only:
        df = pd.read_parquet(out_fp)
    else:
        files = iter_stock_files(args.limit)
        tasks = [(c, f, args.start, feat_cols) for c, f in files]
        print(f"扫描 {len(files)} 只股票 · start={args.start} · workers={args.workers}", flush=True)
        chunks = []
        n_rows = n_pos = 0
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for i, r in enumerate(ex.map(process_stock, tasks, chunksize=8), 1):
                if r is None:
                    continue
                chunks.append(r)
                n_rows += len(r)
                n_pos += int(r["label"].sum())
                if i % 500 == 0:
                    print(f"  {i}/{len(files)}  样本 {n_rows:,}  正 {n_pos:,}", flush=True)
        if not chunks:
            print("无样本", file=sys.stderr)
            return 1
        df = pd.concat(chunks, ignore_index=True)
        del chunks
        for c in df.columns:
            if df[c].dtype == float:
                df[c] = df[c].astype("float32")
        out_fp.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_fp, index=False)
        print(f"\n明细落盘: {out_fp} ({len(df):,} 行)")

    score_cols = {}
    for name, path in models.items():
        m = lgb.Booster(model_file=str(path))
        need = m.feature_name()
        lacking = [c for c in need if c not in df.columns]
        if lacking:
            print(f"[WARN] {name} 缺特征 {lacking}，跳过")
            continue
        col = f"score_{name}"
        df[col] = m.predict(df[need])
        score_cols[name] = col

    print(f"\n{'=' * 72}")
    print("【模型 OOS 对比（同一可交易样本）】")
    print(f"{'模型':<16}{'AUC(全市场)':>12}{'AUC(横盘宽)':>12}{'top10':>9}{'top20':>9}")
    masks = subset_masks(df)
    sub_loose = df[masks["横盘(宽)"]]
    for name, col in score_cols.items():
        top_all = (df.sort_values(["date", col], ascending=[True, False], kind="mergesort")
                   .assign(_r=lambda x: x.groupby("date", sort=False).cumcount()))
        top10 = top_all[top_all["_r"] < 10]
        top_all = top_all[top_all["_r"] < 20]
        print(f"{name:<16}{_auc(df['label'].to_numpy(), df[col].to_numpy()):>12.4f}"
              f"{_auc(sub_loose['label'].to_numpy(), sub_loose[col].to_numpy()):>12.4f}"
              f"{top10['label'].mean() * 100:>8.2f}%{top_all['label'].mean() * 100:>8.2f}%")

    primary = args.score_col or next(iter(score_cols.values()))
    if primary not in score_cols.values():
        print(f"[WARN] --score-col {primary} 不在已打分列中，回退 {next(iter(score_cols.values()))}")
        primary = next(iter(score_cols.values()))
    evaluate(df, k_list, "label", primary)

    print("\n--- 稳健性: 次日开盘可买入口径 (label_buyable) ---")
    evaluate(df, k_list, "label_buyable", primary)

    print("\n--- 分年 (label, top20) ---")
    df["_year"] = df["date"].str.slice(0, 4)
    for y, sub in df.groupby("_year"):
        masks = subset_masks(sub)
        line = [f"{y}: 样本 {len(sub):,} 基准 {sub['label'].mean() * 100:.2f}%"]
        for name in ("全市场", "横盘(宽)"):
            s = sub[masks[name]].sort_values(["date", primary], ascending=[True, False],
                                             kind="mergesort")
            s = s.assign(_rank=s.groupby("date", sort=False).cumcount())
            top = s[s["_rank"] < 20]
            line.append(f"{name} top20 {top.groupby('date')['label'].mean().mean() * 100:.2f}%")
        print("  " + " · ".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
