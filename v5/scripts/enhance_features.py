#!/usr/bin/env python3
"""
T2: 特征增强 + 行业 target encoding

新特征 (在 baseline_features 基础上加):
  - industry_code: baostock 证监会行业 (C39/I65 等), 用 target-encoding 防 leakage
  - industry_mean_trigger: 同期同行业 trigger 频率 (target-encoding)
  - days_listed: 上市天数 (log)
  - log_amount: 区间 log(总成交额)
  - market_dummy: SH/SZ/BJ one-hot

训练用 baseline 模型架构，输出 AUC 增量评估。
"""
import argparse
import json

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ap = argparse.ArgumentParser()
ap.add_argument('--features', default='v5/audit/baseline_features.parquet')
ap.add_argument('--out-prefix', default='v5/audit/baseline_v2')
ap.add_argument('--seed', type=int, default=42)
args = ap.parse_args()

# 1. 加载
print("[1] 加载 features")
df = pd.read_parquet(args.features)
df['date'] = pd.to_datetime(df['date'])
print(f"  shape: {df.shape}")

# 2. 加载 baostock 行业
print("[2] 加载 baostock industry")
industry = pd.read_parquet('/tmp/baostock_industry.parquet')
# 把 sh.600000 → sh600000 (与 symbol 格式匹配)
industry['symbol'] = industry['code'].str.replace('.', '')

# 3. 加 industry_code (字符串 + 缺失标记)
df = df.merge(industry[['symbol', 'industry']], on='symbol', how='left')
df['industry_code'] = df['industry'].fillna('UNKNOWN')
print(f"  unique industry: {df['industry_code'].nunique()}")
print(f"  industry NaN: {(df['industry_code'] == 'UNKNOWN').sum()}")

# 4. 加载 basic (IPO)
basic = pd.read_parquet('/tmp/baostock_basic.parquet')
basic['symbol'] = basic['code'].str.replace('.', '')
basic['ipoDate'] = pd.to_datetime(basic['ipoDate'], errors='coerce')
df = df.merge(basic[['symbol', 'ipoDate']], on='symbol', how='left')
df['days_listed'] = (df['date'] - df['ipoDate']).dt.days.clip(lower=0)
df['log_days_listed'] = np.log1p(df['days_listed'].fillna(0))

# 5. 内部特征 (baseline features.parquet 没有 win_amount/avg_amount, 用 close 派生)
# 改用 amp/vol 作为代理
df['log_amp20'] = np.log1p(df['amp20'])

# 6. Market one-hot
df['market_dummy'] = df['symbol'].str[:2].map({'sh': 0, 'sz': 1, 'bj': 2}).fillna(3).astype(int)

# 7. Target encoding for industry (按 train 期, 防止 leakage)
unique_syms = df['symbol'].unique()
np.random.seed(args.seed)
np.random.shuffle(unique_syms)
n_val = max(int(len(unique_syms) * 0.2), 1)
val_syms = set(unique_syms[:n_val])
train_mask = ~df['symbol'].isin(val_syms)

# train 期按 industry 算 label mean, 加 smoothing
train = df[train_mask]
ind_stats = train.groupby('industry_code')['label'].agg(['mean', 'count']).reset_index()
GLOBAL_MEAN = train['label'].mean()
SMOOTH = 20
ind_stats['industry_mean'] = (ind_stats['mean'] * ind_stats['count'] + GLOBAL_MEAN * SMOOTH) / (ind_stats['count'] + SMOOTH)
df = df.merge(ind_stats[['industry_code', 'industry_mean']], on='industry_code', how='left')
df['industry_mean_trigger'] = df['industry_mean'].fillna(GLOBAL_MEAN)
df = df.drop(columns=['industry_mean'])

# 8. 准备训练集
new_features = ['industry_code_id', 'log_days_listed', 'log_amp20', 'market_dummy', 'industry_mean_trigger']
# 行业 code 编码为 categorical
df['industry_code_id'] = df['industry_code'].astype('category').cat.codes

base_features = [c for c in df.columns if c not in ('label', 'symbol', 'date', 'industry', 'industry_code', 'ipoDate', 'industry_mean_trigger', 'industry_code_id')]
feature_cols = [*base_features, 'industry_code_id', 'industry_mean_trigger', 'log_days_listed', 'log_amp20', 'market_dummy']
feature_cols = list(dict.fromkeys(feature_cols))  # 去重保持顺序

X = df[feature_cols].astype(float)
y = df['label'].astype(int)

# industry_code_id 是 categorical, 重新拼接
X['industry_code_id'] = df['industry_code_id'].astype('int32')

X_train, y_train = X[train_mask], y[train_mask]
X_val, y_val = X[~train_mask], y[~train_mask]
print(f"\ntrain: {len(X_train)} (pos rate {y_train.mean():.3%})")
print(f"val: {len(X_val)} (pos rate {y_val.mean():.3%})")
print(f"新特征: {new_features}")
print(f"总 features: {len(feature_cols)}")

# 9. 用 baseline 同架构 + industry categorical
print("\n[3] 训练")
train_data = lgb.Dataset(X_train, label=y_train, categorical_feature=['industry_code_id'])
val_data = lgb.Dataset(X_val, label=y_val, reference=train_data, categorical_feature=['industry_code_id'])
params = {
    'objective': 'binary', 'metric': 'auc', 'boosting_type': 'gbdt',
    'learning_rate': 0.05, 'num_leaves': 63, 'max_depth': 7,
    'min_child_samples': 50, 'feature_fraction': 0.8, 'bagging_fraction': 0.8,
    'bagging_freq': 5, 'verbose': -1, 'seed': args.seed,
}
model = lgb.train(
    params, train_data, num_boost_round=500,
    valid_sets=[train_data, val_data], valid_names=['train', 'val'],
    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)]
)

y_pred = model.predict(X_val)
auc = roc_auc_score(y_val, y_pred)
baseline_auc = 0.806
print("\n=== T2 评估 ===")
print(f"baseline AUC:    {baseline_auc:.4f}")
print(f"v2 (with industry+log features) AUC: {auc:.4f}")
print(f"delta:           {auc - baseline_auc:+.4f}")

# 输出
model.save_model(f'{args.out_prefix}_model.txt')
with open(f'{args.out_prefix}_metrics.json', 'w') as f:
    json.dump({
        'baseline_auc': baseline_auc,
        'v2_auc': float(auc),
        'delta': float(auc - baseline_auc),
        'new_features': new_features,
        'best_iter': int(model.best_iteration),
        'n_features': len(feature_cols),
        'timestamp': pd.Timestamp.now().isoformat(),
    }, f, indent=2)

# 特征重要性
importance = pd.DataFrame({
    'feature': feature_cols,
    'gain': model.feature_importance(importance_type='gain'),
    'split': model.feature_importance(importance_type='split'),
}).sort_values('gain', ascending=False)
importance.to_csv(f'{args.out_prefix}_importance.csv', index=False)
print("\nTop 15 特征 (含新):")
print(importance.head(15).to_string())
print(f"\n✓ {args.out_prefix}_model.txt + _metrics.json + _importance.csv")
