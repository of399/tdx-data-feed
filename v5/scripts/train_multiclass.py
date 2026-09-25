#!/usr/bin/env python3
"""
T5: 多分类 (涨幅分级 1x / 1.5x / 2x / 3x / 5x+)

将 trigger 区间 max(high)/min(low) - 1 分级:
  0: 未触发 (< 1.0)
  1: 1.0-1.5x
  2: 1.5-2.0x
  3: 2.0-3.0x
  4: 3.0-5.0x
  5: 5.0x+

需要先跑 scan_double.py 重新生成 raw (含 win_amount 等), 再 enrich 取 max(high)/min(low)
"""
import argparse
import json
import time

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, f1_score, roc_auc_score

ap = argparse.ArgumentParser()
ap.add_argument('--features', default='v5/audit/baseline_features.parquet')
ap.add_argument('--triggers', default='v5/audit/double_triggers_enriched.csv')
ap.add_argument('--out-prefix', default='v5/audit/baseline_multiclass')
ap.add_argument('--seed', type=int, default=42)
args = ap.parse_args()

# 1. 加载 features (基础 24 features)
df = pd.read_parquet(args.features)
df['date'] = pd.to_datetime(df['date'])
print(f"features: {df.shape}")

# 2. 计算每只股票每日的"未来 20 日最大涨幅" (label)
# 这是 ground truth 的 multiclass label
print("\n[2] 计算未来 20 日 max(high)/min(low) ...")
def calc_forward_look(df_daily, sym):
    """对每只股票计算每日的 forward_max_gain_20"""
    f = f'data/parquet/daily/{sym}.parquet'
    try:
        d = pd.read_parquet(f)
    except Exception:
        return None
    if d['date'].dtype != 'datetime64[ns]':
        s = d['date'].astype(str)
        mask = s.str.match(r'^\d{8}$')
        d1 = pd.to_datetime(s.where(mask), format='%Y%m%d', errors='coerce')
        d2 = pd.to_datetime(s.where(~mask), errors='coerce')
        d['date'] = d1.fillna(d2)
    d = d.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    d = d[d['low'] > 0]
    # 滚动 20 日 max(high) / min(low)
    d['fwd_max_high'] = d['high'].rolling(20, min_periods=20).max()
    d['fwd_min_low'] = d['low'].rolling(20, min_periods=20).min()
    d['fwd_range'] = d['fwd_max_high'] / d['fwd_min_low'] - 1
    return d[['date', 'fwd_range']]

# 按 symbol 计算, merge 回 df
symbols = df['symbol'].unique()
print(f"  symbols: {len(symbols)}")
forward_dfs = {}
t0 = time.time()
for i, sym in enumerate(symbols):
    fdf = calc_forward_look(df, sym)
    if fdf is not None:
        forward_dfs[sym] = fdf
    if (i + 1) % 500 == 0:
        print(f"    [{i+1}/{len(symbols)}] {time.time()-t0:.0f}s")

# merge
df = df.merge(
    pd.concat([d.assign(symbol=s) for s, d in forward_dfs.items()]),
    on=['symbol', 'date'], how='left'
)
df['fwd_range'] = df['fwd_range'].fillna(0)

# 3. 分级
def to_class(r):
    if r < 1.0:
        return 0
    if r < 1.5:
        return 1
    if r < 2.0:
        return 2
    if r < 3.0:
        return 3
    if r < 5.0:
        return 4
    return 5

df['class'] = df['fwd_range'].apply(to_class)
print("\n分级分布:")
print(df['class'].value_counts().sort_index())
print(f"  NaN/inf: {df['fwd_range'].isna().sum() + (df['fwd_range'] == np.inf).sum()}")

# 4. 训练 multiclass LightGBM
print("\n[3] 训练 multiclass ...")
unique_syms = df['symbol'].unique()
np.random.seed(args.seed)
np.random.shuffle(unique_syms)
n_val = max(int(len(unique_syms) * 0.2), 1)
val_syms = set(unique_syms[:n_val])
train_mask = ~df['symbol'].isin(val_syms)

feature_cols = [c for c in df.columns if c not in ('label', 'symbol', 'date', 'fwd_range', 'class')]
X = df[feature_cols].astype(float)
y_class = df['class'].astype(int)

# 类别权重 (5x+ 极少)
counts = y_class.value_counts().sort_index().to_dict()
total = len(y_class)
n_classes = len(counts)
class_weight = {c: total / (n_classes * cnt) for c, cnt in counts.items()}
print(f"  class weights: {class_weight}")

X_train, y_train = X[train_mask], y_class[train_mask]
X_val, y_val = X[~train_mask], y_class[~train_mask]

train_data = lgb.Dataset(X_train, label=y_train, weight=y_train.map(class_weight))
val_data = lgb.Dataset(X_val, label=y_val, reference=train_data, weight=y_val.map(class_weight))
params = {
    'objective': 'multiclass', 'num_class': n_classes, 'metric': 'multi_logloss',
    'boosting_type': 'gbdt', 'learning_rate': 0.05, 'num_leaves': 63, 'max_depth': 7,
    'min_child_samples': 50, 'feature_fraction': 0.8, 'bagging_fraction': 0.8,
    'bagging_freq': 5, 'verbose': -1, 'seed': args.seed,
}
model = lgb.train(
    params, train_data, num_boost_round=300,
    valid_sets=[val_data], valid_names=['val'],
    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)]
)

# 5. 评估
y_pred_proba = model.predict(X_val)
y_pred_class = y_pred_proba.argmax(axis=1)
print("\n=== T5 评估 ===")
print(classification_report(y_val, y_pred_class, digits=3))

# Macro F1
macro_f1 = f1_score(y_val, y_pred_class, average='macro')
weighted_f1 = f1_score(y_val, y_pred_class, average='weighted')

# 二分类对比: class>=1 vs class<1 (相当于"触发 vs 未触发")
y_val_bin = (y_val >= 1).astype(int)
y_pred_bin = (y_pred_class >= 1).astype(int)
bin_auc = roc_auc_score(y_val_bin, y_pred_proba[:, 1:].sum(axis=1))
print(f"\nBin AUC (class>=1): {bin_auc:.4f} (baseline 0.806)")

# 输出
model.save_model(f'{args.out_prefix}_model.txt')
with open(f'{args.out_prefix}_metrics.json', 'w') as f:
    json.dump({
        'macro_f1': float(macro_f1),
        'weighted_f1': float(weighted_f1),
        'bin_auc_vs_baseline': float(bin_auc),
        'baseline_auc': 0.806,
        'class_distribution': counts,
        'best_iter': int(model.best_iteration),
    }, f, indent=2)
print(f"\n✓ {args.out_prefix}_model.txt + _metrics.json")
