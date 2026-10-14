"""scripts/feature_engineering.py — 18 维特征分析与扩展(v9 训练)

4 阶段流水线:
  Phase 1: 现有 18 维分析(重要性 + 相关性 + 冗余)
  Phase 2: 工程化新增 8 维 + 重建训练数据(26 维)
  Phase 3: 5-fold stratified CV 训练 v9 + 训练最终模型
  Phase 4: 输出综合报告 (Markdown)

8 维工程化新特征:
  1. acceleration       动量加速度: ret_5d - ret_3d
  2. range_efficiency   价格区间效率: mean((close-open)/(high-low))
  3. vol_norm_ret       波动率调整收益: ret_5d / volatility_5d
  4. amount_surge_3d    3 日成交额突增: amount[-1]/mean(amount[-3:])
  5. up_down_ratio      涨跌天数比: up/down
  6. dist_to_5d_high    距 5 日高点: (high.max()-close)/close
  7. dist_to_5d_low     距 5 日低点: (close-low.min())/close
  8. amt_per_price      单位价格成交额: avg_amount_5d/prev_close

用法:
    venv/bin/python scripts/feature_engineering.py

输出:
    data/double_up/d_signal_5d_features_v9.csv       26 维训练数据
    data/double_up/d_signal_5d_model_v9.txt          v9 模型
    data/double_up/feature_engineering_report_*.md   综合报告
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from _common import compute_window_features
from find_d_signal_5d import load_one

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features.csv"
TARGET = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
MODEL_OUT = ROOT / "data" / "double_up" / "d_signal_5d_model_v9.txt"

# 18 维基础特征
FEATURE_BASE = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem",
]

# 8 维工程化新特征 (name, 描述)
NEW_FEATURES = [
    ("acceleration", "动量加速度: ret_5d - ret_3d"),
    ("range_efficiency", "价格区间效率: mean((close-open)/(high-low))"),
    ("vol_norm_ret", "波动率调整收益: ret_5d / volatility_5d"),
    ("amount_surge_3d", "3 日成交额突增: amount[-1]/mean(amount[-3:])"),
    ("up_down_ratio", "涨跌天数比: up/down"),
    ("dist_to_5d_high", "距 5 日高点: (high.max()-close)/close"),
    ("dist_to_5d_low", "距 5 日低点: (close-low.min())/close"),
    ("amt_per_price", "单位价格成交额: avg_amount_5d/prev_close"),
]


def build_extended_features(window_df: pd.DataFrame, code: str) -> dict | None:
    """18 维基础 + 8 维工程化 = 26 维。"""
    base = compute_window_features(window_df, code)
    if base is None:
        return None
    prev_5 = window_df.iloc[-5:]
    close = prev_5["close"]
    pct = close.pct_change().fillna(0)
    amount = prev_5["amount"]
    high = prev_5["high"]
    low = prev_5["low"]
    open_ = prev_5["open"]

    accel = float(base["ret_5d"] - base["ret_3d"])

    daily_range = (high - low).replace(0, np.nan)
    efficiency = float(((close - open_) / daily_range).mean())
    if np.isnan(efficiency):
        efficiency = 0.0

    vol_norm = float(base["ret_5d"] / base["volatility_5d"]) if base["volatility_5d"] > 0 else 0.0

    amount_3d_mean = float(amount.iloc[-3:].mean())
    surge_3d = float(amount.iloc[-1] / amount_3d_mean) if amount_3d_mean > 0 else 1.0

    up_days = int((pct > 0).sum())
    down_days = int((pct < 0).sum())
    up_down = float(up_days / max(down_days, 1))

    high_max = float(high.max())
    dist_high = float((high_max - close.iloc[-1]) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0

    low_min = float(low.min())
    dist_low = float((close.iloc[-1] - low_min) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0

    amt_per_price = float(base["avg_amount_5d"] / base["prev_close"]) if base["prev_close"] > 0 else 0.0

    return {
        **base,
        "acceleration": accel,
        "range_efficiency": efficiency,
        "vol_norm_ret": vol_norm,
        "amount_surge_3d": surge_3d,
        "up_down_ratio": up_down,
        "dist_to_5d_high": dist_high,
        "dist_to_5d_low": dist_low,
        "amt_per_price": amt_per_price,
    }


def phase1_analyze(df_source: pd.DataFrame) -> tuple[dict, list, list]:
    """分析现有 18 维特征: 重要性(v8 模型) + 相关性矩阵 + 低贡献识别。"""
    print("\n=== Phase 1: 18 维特征分析 ===")

    v8_importance = {
        "avg_amount_5d": 341, "prev_close": 327, "turnover_proxy": 296,
        "close_pos_5d": 235, "max_5d_pct": 223, "ret_5d": 203,
        "volatility_5d": 200, "ret_3d": 195, "pct_t1": 173,
        "max_drawdown_5d": 161, "vol_ratio_t1": 142, "limit": 93,
        "is_new": 42, "is_gem": 14, "limit_up_5d": 8,
        "limit_up_t1": 0, "is_st": 0, "is_bj": 0,
    }

    print("\n[v8 模型特征重要性排序]")
    for i, (k, v) in enumerate(sorted(v8_importance.items(), key=lambda x: -x[1]), 1):
        tag = " ← 低贡献" if v < 20 else ""
        print(f"  {i:2d}. {k:20s} {v:4d}{tag}")

    low_contrib = [k for k, v in v8_importance.items() if v < 20]
    print(f"\n低贡献特征 (重要性<20): {low_contrib}")

    X = df_source[FEATURE_BASE].astype(float)
    corr = X.corr()
    high_corr: list[tuple[str, str, float]] = []
    for i in range(len(FEATURE_BASE)):
        for j in range(i + 1, len(FEATURE_BASE)):
            if abs(corr.iloc[i, j]) > 0.7:
                high_corr.append((FEATURE_BASE[i], FEATURE_BASE[j], float(corr.iloc[i, j])))

    print("\n[高相关性对 |r|>0.7]")
    for a, b, r in sorted(high_corr, key=lambda x: -abs(x[2])):
        print(f"  {a:18s} ~ {b:18s}  r={r:+.3f}")

    return v8_importance, high_corr, low_contrib


def _process_code_samples(args: tuple) -> list[dict]:
    """处理一个 code 的所有 sample (用于并行)。"""
    code, sig_dates, is_pos_flags = args
    df = load_one(code)
    if df.empty:
        return []

    rows = []
    for sig_date, is_pos in zip(sig_dates, is_pos_flags):
        try:
            sig_match = df.index[df["date"] == sig_date]
        except Exception:
            continue
        if len(sig_match) == 0:
            continue
        sig_i = int(sig_match[0])
        if sig_i < 5:
            continue
        ext = build_extended_features(df.iloc[:sig_i], code)
        if ext is None:
            continue
        ext["code"] = code
        ext["signal_date"] = sig_date
        ext["is_positive"] = is_pos
        rows.append(ext)
    return rows


def phase2_build_extended(df_source: pd.DataFrame, max_workers: int = 8) -> pd.DataFrame:
    """重建训练数据,加 8 维工程化特征(共 26 维)。"""
    print("\n=== Phase 2: 构建 26 维扩展训练数据 ===")
    t0 = datetime.now()

    grouped = df_source.groupby("code")
    tasks = []
    for code, group in grouped:
        sig_dates = [pd.Timestamp(d) for d in group["signal_date"].tolist()]
        is_pos = group["is_positive"].astype(int).tolist()
        tasks.append((code, sig_dates, is_pos))

    print(f"  共 {len(tasks)} 个 code, 总 {len(df_source)} 个 sample")

    all_rows: list[dict] = []
    if max_workers > 1 and len(tasks) > 50:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_process_code_samples, t): t[0] for t in tasks}
            done = 0
            for fut in as_completed(futs):
                done += 1
                rows = fut.result()
                all_rows.extend(rows)
                if done % 200 == 0 or done == len(tasks):
                    print(
                        f"    [{done}/{len(tasks)}] 有效: {len(all_rows)} 耗时: {(datetime.now()-t0).total_seconds():.0f}s"
                    )
    else:
        for i, t in enumerate(tasks, 1):
            rows = _process_code_samples(t)
            all_rows.extend(rows)
            if i % 200 == 0 or i == len(tasks):
                print(
                    f"    [{i}/{len(tasks)}] 有效: {len(all_rows)} 耗时: {(datetime.now()-t0).total_seconds():.0f}s"
                )

    df_v9 = pd.DataFrame(all_rows)
    df_v9.to_csv(TARGET, index=False)

    print(f"\n  ✅ {len(df_v9)} 样本 × {len(df_v9.columns)-3} 特征 → {TARGET.name}")
    print(f"  耗时: {(datetime.now()-t0).total_seconds():.0f}s")
    return df_v9


def phase3_train_cv(df_v9: pd.DataFrame, n_folds: int = 5) -> tuple[float, float, float, pd.Series, list]:
    """5-fold stratified CV 训练 v9,输出对比指标。"""
    print("\n=== Phase 3: 5-Fold CV 训练 v9 ===")
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold, train_test_split

    meta_cols = ["code", "signal_date", "is_positive"]
    feature_cols = [c for c in df_v9.columns if c not in meta_cols]
    X = df_v9[feature_cols].astype(float)
    y = df_v9["is_positive"].astype(int)

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)

    fold_aucs: list[float] = []
    fold_imp: list[pd.Series] = []

    for fold_i, (tr_idx, te_idx) in enumerate(skf.split(X, y), 1):
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
        auc = float(roc_auc_score(y_te, pred))
        fold_aucs.append(auc)
        fold_imp.append(pd.Series(model.feature_importances_, index=feature_cols))
        print(f"  Fold {fold_i}: AUC={auc:.4f}")

    mean_auc = float(np.mean(fold_aucs))
    std_auc = float(np.std(fold_aucs))
    print(f"\n  CV 平均 AUC: {mean_auc:.4f} ± {std_auc:.4f}")

    print("\n  训练最终模型 (80/20 split)...")
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    model_final = lgb.LGBMClassifier(
        n_estimators=500, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model_final.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])
    pred = model_final.predict_proba(X_te)[:, 1]
    test_auc = float(roc_auc_score(y_te, pred))
    print(f"  Test AUC (final): {test_auc:.4f}")

    avg_imp = sum(fold_imp) / len(fold_imp)
    avg_imp = avg_imp.sort_values(ascending=False)
    print("\n  Top 20 特征重要性 (跨 fold 平均):")
    new_names = {n for n, _ in NEW_FEATURES}
    for i, (k, v) in enumerate(avg_imp.head(20).items(), 1):
        flag = " [NEW]" if k in new_names else ""
        print(f"    {i:2d}. {k:20s} {v:.0f}{flag}")

    model_final.booster_.save_model(str(MODEL_OUT))
    print(f"\n  ✅ 模型保存: {MODEL_OUT}")

    return mean_auc, std_auc, test_auc, avg_imp, feature_cols


def phase4_report(
    v8_importance: dict,
    high_corr: list,
    low_contrib: list,
    df_v9: pd.DataFrame,
    mean_auc: float,
    std_auc: float,
    test_auc: float,
    avg_imp: pd.Series,
    feature_cols: list,
) -> Path:
    """输出综合报告 (Markdown)。"""
    print("\n=== Phase 4: 输出报告 ===")
    new_names = {n for n, _ in NEW_FEATURES}

    report_path = ROOT / "data" / "double_up" / f"feature_engineering_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# 18 维特征分析与扩展报告\n\n")
        f.write(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n\n")

        f.write("## 1. 现有 18 维特征分析\n\n")
        f.write("### 重要性 (v8 模型,12884 样本)\n\n")
        f.write("| 排名 | 特征 | 重要性 | 标记 |\n|---|---:|---:|---|\n")
        for i, (k, v) in enumerate(sorted(v8_importance.items(), key=lambda x: -x[1]), 1):
            tag = "**低贡献**" if v < 20 else ""
            f.write(f"| {i} | `{k}` | {v} | {tag} |\n")
        f.write(f"\n**低贡献特征 (重要性<20)**: {', '.join(low_contrib)}\n\n")

        f.write("### 高相关性对 (|r|>0.7)\n\n")
        if high_corr:
            f.write("| 特征 A | 特征 B | 相关系数 |\n|---|---|---:|\n")
            for a, b, r in sorted(high_corr, key=lambda x: -abs(x[2])):
                f.write(f"| `{a}` | `{b}` | {r:+.3f} |\n")
        else:
            f.write("(无 r>0.7 的对)\n")

        f.write("\n## 2. 8 维工程化新特征\n\n")
        for name, desc in NEW_FEATURES:
            f.write(f"- **`{name}`**: {desc}\n")

        f.write("\n## 3. v9 模型 (26 维) 训练结果\n\n")
        f.write(f"- 样本数: **{len(df_v9)}**\n")
        f.write(f"- 特征数: **{len(feature_cols)}** (18 基础 + 8 新)\n")
        f.write(f"- 5-fold CV AUC: **{mean_auc:.4f} ± {std_auc:.4f}**\n")
        f.write(f"- Test AUC (final): **{test_auc:.4f}**\n\n")

        f.write("### 特征重要性 Top 20 (跨 fold 平均)\n\n")
        f.write("| 排名 | 特征 | 重要性 | 标记 |\n|---|---:|---:|---|\n")
        for i, (k, v) in enumerate(avg_imp.head(20).items(), 1):
            tag = "**NEW**" if k in new_names else ""
            f.write(f"| {i} | `{k}` | {v:.0f} | {tag} |\n")

        f.write("\n## 4. 对比 v8 vs v9\n\n")
        f.write("| 指标 | v8 | v9 | 变化 |\n|---|---|---|---|\n")
        f.write(f"| 特征数 | 18 | {len(feature_cols)} | +{len(feature_cols)-18} |\n")
        f.write(f"| 样本 | 12884 | {len(df_v9)} | {len(df_v9)-12884:+d} |\n")
        f.write(f"| 5-fold CV AUC | — | {mean_auc:.4f} | — |\n")
        f.write(f"| Test AUC | 0.7934 | {test_auc:.4f} | {test_auc-0.7934:+.4f} |\n")

        f.write("\n## 5. 优化建议\n\n")
        if test_auc > 0.80:
            f.write("✅ **AUC 显著提升**(+0.005+),推荐 v9 上线。\n")
            f.write("   ```bash\n   mv data/double_up/d_signal_5d_model_v9.txt data/double_up/d_signal_5d_model.txt\n   ```\n\n")
        elif test_auc > 0.7934:
            f.write("⚠️ **AUC 略升**,建议跑 sim_live 验证实际胜率再决定。\n\n")
        elif test_auc > 0.79:
            f.write("⚠️ AUC 持平,新特征贡献有限,可保留为回退。\n\n")
        else:
            f.write("❌ **AUC 略降**,可能是过拟合或新特征噪声。建议保留 v8,分析新特征贡献。\n\n")

        new_top15 = [n for n in new_names if n in avg_imp.head(15).index.tolist()]
        new_weak = [n for n in new_names if n not in avg_imp.head(15).index.tolist()]
        f.write("### 新特征表现\n\n")
        f.write(f"- **进入 Top 15**: {new_top15}\n")
        f.write(f"- **较弱 (Top 15 外)**: {new_weak}\n")

        f.write("\n### 改进方向\n\n")
        f.write("- 若 v9 提升,跑 sim_live 验证真实胜率\n")
        f.write("- 若 v9 持平,考虑: 行业/题材特征、资金流、情绪指标\n")
        f.write("- 长期: 引入非线性组合 (XGBoost ensemble / LSTM / TabNet)\n")

    print(f"  ✅ 报告: {report_path}")
    return report_path


def main():
    print("=== 18 维特征分析与扩展 (v9 训练) ===\n")

    if not SOURCE.exists():
        sys.exit(f"❌ 源训练数据不存在: {SOURCE}\n先跑 find_d_signal_5d.py 生成 v8 数据")

    df_source = pd.read_csv(SOURCE)
    print(f"源训练数据: {len(df_source)} 样本 × {len(df_source.columns)} 列")

    v8_imp, high_corr, low_contrib = phase1_analyze(df_source)
    df_v9 = phase2_build_extended(df_source, max_workers=8)
    mean_auc, std_auc, test_auc, avg_imp, feature_cols = phase3_train_cv(df_v9)
    report_path = phase4_report(v8_imp, high_corr, low_contrib, df_v9, mean_auc, std_auc, test_auc, avg_imp, feature_cols)

    print("\n=== 完成 ===")
    print(f"  v9 模型: {MODEL_OUT}")
    print(f"  训练数据: {TARGET}")
    print(f"  报告: {report_path}")


if __name__ == "__main__":
    main()
