#!/usr/bin/env python3
"""
T1: 超参数调优（LightGBM + Optuna）

基于现有 features.parquet 跑 Optuna search，输出 tuned model + metrics。
"""
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.metrics import roc_auc_score
import optuna
import json, argparse, time

ap = argparse.ArgumentParser()
ap.add_argument('--features', default='v5/audit/baseline_features.parquet')
ap.add_argument('--out-prefix', default='v5/audit/baseline_tuned')
ap.add_argument('--n-trials', type=int, default=30)
ap.add_argument('--seed', type=int, default=42)
ap.add_argument('--timeout', type=int, default=1500, help='秒')
args = ap.parse_args()

print(f"加载 {args.features}")
df = pd.read_parquet(args.features)
df['date'] = pd.to_datetime(df['date'])

unique_syms = df['symbol'].unique()
np.random.seed(args.seed)
np.random.shuffle(unique_syms)
n_val = max(int(len(unique_syms) * 0.2), 1)
val_syms = set(unique_syms[:n_val])
train_mask = ~df['symbol'].isin(val_syms)

feature_cols = [c for c in df.columns if c not in ('label', 'symbol', 'date')]
X = df[feature_cols].astype(float)
y = df['label'].astype(int)
X_train, y_train = X[train_mask], y[train_mask]
X_val, y_val = X[~train_mask], y[~train_mask]
print(f"train: {len(X_train)} (pos rate {y_train.mean():.3%})")
print(f"val: {len(X_val)} (pos rate {y_val.mean():.3%})")


def objective(trial):
    params = {
        'objective': 'binary', 'metric': 'auc', 'boosting_type': 'gbdt',
        'verbose': -1, 'seed': args.seed,
        'learning_rate': trial.suggest_float('lr', 0.01, 0.1, log=True),
        'num_leaves': trial.suggest_int('num_leaves', 31, 127),
        'max_depth': trial.suggest_int('max_depth', 5, 9),
        'min_child_samples': trial.suggest_int('min_child_samples', 20, 200),
        'feature_fraction': trial.suggest_float('feature_fraction', 0.5, 1.0),
        'bagging_fraction': trial.suggest_float('bagging_fraction', 0.5, 1.0),
        'bagging_freq': trial.suggest_int('bagging_freq', 1, 10),
        'min_split_gain': trial.suggest_float('min_split_gain', 0.0, 0.1),
        'reg_alpha': trial.suggest_float('reg_alpha', 1e-8, 1.0, log=True),
        'reg_lambda': trial.suggest_float('reg_lambda', 1e-8, 1.0, log=True),
    }
    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
    model = lgb.train(
        params, train_data, num_boost_round=300,
        valid_sets=[val_data], valid_names=['val'],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)]
    )
    return model.best_score['val']['auc']


print(f"\n[Optuna] {args.n_trials} trials, timeout {args.timeout}s")
t0 = time.time()
optuna.logging.set_verbosity(optuna.logging.WARNING)
study = optuna.create_study(direction='maximize')
study.optimize(objective, n_trials=args.n_trials, timeout=args.timeout, show_progress_bar=False)
print(f"Optuna 完成 {len(study.trials)} trials, 耗时 {time.time()-t0:.0f}s")
print(f"  best AUC = {study.best_value:.4f}")
print(f"  best params = {study.best_params}")

print(f"\n[Final] 用最优参数重训...")
final_params = {
    'objective': 'binary', 'metric': 'auc', 'boosting_type': 'gbdt',
    'verbose': -1, 'seed': args.seed, **study.best_params,
}
train_data = lgb.Dataset(X_train, label=y_train)
val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
model = lgb.train(
    final_params, train_data, num_boost_round=500,
    valid_sets=[train_data, val_data], valid_names=['train', 'val'],
    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)]
)

y_pred = model.predict(X_val)
final_auc = roc_auc_score(y_val, y_pred)
print(f"\n=== Final ===")
print(f"baseline AUC: 0.806")
print(f"tuned AUC:    {final_auc:.4f}")
print(f"delta:        {final_auc - 0.806:+.4f}")

model.save_model(f'{args.out_prefix}_model.txt')
with open(f'{args.out_prefix}_metrics.json', 'w') as f:
    json.dump({
        'baseline_auc': 0.806,
        'tuned_auc': float(final_auc),
        'delta': float(final_auc - 0.806),
        'best_params': study.best_params,
        'best_iter': int(model.best_iteration),
        'n_trials': len(study.trials),
        'elapsed_s': round(time.time() - t0, 1),
        'feature_cols': feature_cols,
    }, f, indent=2)
print(f"\n✓ {args.out_prefix}_model.txt + {args.out_prefix}_metrics.json")