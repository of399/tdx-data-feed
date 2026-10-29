"""scripts/train_pnl_proxy.py — PnL 代理目标训练 (替代 AUC)

核心思想:
  传统分类器训练:  P(翻倍) — 与"实际交易胜率"非直接对应
  PnL 代理训练:    E[PnL]   — 直接对应 sim_live 平均收益(目标)

流程:
  1. 对每个训练样本 (code, sig_date):
     - 加载 parquet 至 sig_date
     - 模拟 T+1 open 入场 + TP=30% / SL=-10% / HOLD=10d(与 sim_live 一致)
     - 记录 hypothetical_pnl(含 0.03% 佣金 + 0.10% 滑点,双边)
  2. 训练 LGBMRegressor(目标 = hypothetical_pnl)
  3. 预测时: regressor.predict(X) → PnL 估计 → Top N by PnL → 模拟交易

输出:
  - data/double_up/d_signal_5d_features_vpnl.csv    含 hypothetical_pnl 列的训练数据
  - data/double_up/d_signal_5d_model_vpnl.txt      PnL 回归模型
  - data/double_up/pnl_proxy_report_*.md           报告

用法:
    venv/bin/python scripts/train_pnl_proxy.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent

# 直跑脚本时 sys.path[0] 是 scripts/，`tdxfeed` 不在其下（与 plan_e_journal:100-106 同一处理）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.adjusted import MIN_FACTOR_COV, frame as adj_frame  # noqa: E402
from tdxfeed.execution.bars import as_bars  # noqa: E402
from tdxfeed.execution.costs import CostModel  # noqa: E402
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade  # noqa: E402

SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
FEATURE_BASE = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem",
]
# 加上 greedy 选出的 2 维工程化
FEATURE_VPNL = FEATURE_BASE + ["dist_to_5d_high", "amt_per_price"]

TP = 0.30
SL = -0.10
HOLD_DAYS = 10
FEE = 0.0003 * 2  # 双边佣金
SLIP = 0.0010 * 2  # 双边滑点

OUT_DATA = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
MODEL_OUT_BASE = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl.txt"
MODEL_OUT_ASYM = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_asym.txt"


def _load_one(code: str) -> pd.DataFrame:
    """从 data/parquet/daily 读单只股。"""
    from find_d_signal_5d import load_one as _load
    return _load(code)


def _load_one_basis(code: str, price_basis: str) -> pd.DataFrame:
    """raw 走原加载器（零改动）；hfq 走咽喉，覆盖率不足返回空表交给上层记 None。"""
    if price_basis == "raw":
        return _load_one(code)
    f = adj_frame(code, kind="hfq", strict=False)
    if f.attrs.get("factor_cov", 0.0) < MIN_FACTOR_COV:
        return pd.DataFrame()
    f = f.copy()
    f["date"] = pd.to_datetime(f["date"])
    f["code"] = code
    return f.sort_values("date").reset_index(drop=True)


def compute_hypothetical_pnl(code: str, sig_date: pd.Timestamp,
                             price_basis: str = "raw") -> float | None:
    """与 plan_e 同一结算口径的 PnL 代理标签。返回 None 表示数据不足。"""
    df = _load_one_basis(code, price_basis)
    if df.empty:
        return None
    sig_match = df.index[df["date"] == sig_date]
    if len(sig_match) == 0:
        return None
    sig_i = int(sig_match[0])
    if sig_i + 1 >= len(df):
        return None
    future = df.iloc[sig_i + 1: sig_i + 1 + HOLD_DAYS].reset_index(drop=True)
    bars = as_bars(future)
    entry = float(bars["open"].iloc[0])
    if not np.isfinite(entry) or entry <= 0:
        return None
    plan = SingleTradePlan(judge="intrabar", tp=TP, sl=SL, hold_days=HOLD_DAYS, sl_first=True)
    # stamp_sell 显式钉 0：与 plan_e / sim_live 同一口径「无印花税」，不靠 CostModel
    # 的 dataclass 默认值隐性维持（默认值一旦改动，标签与回测会静默分叉）。
    costs = CostModel(commission=FEE / 2, slippage=SLIP / 2, stamp_sell=0.0)
    r = settle_single_trade(bars, 0, plan, costs, insufficient="last_close")
    return None if r is None else float(r.net_ret)


def attach_pnl(df: pd.DataFrame, max_workers: int = 8,
               price_basis: str = "raw") -> pd.DataFrame:
    """给每个样本算 hypothetical_pnl(并行)。"""
    from concurrent.futures import ProcessPoolExecutor, as_completed

    rows = [(row["code"], pd.Timestamp(row["signal_date"])) for _, row in df.iterrows()]
    results: dict[int, float] = {}

    if max_workers > 1 and len(rows) > 100:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(compute_hypothetical_pnl, code, sd, price_basis): i
                    for i, (code, sd) in enumerate(rows)}
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception:
                    results[i] = None
                done += 1
                if done % 500 == 0 or done == len(rows):
                    valid = sum(1 for v in results.values() if v is not None)
                    print(f"    [{done}/{len(rows)}] valid_pnl={valid}", flush=True)
    else:
        for i, (code, sd) in enumerate(rows, 1):
            results[i - 1] = compute_hypothetical_pnl(code, sd, price_basis)
            if i % 500 == 0 or i == len(rows):
                valid = sum(1 for v in results.values() if v is not None)
                print(f"    [{i}/{len(rows)}] valid_pnl={valid}", flush=True)

    pnl_series = pd.Series([results.get(i) for i in range(len(df))], index=df.index)
    df = df.copy()
    df["hypothetical_pnl"] = pnl_series
    return df


def train_regressor(df: pd.DataFrame, feature_cols: list[str], n_folds: int = 5, loss_weight: float = 1.0, model_out: Path | None = None, objective: str = "regression", objective_alpha: float = 0.9):
    """训练 LGBMRegressor,5-fold CV 评估。

    参数:
      loss_weight: 亏损样本 (y<0) 的 sample_weight 乘子(loss_weight=1.0 → 原始)
      objective: regression(MSE) / huber / quantile / regression_l1(MAE)
      objective_alpha: huber 的 delta / quantile 的分位数
    """
    import lightgbm as lgb
    from sklearn.metrics import mean_squared_error, r2_score
    from sklearn.model_selection import StratifiedKFold, train_test_split

    # 过滤 NaN(部分样本未来数据不足,无法算 hypothetical_pnl)
    df_clean = df.dropna(subset=["hypothetical_pnl"] + feature_cols).copy()
    print(f"  过滤 NaN: {len(df)} → {len(df_clean)}")
    X = df_clean[feature_cols].astype(float)
    y = df_clean["hypothetical_pnl"].astype(float)
    stratify_y = df_clean["is_positive"].astype(int)

    # 亏损样本加权(2.0 即权重 ×2)
    sample_weight = np.where(y < 0, float(loss_weight), 1.0)
    n_neg = int((y < 0).sum())
    n_pos = int((y >= 0).sum())
    if loss_weight != 1.0:
        print(f"  加权: 亏损样本 × {loss_weight:.1f} ({n_neg} 笔亏损 / {n_pos} 笔盈利)")

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    fold_mse: list[float] = []
    fold_r2: list[float] = []

    for fold_i, (tr_idx, te_idx) in enumerate(skf.split(X, stratify_y), 1):
        X_tr, X_te = X.iloc[tr_idx], X.iloc[te_idx]
        y_tr, y_te = y.iloc[tr_idx], y.iloc[te_idx]
        w_tr = sample_weight[tr_idx]
        model = lgb.LGBMRegressor(
            n_estimators=500, learning_rate=0.05, max_depth=6,
            num_leaves=31, min_child_samples=20,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=-1, verbose=-1,
        )
        model.fit(X_tr, y_tr, sample_weight=w_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
        pred = model.predict(X_te)
        fold_mse.append(float(mean_squared_error(y_te, pred)))
        fold_r2.append(float(r2_score(y_te, pred)))
        print(f"  Fold {fold_i}: MSE={fold_mse[-1]:.4f}, R²={fold_r2[-1]:.4f}")

    # 训练最终模型
    X_tr, X_te, y_tr, y_te, w_tr, w_te = train_test_split(X, y, sample_weight, test_size=0.2, random_state=42, stratify=stratify_y)
    model_final = lgb.LGBMRegressor(
        n_estimators=500, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model_final.fit(X_tr, y_tr, sample_weight=w_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
    pred = model_final.predict(X_te)
    test_mse = float(mean_squared_error(y_te, pred))
    test_r2 = float(r2_score(y_te, pred))
    importance = pd.Series(model_final.feature_importances_, index=feature_cols).sort_values(ascending=False)
    out_path = model_out or MODEL_OUT_BASE
    model_final.booster_.save_model(str(out_path))

    return {
        "cv_mse_mean": float(np.mean(fold_mse)),
        "cv_r2_mean": float(np.mean(fold_r2)),
        "test_mse": test_mse,
        "test_r2": test_r2,
        "importance": importance,
        "model_path": str(out_path),
        "loss_weight": loss_weight,
        "n_neg": n_neg,
        "n_pos": n_pos,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loss-weight", type=float, default=1.0,
                        help="亏损样本权重乘子(默认 1.0=原始; 推荐 2.0 加亏权)")
    parser.add_argument("--model-out", type=str, default=None,
                        help="模型保存路径(默认 BASE 或 ASYM 取决于 loss-weight)")
    parser.add_argument("--objective", type=str, default="regression",
                        choices=["regression", "huber", "quantile", "regression_l1"],
                        help="目标函数 (默认 regression=标准 MSE; 推荐 huber/quantile 鲁棒)")
    parser.add_argument("--objective-alpha", type=float, default=0.9,
                        help="huber 的 delta / quantile 的分位数 (默认 0.9)")
    args_cli = parser.parse_args()

    print("=== PnL 代理目标训练 (替代 AUC) ===\n")
    print(f"loss_weight = {args_cli.loss_weight:.1f}")
    print(f"objective  = {args_cli.objective} (alpha={args_cli.objective_alpha:.2f})")
    if not SOURCE.exists():
        sys.exit(f"❌ 训练数据不存在: {SOURCE}")

    df = pd.read_csv(SOURCE)
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")

    # 如已存在 OUT_DATA,优先合并 PnL(避免重复计算)
    if OUT_DATA.exists():
        existing = pd.read_csv(OUT_DATA, usecols=["code", "signal_date", "hypothetical_pnl"])
        if existing["hypothetical_pnl"].notna().sum() > len(existing) * 0.9:
            df = df.merge(existing, on=["code", "signal_date"], how="left")
            valid_pnl = df["hypothetical_pnl"].notna().sum()
            print(f"\n[Phase 1] 复用 OUT_DATA PnL 列 ({valid_pnl} 有效 / {len(df)} 总)")
        else:
            print("\n[Phase 1] 计算 hypothetical PnL (并行 8 进程)...")
            t0 = datetime.now()
            df = attach_pnl(df, max_workers=8)
            valid_pnl = df["hypothetical_pnl"].notna().sum()
            print(f"\n  有效 PnL: {valid_pnl}/{len(df)}")
            print(f"  耗时: {(datetime.now()-t0).total_seconds():.0f}s")
            df.to_csv(OUT_DATA, index=False)
    else:
        print("\n[Phase 1] 计算 hypothetical PnL (并行 8 进程)...")
        t0 = datetime.now()
        df = attach_pnl(df, max_workers=8)
        valid_pnl = df["hypothetical_pnl"].notna().sum()
        print(f"\n  有效 PnL: {valid_pnl}/{len(df)}")
        print(f"  耗时: {(datetime.now()-t0).total_seconds():.0f}s")
        df.to_csv(OUT_DATA, index=False)
        print(f"\n  数据: {OUT_DATA}")

    if valid_pnl < 100:
        sys.exit("❌ 有效 PnL 样本过少,退出")

    # 检查 PnL 分布
    pnl_stats = df["hypothetical_pnl"].describe()
    print("\n[PnL 分布]")
    print(pnl_stats)

    # 保存带 PnL 的训练数据
    df.to_csv(OUT_DATA, index=False)
    print(f"\n  数据: {OUT_DATA}")

    # 训练
    print("\n[Phase 2] LGBMRegressor 训练 (目标 = hypothetical_pnl)...")
    print(f"  特征: {len(FEATURE_VPNL)} 维 (18 基础 + 2 greedy 选中)")

    # 决定模型保存路径
    if args_cli.model_out:
        model_out = Path(args_cli.model_out)
    elif args_cli.loss_weight != 1.0:
        model_out = MODEL_OUT_ASYM
    elif args_cli.objective != "regression":
        suffix = args_cli.objective
        model_out = ROOT / "data" / "double_up" / f"d_signal_5d_model_vpnl_{suffix}.txt"
    else:
        model_out = MODEL_OUT_BASE

    metrics = train_regressor(
        df, FEATURE_VPNL,
        loss_weight=args_cli.loss_weight,
        model_out=model_out,
        objective=args_cli.objective,
        objective_alpha=args_cli.objective_alpha,
    )
    print(f"\n  CV MSE: {metrics['cv_mse_mean']:.4f}  R²: {metrics['cv_r2_mean']:.4f}")
    print(f"  Test MSE: {metrics['test_mse']:.4f}  R²: {metrics['test_r2']:.4f}")

    print("\n[特征重要性 Top 15]")
    for i, (k, v) in enumerate(metrics["importance"].head(15).items(), 1):
        tag = " **(NEW)**" if k in ["dist_to_5d_high", "amt_per_price"] else ""
        print(f"  {i:2d}. {k:22s} {v:.0f}{tag}")

    # 报告
    suffix = f"_asym_w{args_cli.loss_weight:.1f}" if args_cli.loss_weight != 1.0 else ""
    report_path = ROOT / "data" / "double_up" / f"pnl_proxy_report_{datetime.now().strftime('%Y%m%d_%H%M')}{suffix}.md"
    pnl_stats = df["hypothetical_pnl"].describe()
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# PnL 代理目标训练报告" + (f" (loss_weight={args_cli.loss_weight:.1f})" if args_cli.loss_weight != 1.0 else "") + "\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write("## 1. 核心思想\n\n")
        f.write("- 传统分类器: 训练目标 `P(翻倍)` (AUC 衡量)\n")
        f.write("- PnL 代理: 训练目标 `E[PnL]` (R² / MSE 衡量)\n")
        f.write("- **PnL 代理直接对应 sim_live 平均收益(我们的真实目标)**\n\n")
        f.write("## 2. hypothetical_pnl 计算口径\n\n")
        f.write("- T+1 open 入场\n")
        f.write("- TP=+30%, SL=-10%, HOLD=10d\n")
        f.write(f"- 含 {FEE*100:.2f}% 佣金 + {SLIP*100:.2f}% 滑点(双边)\n\n")
        if args_cli.loss_weight != 1.0:
            f.write(f"## 2.5 加亏权: 亏损样本 (y<0) sample_weight × {args_cli.loss_weight:.1f}\n\n")
            f.write(f"- 盈利样本 {metrics['n_pos']} 笔(权重 1.0)\n")
            f.write(f"- 亏损样本 {metrics['n_neg']} 笔(权重 {args_cli.loss_weight:.1f})\n")
            f.write("- 目标: 让模型对亏损预测更敏感,在 Top N 选股中过滤「看似高 PnL 实则亏」的样本\n\n")
        f.write("## 3. PnL 分布\n\n")
        f.write("| 统计 | 值 |\n|---|---|\n")
        for k in ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]:
            f.write(f"| {k} | {pnl_stats[k]:.4f} |\n")
        f.write(f"\n  正 PnL 样本: {(df['hypothetical_pnl'] > 0).sum()}/{valid_pnl} ({(df['hypothetical_pnl'] > 0).mean()*100:.1f}%)\n\n")

        f.write("## 4. 训练结果\n\n")
        f.write(f"- 样本: **{valid_pnl}**\n")
        f.write(f"- 特征: **{len(FEATURE_VPNL)}** (18 base + 2 greedy)\n")
        f.write(f"- loss_weight: **{args_cli.loss_weight:.1f}**\n")
        f.write(f"- 5-fold CV R²: **{metrics['cv_r2_mean']:.4f}**\n")
        f.write(f"- Test R²: **{metrics['test_r2']:.4f}**\n\n")

        f.write("## 5. 特征重要性 Top 15\n\n")
        f.write("| 排名 | 特征 | 重要性 |\n|---|---:|---:|\n")
        for i, (k, v) in enumerate(metrics["importance"].head(15).items(), 1):
            tag = " **(NEW)**" if k in ["dist_to_5d_high", "amt_per_price"] else ""
            f.write(f"| {i} | `{k}` | {v:.0f}{tag} |\n")

        f.write("\n## 6. 下一步\n\n")
        f.write("- 用此模型跑 sim_live(PnL 排序 vs 概率排序)\n")
        f.write("- 若真实胜率/收益优于 v8(32.1% / -0.92%)与 vpnl(32.1% / -1.39%),可考虑上线\n")
        f.write(f"- 模型文件: `{model_out.name}`\n")

    print(f"\n  模型: {metrics['model_path']}")
    print(f"  报告: {report_path}")
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
