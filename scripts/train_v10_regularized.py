"""scripts/train_v10_regularized.py — v10 正则化训练

目标:在 v9 (26 维) 基础上引入正则化,降低过拟合、提升泛化:
  - L1 (reg_alpha=0.1):  稀疏化,自动剔除弱贡献特征
  - L2 (reg_lambda=0.1): 平滑化,降低极端权重
  - min_child_samples 20 → 50: 更保守的叶节点分裂
  - n_estimators 500 → 200 (early_stopping): 减少树数
  - Dropout: boosting_type='dart', drop_rate=0.1: 训练时随机丢弃树,等价于模型 Dropout

数据: data/double_up/d_signal_5d_features_v9.csv (12884 样本 × 26 维)
输出:
  - data/double_up/d_signal_5d_model_v10.txt       LightGBM v10 模型 (DART)
  - data/double_up/d_signal_5d_model_v10_gbdt.txt  对照 GBDT (便于回退对比)
  - data/double_up/v10_regularization_report_*.md  报告

用法:
    venv/bin/python scripts/train_v10_regularized.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
MODEL_DART = ROOT / "data" / "double_up" / "d_signal_5d_model_v10.txt"
MODEL_GBDT = ROOT / "data" / "double_up" / "d_signal_5d_model_v10_gbdt.txt"


def cv_train_dart(df: pd.DataFrame, n_folds: int = 5) -> tuple:
    """DART (Dropout) 训练 + 5-fold CV。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    meta_cols = ["code", "signal_date", "is_positive"]
    feature_cols = [c for c in df.columns if c not in meta_cols]
    X = df[feature_cols].astype(float)
    y = df["is_positive"].astype(int)

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    fold_aucs: list[float] = []
    fold_imp: list[pd.Series] = []

    for fold_i, (tr_idx, te_idx) in enumerate(skf.split(X, y), 1):
        X_tr, X_te = X.iloc[tr_idx], X.iloc[te_idx]
        y_tr, y_te = y.iloc[tr_idx], y.iloc[te_idx]
        # DART 模式不支持 early_stopping,固定 200 树
        model = lgb.LGBMClassifier(
            boosting_type="dart",
            n_estimators=200,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            min_child_samples=50,        # ↑ 20 → 50
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,                # L1 (新)
            reg_lambda=0.1,               # L2 (新)
            drop_rate=0.1,                # Dropout 比例
            max_drop=50,
            skip_drop=0.5,
            random_state=42,
            n_jobs=-1,
            verbose=-1,
        )
        model.fit(X_tr, y_tr)
        pred = model.predict_proba(X_te)[:, 1]
        auc = float(roc_auc_score(y_te, pred))
        fold_aucs.append(auc)
        fold_imp.append(pd.Series(model.feature_importances_, index=feature_cols))
        print(f"  DART Fold {fold_i}: AUC={auc:.4f}")

    return float(np.mean(fold_aucs)), float(np.std(fold_aucs)), fold_imp, feature_cols


def train_final_dart(df: pd.DataFrame, feature_cols: list[str]):
    """用全部数据训练最终 DART 模型。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    X = df[feature_cols].astype(float)
    y = df["is_positive"].astype(int)
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    model = lgb.LGBMClassifier(
        boosting_type="dart",
        n_estimators=200,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        min_child_samples=50,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
        drop_rate=0.1,
        max_drop=50,
        skip_drop=0.5,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X_tr, y_tr)
    pred = model.predict_proba(X_te)[:, 1]
    test_auc = float(roc_auc_score(y_te, pred))
    # DART 保存: Booster.save_model 直接支持
    model.booster_.save_model(str(MODEL_DART))
    return test_auc, model


def train_gbdt_baseline(df: pd.DataFrame, feature_cols: list[str]):
    """训练对照 GBDT (无正则化,baseline 用于对比)。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    X = df[feature_cols].astype(float)
    y = df["is_positive"].astype(int)
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    model = lgb.LGBMClassifier(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        min_child_samples=20,           # baseline 20
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
    pred = model.predict_proba(X_te)[:, 1]
    test_auc = float(roc_auc_score(y_te, pred))
    model.booster_.save_model(str(MODEL_GBDT))
    return test_auc, model


def main():
    print("=== v10 正则化训练 (L1/L2 + min_child_samples↑ + n_estimators↓ + DART Dropout) ===\n")
    if not DATA.exists():
        sys.exit(f"❌ 训练数据不存在: {DATA}\n先跑 feature_engineering.py 生成 v9 数据")

    df = pd.read_csv(DATA)
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")

    print("\n[Phase 1] DART 5-fold CV ...")
    cv_mean, cv_std, fold_imp, feature_cols = cv_train_dart(df)
    print(f"\n  CV 平均 AUC: {cv_mean:.4f} ± {cv_std:.4f}")

    print("\n[Phase 2] DART 训练最终模型...")
    dart_test_auc, _ = train_final_dart(df, feature_cols)
    print(f"  Test AUC (DART final): {dart_test_auc:.4f}")

    print("\n[Phase 3] GBDT baseline 对照...")
    gbdt_test_auc, _ = train_gbdt_baseline(df, feature_cols)
    print(f"  Test AUC (GBDT baseline): {gbdt_test_auc:.4f}")

    # 特征重要性 (跨 fold 平均)
    avg_imp = sum(fold_imp) / len(fold_imp)
    avg_imp = avg_imp.sort_values(ascending=False)
    print(f"\n[Phase 4] Top 15 特征重要性 (DART 跨 fold 平均):")
    for i, (k, v) in enumerate(avg_imp.head(15).items(), 1):
        print(f"  {i:2d}. {k:22s} {v:.0f}")

    # 报告
    new_names = {
        "acceleration", "range_efficiency", "vol_norm_ret", "amount_surge_3d",
        "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low", "amt_per_price",
    }
    report_path = ROOT / "data" / "double_up" / f"v10_regularization_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# v10 正则化训练报告\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")

        f.write("## 1. 正则化策略对比\n\n")
        f.write("| 参数 | v9 (baseline) | v10 (正则化) |\n|---|---|---|\n")
        f.write("| boosting_type | gbdt | **dart** (Dropout) |\n")
        f.write("| n_estimators | 500 | **200** |\n")
        f.write("| min_child_samples | 20 | **50** |\n")
        f.write("| reg_alpha (L1) | 0 | **0.1** |\n")
        f.write("| reg_lambda (L2) | 0 | **0.1** |\n")
        f.write("| drop_rate | — | **0.1** |\n\n")

        f.write("## 2. 训练结果\n\n")
        f.write(f"- 样本: **{len(df)}**\n")
        f.write(f"- 特征数: **{len(feature_cols)}** (26 维,同 v9)\n")
        f.write(f"- **DART 5-fold CV AUC: {cv_mean:.4f} ± {cv_std:.4f}**\n")
        f.write(f"- **DART Test AUC (final): {dart_test_auc:.4f}**\n")
        f.write(f"- GBDT baseline Test AUC: {gbdt_test_auc:.4f}\n\n")

        f.write("## 3. 特征重要性 Top 15 (DART 跨 fold 平均)\n\n")
        f.write("| 排名 | 特征 | 重要性 | 标记 |\n|---|---:|---:|---|\n")
        for i, (k, v) in enumerate(avg_imp.head(15).items(), 1):
            tag = "**NEW**" if k in new_names else ""
            f.write(f"| {i} | `{k}` | {v:.0f} | {tag} |\n")

        f.write("\n## 4. 对比 v9\n\n")
        f.write("| 指标 | v9 (无正则) | v10 (正则+Dropout) | 变化 |\n|---|---|---|---|\n")
        f.write(f"| 特征数 | 26 | {len(feature_cols)} | 0 |\n")
        f.write(f"| 5-fold CV AUC | 0.8054 | {cv_mean:.4f} | {cv_mean-0.8054:+.4f} |\n")
        f.write(f"| Test AUC | 0.8138 | {dart_test_auc:.4f} | {dart_test_auc-0.8138:+.4f} |\n")
        f.write(f"| GBDT 对照 | 0.8138 | {gbdt_test_auc:.4f} | — |\n\n")

        f.write("## 5. 解读\n\n")
        if dart_test_auc > gbdt_test_auc:
            f.write("✅ **DART > GBDT**:正则化+Dropout 有效,泛化能力提升。\n\n")
        elif dart_test_auc > 0.81:
            f.write("⚠️ **DART ≈ GBDT**:正则化未显著提升 AUC(可能因 v9 已较稳健)。\n\n")
        else:
            f.write("❌ **DART < GBDT**:正则化导致 AUC 下降,需重新调参。\n\n")

        # 检查 Dropout 实际生效
        non_zero = (avg_imp > 0).sum()
        f.write(f"### Dropout 影响\n")
        f.write(f"- 非零重要性特征数: **{non_zero}/{len(feature_cols)}** (其余被 Dropout 稀疏化)\n")
        zero_features = [k for k in feature_cols if avg_imp.get(k, 0) == 0]
        if zero_features:
            f.write(f"- 被稀疏化的特征: {zero_features}\n")

    print(f"\n=== 完成 ===")
    print(f"  DART 模型: {MODEL_DART}")
    print(f"  GBDT 对照: {MODEL_GBDT}")
    print(f"  报告: {report_path}")


if __name__ == "__main__":
    main()
