"""scripts/train_pnl_huber_market.py — Huber + 市场代理特征训练 (Task C+D 合并)

训练:
  - 数据: data/double_up/d_signal_5d_features_v9_market.csv (35 维)
  - 目标: hypothetical_pnl(从 d_signal_5d_features_vpnl.csv 合并)
  - 损失: Huber (alpha=0.5) — 鲁棒于 TP-capped 离群点
  - 输出: d_signal_5d_model_vpnl_huber_market.txt
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9_market.csv"
VPNL_DATA = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
MODEL_OUT = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_market.txt"
REPORT_OUT = ROOT / "data" / "double_up" / f"pnl_huber_market_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"

META_COLS = ["code", "signal_date", "is_positive"]
ALL_FEATURE_COLS = [c for c in pd.read_csv(SOURCE, nrows=0).columns if c not in META_COLS]


def attach_pnl_simple(df: pd.DataFrame) -> pd.DataFrame:
    if not VPNL_DATA.exists():
        sys.exit(f"❌ 缺失 vpnl data: {VPNL_DATA} (先跑 train_pnl_proxy.py 至少 1 次)")
    pnl_df = pd.read_csv(VPNL_DATA, usecols=["code", "signal_date", "hypothetical_pnl"])
    df = df.merge(pnl_df, on=["code", "signal_date"], how="left")
    return df


def clean_market_inf(df: pd.DataFrame) -> pd.DataFrame:
    market_cols = ["mkt_ret_5d_avg", "mkt_amt_avg_5d", "mkt_pct_t1_avg"]
    for c in market_cols:
        if c in df.columns:
            n_inf = (~np.isfinite(df[c])).sum()
            if n_inf > 0:
                print(f"  {c}: {n_inf} 个 inf → 替换为 0")
                df[c] = df[c].replace([np.inf, -np.inf], np.nan).fillna(0)
    return df


def main():
    print("=== PnL Huber + 市场代理特征训练 (Task C+D 合并) ===\n")

    df = pd.read_csv(SOURCE)
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")
    df = clean_market_inf(df)

    df = attach_pnl_simple(df)
    valid_pnl = df["hypothetical_pnl"].notna().sum()
    print(f"\nPnL 合并: {valid_pnl}/{len(df)} 有效")

    if valid_pnl < 100:
        sys.exit("❌ PnL 样本过少")

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

    print("\n[Phase 2] 特征重要性 Top 20")
    for i, (k, v) in enumerate(importance.head(20).items(), 1):
        tag = ""
        if k.startswith("board_") or k.startswith("mkt_"):
            tag = " (NEW)"
        elif k in ["dist_to_5d_high", "amt_per_price"]:
            tag = " (engineering)"
        print(f"  {i:2d}. {k:25s} {v:.0f}{tag}")

    with REPORT_OUT.open("w", encoding="utf-8") as f:
        f.write("# PnL Huber + 市场代理特征训练报告\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write("## 1. 改进维度\n\n")
        f.write("- **损失函数**: MSE → **Huber (alpha=0.5)**,鲁棒于 TP-capped 离群点\n")
        f.write(f"- **特征维度**: 20 → **{len(ALL_FEATURE_COLS)}** (+4 市场代理 + 5 板块 one-hot)\n")
        f.write("- **核心新增特征**:\n")
        f.write("  - `mkt_ret_5d_avg`: 当日全市场平均 5d 收益\n")
        f.write("  - `mkt_amt_avg_5d`: 当日全市场平均 5d 成交额\n")
        f.write("  - `mkt_pct_t1_avg`: 当日全市场平均 pct_t1\n")
        f.write("  - `stock_count_active`: 当日有效股票数\n")
        f.write("  - `board_sh_main` / `board_sh_kechuang` / `board_sz_main` / `board_sz_chinext` / `board_bj`: 5 板块 one-hot\n\n")

        f.write("## 2. 板块分布\n\n")
        boards_count = df_clean["code"].apply(lambda c:
            'bj' if c.startswith("bj") else
            'sz_chinext' if c.startswith(("sz300", "sz301")) else
            'sz_main' if c.startswith(("sz000", "sz001", "sz002")) else
            'sh_kechuang' if c.startswith(("sh688", "sh689")) else
            'sh_main' if c.startswith(("sh600", "sh601", "sh603", "sh605")) else
            'other'
        ).value_counts()
        f.write("| 板块 | 笔数 | 占比 |\n|---|---:|---:|\n")
        for b, n in boards_count.items():
            f.write(f"| {b} | {n} | {n/len(df_clean)*100:.1f}% |\n")
        f.write("\n")

        f.write("## 3. 训练结果\n\n")
        f.write(f"- 样本: **{len(df_clean)}**\n")
        f.write(f"- 特征: **{len(ALL_FEATURE_COLS)}**\n")
        f.write(f"- 5-fold CV R²: **{np.mean(fold_r2):.4f}** (MAE: {np.mean(fold_mae):.4f})\n")
        f.write(f"- Test R²: **{test_r2:.4f}** (MAE: {test_mae:.4f})\n\n")

        f.write("## 4. 特征重要性 Top 20\n\n")
        f.write("| 排名 | 特征 | 重要性 | 标记 |\n|---|---:|---:|---|\n")
        for i, (k, v) in enumerate(importance.head(20).items(), 1):
            tag = ""
            if k.startswith("board_") or k.startswith("mkt_"):
                tag = "**NEW**"
            elif k in ["dist_to_5d_high", "amt_per_price"]:
                tag = "工程化"
            f.write(f"| {i} | `{k}` | {v:.0f} | {tag} |\n")

        f.write("\n## 6. 下一步\n\n")
        f.write("- 跑 sim_live 验证真实胜率\n")
        f.write("- 若胜率/收益优于 PnL Huber,可考虑上线\n")

    print(f"\n  模型: {MODEL_OUT}")
    print(f"  报告: {REPORT_OUT}")
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
