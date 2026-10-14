"""scripts/train_pnl_huber_industry.py — Huber + akshare 申万行业特征训练 (Task D 升级)

数据: data/double_up/d_signal_5d_features_v9_industry.csv (33 列)
特征: 26 原 + 6 行业 + 1 industry_code = 33 维(训练时排除 industry_code)

模型: PnL Huber (alpha=0.5) regressor
输出: d_signal_5d_model_vpnl_huber_industry.txt
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9_industry.csv"
VPNL_DATA = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
MODEL_OUT = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_industry.txt"
REPORT_OUT = ROOT / "data" / "double_up" / f"pnl_huber_industry_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"

META_COLS = ["code", "signal_date", "is_positive", "industry_code"]  # industry_code 不入特征
ALL_FEATURE_COLS = [c for c in pd.read_csv(SOURCE, nrows=0).columns if c not in META_COLS]


def attach_pnl_simple(df: pd.DataFrame) -> pd.DataFrame:
    if not VPNL_DATA.exists():
        sys.exit(f"❌ 缺失 vpnl data: {VPNL_DATA}")
    pnl_df = pd.read_csv(VPNL_DATA, usecols=["code", "signal_date", "hypothetical_pnl"])
    df = df.merge(pnl_df, on=["code", "signal_date"], how="left")
    return df


def clean_inf(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if df[c].dtype in [np.float64, np.float32]:
            n_inf = (~np.isfinite(df[c])).sum()
            if n_inf > 0:
                df[c] = df[c].replace([np.inf, -np.inf], np.nan)
    return df


def main():
    print("=== PnL Huber + akshare 申万行业特征训练 (Task D 升级) ===\n")

    df = pd.read_csv(SOURCE)
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")
    print("  含 industry_code 列(分组键,不入特征)")
    df = clean_inf(df)

    df = attach_pnl_simple(df)
    valid_pnl = df["hypothetical_pnl"].notna().sum()
    print(f"\nPnL 合并: {valid_pnl}/{len(df)} 有效")

    if valid_pnl < 100:
        sys.exit("❌ PnL 样本过少")

    # 训练
    print(f"\n[Phase 1] LGBM Huber 训练 ({len(ALL_FEATURE_COLS)} 维)")
    import lightgbm as lgb
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import StratifiedKFold, train_test_split

    df_clean = df.dropna(subset=["hypothetical_pnl"] + ALL_FEATURE_COLS).copy()
    print(f"  过滤 NaN: {len(df)} → {len(df_clean)}")

    X = df_clean[ALL_FEATURE_COLS].astype(float)
    y = df_clean["hypothetical_pnl"].astype(float)
    stratify_y = df_clean["is_positive"].astype(int)

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    fold_mse, fold_r2, fold_mae = [], [], []

    for fold_i, (tr_idx, te_idx) in enumerate(skf.split(X, stratify_y), 1):
        X_tr, X_te = X.iloc[tr_idx], X.iloc[te_idx]
        y_tr, y_te = y.iloc[tr_idx], y.iloc[te_idx]
        model = lgb.LGBMRegressor(
            objective="huber", alpha=0.5,
            n_estimators=500, learning_rate=0.05, max_depth=6,
            num_leaves=31, min_child_samples=20,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=-1, verbose=-1,
        )
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
        pred = model.predict(X_te)
        fold_mse.append(float(mean_squared_error(y_te, pred)))
        fold_r2.append(float(r2_score(y_te, pred)))
        fold_mae.append(float(mean_absolute_error(y_te, pred)))
        print(f"  Fold {fold_i}: MSE={fold_mse[-1]:.4f}, R²={fold_r2[-1]:.4f}, MAE={fold_mae[-1]:.4f}")

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=stratify_y)
    model_final = lgb.LGBMRegressor(
        objective="huber", alpha=0.5,
        n_estimators=500, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model_final.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
    pred = model_final.predict(X_te)
    test_mse = float(mean_squared_error(y_te, pred))
    test_r2 = float(r2_score(y_te, pred))
    test_mae = float(mean_absolute_error(y_te, pred))
    importance = pd.Series(model_final.feature_importances_, index=ALL_FEATURE_COLS).sort_values(ascending=False)
    model_final.booster_.save_model(str(MODEL_OUT))

    print(f"\n  CV R²: {np.mean(fold_r2):.4f}  MAE: {np.mean(fold_mae):.4f}")
    print(f"  Test R²: {test_r2:.4f}  MAE: {test_mae:.4f}")

    print("\n[Phase 2] 特征重要性 Top 25")
    new_feats = ["ind_ret_5d_avg", "ind_amt_avg_5d", "ind_pct_t1_avg", "ind_volatility_avg", "ind_stock_count", "ret_minus_ind_5d"]
    for i, (k, v) in enumerate(importance.head(25).items(), 1):
        tag = ""
        if k in new_feats:
            tag = " (NEW · 申万行业)"
        elif k in ["dist_to_5d_high", "amt_per_price"]:
            tag = " (engineering)"
        print(f"  {i:2d}. {k:25s} {v:.0f}{tag}")

    with REPORT_OUT.open("w", encoding="utf-8") as f:
        f.write("# PnL Huber + akshare 申万行业特征训练报告\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write("## 1. 数据源\n\n")
        f.write("- **行业分类体系**:申万(SW)三级(国内主流,335 个三级行业)\n")
        f.write("- **接口**: `ak.stock_industry_clf_hist_sw()` 个股 → 行业历史变更(5930 只,12925 条)\n")
        f.write("- **更新频率**:申万每月换\n")
        f.write("- **缓存**: `data/industry/sw_stock_industry.csv` 30 天 TTL\n\n")

        f.write("## 2. 新增特征(6 维,基于 akshare 申万行业)\n\n")
        f.write("| 特征 | 含义 |\n|---|---|\n")
        f.write("| `ind_ret_5d_avg` | 同行业当日 5d 平均收益(行业动量) |\n")
        f.write("| `ind_amt_avg_5d` | 同行业当日 5d 平均成交额(行业资金流) |\n")
        f.write("| `ind_pct_t1_avg` | 同行业当日平均昨日方向 |\n")
        f.write("| `ind_volatility_avg` | 同行业当日平均波动率 |\n")
        f.write("| `ind_stock_count` | 同行业当日股票数(行业容量) |\n")
        f.write("| `ret_minus_ind_5d` | 个股 ret_5d - 行业均值(相对强弱) |\n\n")

        f.write("## 3. 训练结果\n\n")
        f.write(f"- 样本: **{len(df_clean)}**\n")
        f.write(f"- 特征: **{len(ALL_FEATURE_COLS)}** (原 26 + 6 akshare 申万行业)\n")
        f.write("- objective: **Huber (alpha=0.5)**\n")
        f.write(f"- 5-fold CV R²: **{np.mean(fold_r2):.4f}** (MAE: {np.mean(fold_mae):.4f})\n")
        f.write(f"- Test R²: **{test_r2:.4f}** (MAE: {test_mae:.4f})\n\n")

        f.write("## 4. 特征重要性 Top 20\n\n")
        f.write("| 排名 | 特征 | 重要性 | 标记 |\n|---|---:|---:|---|\n")
        for i, (k, v) in enumerate(importance.head(20).items(), 1):
            tag = ""
            if k in new_feats:
                tag = "**申万 NEW**"
            elif k in ["dist_to_5d_high", "amt_per_price"]:
                tag = "工程化"
            f.write(f"| {i} | `{k}` | {v:.0f} | {tag} |\n")

        f.write("\n## 5. 对比\n\n")
        f.write("| 模型 | 特征 | Test R² |\n|---|---:|---:|\n")
        f.write("| PnL proxy (原) | 20 | 0.1657 |\n")
        f.write("| PnL Huber (Task C) | 20 | 0.1612 |\n")
        f.write("| PnL Huber + Market (Task D 代理) | 35 | 0.2777 |\n")
        f.write(f"| **PnL Huber + akshare SW (本轮)** | **{len(ALL_FEATURE_COLS)}** | **{test_r2:.4f}** |\n\n")

        f.write("## 6. 下一步\n\n")
        f.write("- 跑 sim_live 验证真实胜率(需 predict.py 加 6 行业特征)\n")
        f.write("- 若累计优于 PnL Huber+Market (-808.7%),可考虑上线\n")

    print(f"\n  模型: {MODEL_OUT}")
    print(f"  报告: {REPORT_OUT}")
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
