"""scripts/greedy_feature_selection.py — 贪心特征子集选择

策略:
  从 18 维基础特征(v8 baseline)出发,每轮贪心加 1 个工程化新特征(共 8 候选),
  用 5-fold stratified CV AUC 作为评估指标,选择带来最大 AUC 提升的特征。
  持续到: 5 轮内无显著改善(ΔAUC < threshold) 或全部 8 个特征用完。

输出:
  - data/double_up/d_signal_5d_model_greedy.txt       最佳子集模型
  - data/double_up/d_signal_5d_features_greedy.csv    最佳子集训练数据
  - data/double_up/greedy_feature_selection_*.md      报告

用法:
    venv/bin/python scripts/greedy_feature_selection.py [--threshold 0.001]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
MODEL_OUT = ROOT / "data" / "double_up" / "d_signal_5d_model_greedy.txt"
TARGET = ROOT / "data" / "double_up" / "d_signal_5d_features_greedy.csv"

# 18 维基础(v8 baseline)
FEATURE_BASE = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem",
]

# 8 维工程化候选(贪心逐个尝试)
NEW_CANDIDATES = [
    ("acceleration", "动量加速度"),
    ("range_efficiency", "价格区间效率"),
    ("vol_norm_ret", "波动率调整收益"),
    ("amount_surge_3d", "3 日成交额突增"),
    ("up_down_ratio", "涨跌天数比"),
    ("dist_to_5d_high", "距 5 日高点"),
    ("dist_to_5d_low", "距 5 日低点"),
    ("amt_per_price", "单位价格成交额"),
]


def cv_eval(df: pd.DataFrame, feature_cols: list[str], n_folds: int = 5) -> float:
    """5-fold stratified CV 评估给定特征子集的 AUC。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    X = df[feature_cols].astype(float)
    y = df["is_positive"].astype(int)

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    aucs: list[float] = []
    for tr_idx, te_idx in skf.split(X, y):
        X_tr, X_te = X.iloc[tr_idx], X.iloc[te_idx]
        y_tr, y_te = y.iloc[tr_idx], y.iloc[te_idx]
        model = lgb.LGBMClassifier(
            n_estimators=500, learning_rate=0.05, max_depth=6,
            num_leaves=31, min_child_samples=20,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=-1, verbose=-1,
        )
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
        pred = model.predict_proba(X_te)[:, 1]
        aucs.append(float(roc_auc_score(y_te, pred)))
    return float(np.mean(aucs))


def train_final(df: pd.DataFrame, feature_cols: list[str], out_path: Path) -> tuple[float, pd.Series]:
    """训练最终模型(80/20 split) + 保存 + 返回 (test_auc, importance)。"""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    X = df[feature_cols].astype(float)
    y = df["is_positive"].astype(int)
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    model = lgb.LGBMClassifier(
        n_estimators=500, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
    pred = model.predict_proba(X_te)[:, 1]
    test_auc = float(roc_auc_score(y_te, pred))
    importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    model.booster_.save_model(str(out_path))
    return test_auc, importance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=0.001, help="继续贪心的最小 AUC 提升阈值(默认 0.001)")
    parser.add_argument("--max-features", type=int, default=8, help="最多添加几个新特征(默认 8)")
    args = parser.parse_args()

    print("=== 贪心特征子集选择 (从 18 维 base 开始,逐步加 1 维工程化) ===\n")
    if not SOURCE.exists():
        sys.exit(f"❌ 训练数据不存在: {SOURCE}")

    df = pd.read_csv(SOURCE)
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")

    # 基线 AUC (18 维)
    print(f"\n[基线] 18 维基础特征 CV 评估...")
    baseline_auc = cv_eval(df, FEATURE_BASE)
    print(f"  Baseline AUC: {baseline_auc:.4f}")

    # 贪心 forward
    selected: list[str] = list(FEATURE_BASE)
    remaining = list(NEW_CANDIDATES)
    history: list[dict] = [{"step": 0, "auc": baseline_auc, "added": None, "subset_size": 18}]
    current_auc = baseline_auc

    for step in range(1, args.max_features + 1):
        print(f"\n[Step {step}] 评估候选特征 (当前 AUC={current_auc:.4f}, 已选 {len(selected)} 维)...")
        best_name, best_desc, best_auc = None, None, current_auc
        for name, desc in remaining:
            trial = selected + [name]
            auc = cv_eval(df, trial)
            marker = " ✓" if auc > best_auc else ""
            print(f"  +{name:20s} ({desc}) → AUC={auc:.4f}{marker}")
            if auc > best_auc:
                best_auc = auc
                best_name = name
                best_desc = desc

        if best_name is None:
            print(f"  无特征带来提升,停止。")
            break

        delta = best_auc - current_auc
        selected.append(best_name)
        remaining = [(n, d) for n, d in remaining if n != best_name]
        current_auc = best_auc
        history.append({
            "step": step, "auc": current_auc, "added": best_name,
            "desc": best_desc, "delta": delta, "subset_size": len(selected),
        })
        print(f"  → 添加: {best_name} (ΔAUC={delta:+.4f})")

        if delta < args.threshold:
            print(f"  ΔAUC {delta:+.4f} < threshold {args.threshold}, 停止贪心。")
            break

    print(f"\n=== 贪心完成 ===")
    print(f"  最佳子集: {len(selected)} 维 (基线 + {len(selected)-18} 工程化)")
    print(f"  最终 AUC: {current_auc:.4f}")
    print(f"  相比基线: {current_auc - baseline_auc:+.4f}")

    # 训练最终模型
    print(f"\n训练最终模型...")
    test_auc, importance = train_final(df, selected, MODEL_OUT)

    # 保存最佳子集训练数据
    cols_to_save = selected + ["code", "signal_date", "is_positive"]
    df[cols_to_save].to_csv(TARGET, index=False)
    print(f"  最佳子集数据: {TARGET}")

    # 报告
    report_path = ROOT / "data" / "double_up" / f"greedy_feature_selection_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# 贪心特征子集选择报告\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write("## 1. 贪心过程\n\n")
        f.write("| Step | AUC | ΔAUC | 添加特征 | 子集大小 |\n|---|---:|---:|---|---:|\n")
        for h in history:
            f.write(f"| {h['step']} | {h['auc']:.4f} | — | {h['added'] or '基线'} | {h['subset_size']} |\n")
        if len(history) > 1:
            last = history[-1]
            f.write(f"| {last['step']} | {last['auc']:.4f} | {last['delta']:+.4f} | **{last['added']}** | {last['subset_size']} |\n")

        f.write(f"\n## 2. 最终结果\n\n")
        f.write(f"- 基线 AUC (18 维): {baseline_auc:.4f}\n")
        f.write(f"- 最佳 AUC ({len(selected)} 维): {current_auc:.4f}\n")
        f.write(f"- 总提升: **{current_auc - baseline_auc:+.4f}**\n")
        f.write(f"- Test AUC (final 80/20): **{test_auc:.4f}**\n\n")

        f.write("## 3. 最佳子集特征重要性\n\n")
        f.write("| 排名 | 特征 | 重要性 |\n|---|---:|---:|\n")
        for i, (k, v) in enumerate(importance.items(), 1):
            tag = ""
            if k in [n for n, _ in NEW_CANDIDATES]:
                tag = " **(NEW)**"
            f.write(f"| {i} | `{k}` | {v:.0f}{tag} |\n")

        f.write("\n## 4. 对比\n\n")
        f.write("| 模型 | 特征数 | 5-fold CV AUC | Test AUC |\n|---|---:|---:|---:|\n")
        f.write(f"| v8 baseline (18 维) | 18 | {baseline_auc:.4f} | 0.7934 |\n")
        f.write(f"| v9 全 26 维 | 26 | 0.8054 | 0.8138 |\n")
        f.write(f"| **greedy 最佳子集** | **{len(selected)}** | **{current_auc:.4f}** | **{test_auc:.4f}** |\n")

    print(f"  报告: {report_path}")
    print(f"\n=== 完成 ===")


if __name__ == "__main__":
    main()
