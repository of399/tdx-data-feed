#!/usr/bin/env python
"""阶段 1：标签口径可诊断性（分钟级探路，为阶段 2 省掉整轮训练）。

**要回答的问题**：把训练目标从「T+1 收盘涨停」换成「T+1 开盘溢价」，
在**同一评估集、同一评估标签**下，排序能力是否真的更强？
若不更强 → 阶段 2 不必做（省掉改码 + 2×17min 训练 + 评估）。

**三个对照模型**（同一套 29 维特征、同一套 LightGBM 超参）：

| # | 模型 | 训练标签 | 训练样本空间 |
|---|---|---|---|
| 0 | **现役** `d_signal_5d_model_daban.txt` | T+1 收盘涨停 | 全市场日频 |
| 1 | diag-A | T+1 收盘涨停 | T 日封板样本 |
| 2 | diag-B | T+1 开盘价 > T 日收盘价 | T 日封板样本 |

**统一评估口径**（三者完全一致，故可比）：
- 评估集 = 阶段 0 过滤后的封板样本 ∩ test 集（`SPLIT_DATE=2025-06-01` 之后）
- 统一评估标签 = T+1 开盘净收益 > 0（打板真实收益口径，扣双边成本 0.30%）
- 指标 = OOS AUC + Top5 命中率 + Top5 平均净收益

注：diag-B 的训练目标与评估标签同源，但**训练样本空间是全封板样本、评估集是过滤后子集**，
故不存在纯自证；且评估用的是「扣成本后的净收益 > 0」而非「高开 > 0」。

用法::

    venv/bin/python scripts/daban_label_diagnose.py                # 默认 400 股 × 400 日
    venv/bin/python scripts/daban_label_diagnose.py --limit 50 --recent 200   # 冒烟
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "scripts"), str(_ROOT / "tdxfeed")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _common import compute_window_features_extended  # noqa: E402
from features_5d import get_limit, is_open_limit_up  # noqa: E402

from tdxfeed.daban.filter import SectorFilter, load_sector_features  # noqa: E402
from tdxfeed.daban.limit_price import limit_up_price  # noqa: E402
from tdxfeed.daban.sector import load_industry_map  # noqa: E402

SPLIT_DATE = "2025-06-01"
RANDOM_SEED = 42
COST = 0.0030
NEG_RATIO = 5
LIVE_MODEL = _ROOT / "data" / "double_up" / "d_signal_5d_model_daban.txt"
PARQUET = _ROOT / "data" / "parquet" / "daily"

LGB_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 20,
    "seed": RANDOM_SEED,
    "verbosity": -1,
}


_LOOKUP = None
_IND_MAP = None


def _get_lookup():
    """板块特征查表（进程内缓存一次）。"""
    global _LOOKUP
    if _LOOKUP is None:
        from tdxfeed.daban.sector_features import build_lookup

        _LOOKUP = build_lookup()
    return _LOOKUP


def _get_ind_map():
    global _IND_MAP
    if _IND_MAP is None:
        from tdxfeed.daban.sector import load_industry_map

        _IND_MAP = load_industry_map()
    return _IND_MAP


def build_samples(code: str, recent: int, sample_space: str = "limit_up") -> pd.DataFrame | None:
    """构造样本（含两套训练标签 + 统一评估标签）。code 为 6 位。

    sample_space:
        limit_up —— 只保留 T 日封板的样本（打板可交易集）
        all      —— 保留全部交易日（与现役同口径），负样本按 NEG_RATIO 抽样
    """
    from tdxfeed.daban.limit_price import exchange_prefix

    fp = PARQUET / f"{exchange_prefix(code)}{code}.parquet"
    if not fp.exists():
        return None
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    if df is None or len(df) < 30:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    df = df.tail(recent + 30).reset_index(drop=True)
    limit_val = get_limit(code)
    close = df["close"].to_numpy()
    openp = df["open"].to_numpy()
    rows = []
    for i in range(4, len(df) - 1):
        prev_c = float(close[i - 1])
        cur_c = float(close[i])
        if prev_c <= 0 or cur_c <= 0:
            continue
        if sample_space == "limit_up":
            # T 日必须真封板（精确涨停价口径）
            lp = limit_up_price(prev_c, limit_ratio_exact(code))
            if cur_c < lp - 0.005:
                continue
            # T 日开盘即涨停 → 实盘买不进，剔除
            if is_open_limit_up(float(openp[i]), prev_c, limit_val):
                continue
        next_close = float(close[i + 1])
        next_open = float(openp[i + 1])
        if next_close <= 0 or next_open <= 0:
            continue
        label_a = 1 if next_close / cur_c - 1.0 >= limit_val - 0.001 else 0
        label_b = 1 if next_open > cur_c else 0
        eval_net = next_open / cur_c - 1.0 - COST
        feat = compute_window_features_extended(df, code, sig_idx=i)
        if feat is None:
            continue
        # 阶段3 特征（与训练侧同源：lag=1）
        lookup = _get_lookup()
        if lookup is not None:
            from tdxfeed.daban.sector_features import EXTRA_FEATURE_COLS

            sig_date = pd.Timestamp(df["date"].iloc[i]).strftime("%Y%m%d")
            ind = _get_ind_map().get(code, "")
            row = lookup.get(ind, sig_date, lag=1)
            feat.update(
                {c: float(row.get(c, 0.0)) for c in EXTRA_FEATURE_COLS}
                if row
                else {c: 0.0 for c in EXTRA_FEATURE_COLS}
            )
        rows.append(
            {
                "code": code,
                "signal_date": pd.Timestamp(df["date"].iloc[i]).strftime("%Y%m%d"),
                "label_a": label_a,
                "label_b": label_b,
                "eval_net": eval_net,
                "eval_label": 1 if eval_net > 0 else 0,
                **feat,
            }
        )
    if not rows:
        return None
    out = pd.DataFrame(rows)
    if sample_space == "all":
        # 与现役一致：正样本全取，负样本按 NEG_RATIO 抽样（同时大幅降低特征计算量）
        rng = np.random.default_rng(RANDOM_SEED)
        pos = out[out["label_b"] == 1]
        neg = out[out["label_b"] == 0]
        n_pick = max(1, len(pos) * NEG_RATIO)
        if len(neg) > n_pick:
            neg = neg.iloc[rng.choice(len(neg), n_pick, replace=False)]
        out = pd.concat([pos, neg], ignore_index=True)
    return out


def limit_ratio_exact(code: str) -> float:
    from tdxfeed.daban.limit_price import limit_ratio

    return float(limit_ratio(code, ""))


def _work(code: str, recent: int, sample_space: str) -> pd.DataFrame | None:
    return build_samples(code, recent, sample_space)


def topk_metrics(df: pd.DataFrame, score_col: str, k: int = 5) -> dict:
    """Top-k 命中率与平均净收益（按 date 分组取 top-k）。"""
    hit, rets, n_dates = [], [], 0
    for _, g in df.groupby("signal_date"):
        n_dates += 1
        top = g.nlargest(k, score_col)
        hit.append(float(top["eval_label"].mean()))
        rets.append(float(top["eval_net"].mean()))
    return {
        f"top{k}_命中率%": round(float(np.mean(hit)) * 100, 2) if hit else 0.0,
        f"top{k}_均净收益%": round(float(np.mean(rets)) * 100, 3) if rets else 0.0,
        "评估日数": n_dates,
    }


def auc(y: np.ndarray, s: np.ndarray) -> float:
    y = np.asarray(y)
    s = np.asarray(s)
    if len(np.unique(y)) < 2:
        return float("nan")
    order = np.argsort(s)
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # 处理并列
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.zeros(len(cnt))
    np.add.at(sums, inv, ranks)
    ranks = (sums / cnt)[inv]
    n_pos = float((y == 1).sum())
    n_neg = float((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段1 标签口径可诊断性")
    ap.add_argument("--limit", type=int, default=400, help="抽样股票数")
    ap.add_argument("--recent", type=int, default=400, help="每只股票最近交易日数")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument(
        "--sample-space",
        choices=["limit_up", "all"],
        default="limit_up",
        help="训练样本空间：limit_up=只取T日封板样本；all=全部交易日(现役口径,负样本1:5)",
    )
    ap.add_argument("--no-filter", action="store_true", help="评估集不做阶段0 过滤")
    ap.add_argument(
        "--min-eval-n", type=int, default=500, help="过滤后最少评估样本数（不足则放宽）"
    )
    ap.add_argument(
        "--model",
        default="",
        help="额外评估的模型文件（逗号分隔），与现役同口径评估",
    )
    ap.add_argument(
        "--eval-limit",
        type=int,
        default=1500,
        help="评估池股票数（独立于 --limit，固定 seed → 跨训练规模可比；0=与训练同池）",
    )
    args = ap.parse_args()

    t0 = time.time()
    all_codes = sorted(
        p.stem.lower().replace("sh", "").replace("sz", "").replace("bj", "")
        for p in PARQUET.glob("*.parquet")
        if ".bak" not in p.stem
    )
    all_codes = [c for c in all_codes if c.isdigit() and len(c) == 6]

    # 训练池：seed 42 抽样（--limit 控制规模）
    rng = np.random.default_rng(RANDOM_SEED)
    train_codes = all_codes
    if args.limit and args.limit < len(all_codes):
        idx = rng.choice(len(all_codes), args.limit, replace=False)
        train_codes = [all_codes[i] for i in idx]

    # 评估池：独立 seed 43 抽样，固定不变 → 跨训练规模可比
    eval_codes = train_codes
    if args.eval_limit and args.eval_limit != args.limit:
        rng_e = np.random.default_rng(RANDOM_SEED + 1)
        idx_e = rng_e.choice(len(all_codes), args.eval_limit, replace=False)
        eval_codes = [all_codes[i] for i in idx_e]
    print(
        f"[info] 训练池 {len(train_codes)} 只 × 最近 {args.recent} 日 | "
        f"评估池 {len(eval_codes)} 只（固定）"
    )

    def _collect(codes: list[str]) -> pd.DataFrame:
        frames = []
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for df in pool.map(
                _work, codes, [args.recent] * len(codes), [args.sample_space] * len(codes)
            ):
                if df is not None and not df.empty:
                    frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    data = _collect(train_codes)
    eval_data = data if eval_codes is train_codes else _collect(eval_codes)
    if data.empty or eval_data.empty:
        print("[fail] 无样本")
        return 1
    print(
        f"[info] 封板样本 {len(data):,} 条 / {data['signal_date'].nunique()} 个交易日"
        f"（{time.time() - t0:.0f}s）"
    )

    data["signal_date"] = data["signal_date"].astype(str)
    eval_data["signal_date"] = eval_data["signal_date"].astype(str)

    # 切分：训练侧用全封板样本（实盘口径：模型在历史全样本上训练），
    # 评估侧应用阶段 0 过滤（实盘口径：盘中过滤后才交易）。
    # ⚠ 不能在切分前过滤：过滤依赖 T 日盘中板块信息，训练时无法复现，
    #   且会让训练样本骤减导致欠拟合。
    train = data[data["signal_date"] < SPLIT_DATE]
    test = eval_data[eval_data["signal_date"] >= SPLIT_DATE]
    print(f"[info] 切分 {SPLIT_DATE}：train {len(train):,} / test {len(test):,}（固定评估池）")

    filtered = False
    if not args.no_filter:
        flt = SectorFilter.load()
        if flt.enabled:
            ind_map = load_industry_map()
            test = test.assign(industry_code=test["code"].map(ind_map))
            kept = flt.apply(test, load_sector_features())
            if len(kept) >= args.min_eval_n:
                test = kept
                filtered = True
                print(f"[filter] 评估集应用阶段0 口径 → {len(test):,} 条")
            else:
                print(
                    f"[warn] 阶段0 过滤后仅 {len(kept)} 条 < {args.min_eval_n}，"
                    f"回退未过滤评估集（结论需谨慎）"
                )
    if len(test) < 100:
        print("[warn] test 样本过少，建议放宽 --limit/--recent 或 --no-filter")
        return 1

    feat_cols = [
        c
        for c in data.columns
        if c
        not in (
            "code",
            "signal_date",
            "label_a",
            "label_b",
            "eval_net",
            "eval_label",
            "industry_code",
        )
    ]
    y_eval = test["eval_label"].to_numpy()
    results = []

    # --- 模型 0：现役（不重训，直接加载）---
    if LIVE_MODEL.exists():
        booster = lgb.Booster(model_file=str(LIVE_MODEL))
        live_cols = booster.feature_name()
        missing = [c for c in live_cols if c not in test.columns]
        if not missing:
            s = booster.predict(test[live_cols])
            row = {"模型": "现役(标签A/全市场)", "训练样本": "全市场日频"}
            row["OOS_AUC"] = round(auc(y_eval, s), 4)
            row.update(topk_metrics(test.assign(_s=s), "_s"))
            results.append(row)
        else:
            print(f"[warn] 现役模型缺特征 {missing[:5]}，跳过基线")
    else:
        print(f"[warn] 现役模型不存在: {LIVE_MODEL}")

    # --- 模型 1/2：同超参小模型，训练集 = 封板样本 ---
    for tag, label_col, desc in (
        ("diag-A", "label_a", "标签A(T+1收盘涨停)"),
        ("diag-B", "label_b", "标签B(T+1开盘溢价)"),
    ):
        if train[label_col].nunique() < 2:
            print(f"[warn] {tag} 训练标签单一，跳过")
            continue
        scale = float((train[label_col] == 0).sum()) / max(int((train[label_col] == 1).sum()), 1)
        params = {**LGB_PARAMS, "scale_pos_weight": scale}
        dtr = lgb.Dataset(train[feat_cols], train[label_col])
        dte = lgb.Dataset(test[feat_cols], test[label_col], reference=dtr)
        model = lgb.train(
            params,
            dtr,
            num_boost_round=500,
            valid_sets=[dte],
            valid_names=["test"],
            callbacks=[lgb.early_stopping(30, verbose=False)],
        )
        s = model.predict(test[feat_cols], num_iteration=model.best_iteration)
        row = {
            "模型": f"{tag}({desc})",
            "训练样本": "封板样本",
            "OOS_AUC": round(auc(y_eval, s), 4),
        }
        row.update(topk_metrics(test.assign(_s=s), "_s"))
        row["best_iter"] = model.best_iteration
        results.append(row)
        print(f"[ok] {tag} 训练完成 best_iter={model.best_iteration}")

    # --- 额外模型（阶段2 重训 / 阶段3 特征化产物的统一评估）---
    for mpath in [m.strip() for m in args.model.split(",") if m.strip()]:
        mp = Path(mpath)
        if not mp.exists():
            print(f"[warn] 模型不存在: {mp}")
            continue
        booster = lgb.Booster(model_file=str(mp))
        mcols = booster.feature_name()
        missing = [c for c in mcols if c not in test.columns]
        if missing:
            print(f"[warn] {mp.name} 缺特征 {missing[:4]}，跳过")
            continue
        s = booster.predict(test[mcols])
        row = {"模型": mp.stem, "训练样本": "外部模型"}
        row["OOS_AUC"] = round(auc(y_eval, s), 4)
        row.update(topk_metrics(test.assign(_s=s), "_s"))
        results.append(row)
        print(f"[ok] 评估外部模型 {mp.name}")

    print("\n=== 统一评估集（阶段0口径）· 统一评估标签=T+1开盘净收益>0 ===")
    print(
        f"评估样本 {len(test):,} / 评估日数 {test['signal_date'].nunique()}"
        f" / 口径={'阶段0过滤后' if filtered else '未过滤'}"
    )
    print(pd.DataFrame(results).to_string(index=False))
    print(f"\n[info] 总耗时 {time.time() - t0:.0f}s")
    print("\n判定规则：若 diag-B 的 Top5 命中率 ≥ 现役 + 2pp → 阶段 2 值得做；")
    print("          否则阶段 2 降级（只改样本空间，不改标签）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
