#!/usr/bin/env python3
"""
训练 baseline ML 模型：基于候选清单 + 28 个技术指标预测 20 日涨幅 ≥ 100%

数据:
  正样本: v5/audit/double_triggers_enriched.csv (39,979 triggers)
  负样本: 随机采同周期未触发窗口 (1:5)

特征 (28 个): 价/量/形态/估值
模型: LightGBM 二分类
评估: ROC-AUC + 特征重要性 + 模型持久化

输出:
  v5/audit/baseline_model.txt
  v5/audit/baseline_features.parquet
  v5/audit/baseline_metrics.json
  v5/audit/baseline_importance.csv
"""
import pandas as pd
import numpy as np
import glob, json, time
from pathlib import Path
import argparse
from sklearn.metrics import roc_auc_score, classification_report
import lightgbm as lgb

START_DATE = "2001-01-01"
TRIGGER_CSV = "v5/audit/double_triggers_enriched.csv"
NEG_RATIO = 5


def calc_features(df):
    """输入 OHLCV DataFrame; 输出 28 个特征"""
    f = pd.DataFrame(index=df.index)
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']

    for w in [5, 10, 20]:
        f[f'ma{w}'] = c.rolling(w).mean()
        f[f'mom{w}'] = c.pct_change(w)

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    f['rsi14'] = 100 - 100 / (1 + rs)

    ma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    f['bb_upper'] = ma20 + 2 * std20
    f['bb_lower'] = ma20 - 2 * std20
    f['bb_width'] = (f['bb_upper'] - f['bb_lower']) / (ma20 + 1e-9)
    f['bb_pos'] = (c - f['bb_lower']) / (f['bb_upper'] - f['bb_lower'] + 1e-9)

    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    f['atr14'] = tr.rolling(14).mean()
    f['atr_ratio'] = f['atr14'] / (c + 1e-9)

    for w in [5, 10, 20]:
        f[f'vol_ma{w}'] = v.rolling(w).mean()
    f['vol_ratio'] = v / (f['vol_ma20'] + 1e-9)
    f['vp_corr_10'] = v.pct_change().rolling(10).corr(c.pct_change())

    for w in [5, 10, 20]:
        f[f'amp{w}'] = (h.rolling(w).max() - l.rolling(w).min()) / (c.rolling(w).mean() + 1e-9)

    f['hl_ratio'] = h / (l + 1e-9)
    f['gap'] = (c - c.shift(1)) / (c.shift(1) + 1e-9)
    f['close_pct_20'] = (c - c.rolling(20).min()) / (c.rolling(20).max() - c.rolling(20).min() + 1e-9)

    return f


def load_daily(symbol):
    df = pd.read_parquet(f'data/parquet/daily/{symbol}.parquet')
    if df['date'].dtype != 'datetime64[ns]':
        s = df['date'].astype(str)
        mask = s.str.match(r'^\d{8}$')
        d1 = pd.to_datetime(s.where(mask), format='%Y%m%d', errors='coerce')
        d2 = pd.to_datetime(s.where(~mask), errors='coerce')
        df['date'] = d1.fillna(d2)
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    df = df[df['date'] >= START_DATE].reset_index(drop=True)
    df = df[df['low'] > 0].reset_index(drop=True)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-prefix', default='v5/audit/baseline')
    ap.add_argument('--max-stocks', type=int, default=None)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    np.random.seed(args.seed)

    triggers = pd.read_csv(TRIGGER_CSV)
    triggers['start_dt'] = pd.to_datetime(triggers['start_date'])
    triggers['end_dt'] = pd.to_datetime(triggers['end_date'])
    print(f"正样本 (triggers): {len(triggers)}")

    symbols = sorted(set(triggers['symbol']))
    if args.max_stocks:
        symbols = symbols[:args.max_stocks]
    print(f"扫描 {len(symbols)} 只股票")

    t0 = time.time()
    pos_features = []
    neg_features = []

    sym_to_path = {Path(f).stem: f for f in glob.glob('data/parquet/daily/*.parquet')}

    for i, sym in enumerate(symbols):
        if sym not in sym_to_path:
            continue
        try:
            df = load_daily(sym)
        except Exception:
            continue
        if len(df) < 30:
            continue
        feats_df = calc_features(df)
        feats_df['date'] = df['date'].values

        sym_triggers = triggers[triggers['symbol'] == sym]
        # 防泄漏: 用 start_date (区间起点), 不是 end_date
        sym_pos_dates = set()
        for _, t in sym_triggers.iterrows():
            start_dt = pd.Timestamp(t['start_date'])
            sym_pos_dates.add(start_dt.date())
            if start_dt not in feats_df['date'].values:
                continue
            row = feats_df[feats_df['date'] == start_dt].iloc[0].drop('date')
            row_dict = row.to_dict()
            row_dict['label'] = 1
            row_dict['symbol'] = sym
            row_dict['date'] = str(start_dt.date())
            pos_features.append(row_dict)

        # 负样本: 同一时间窗口但未触发的候选起点
        # 避免和 trigger 起点重叠 (允许未来 60 日内有 trigger, 但这里只采起点 60 日外)
        sym_future_trigger_dates = set()
        for _, t in sym_triggers.iterrows():
            s = pd.Timestamp(t['start_date'])
            sym_future_trigger_dates.add(s.date())
        n_neg_target = len(sym_triggers) * NEG_RATIO
        eligible_idx = list(range(30, len(df)))
        eligible_idx = [i for i in eligible_idx
                        if pd.Timestamp(df.iloc[i]['date']).date() not in sym_future_trigger_dates]
        if not eligible_idx:
            continue
        n_neg = min(n_neg_target, len(eligible_idx))
        neg_idx = np.random.choice(eligible_idx, size=n_neg, replace=False)
        for ni in neg_idx:
            end_dt = pd.Timestamp(df.iloc[ni]['date'])
            row = feats_df.iloc[ni].drop('date')
            row_dict = row.to_dict()
            row_dict['label'] = 0
            row_dict['symbol'] = sym
            row_dict['date'] = str(end_dt.date())
            neg_features.append(row_dict)

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(symbols)}] {time.time()-t0:.0f}s, pos={len(pos_features)}, neg={len(neg_features)}")

    print(f"\n正样本: {len(pos_features)}, 负样本: {len(neg_features)}")

    df_all = pd.DataFrame(pos_features + neg_features)
    df_all = df_all.dropna()
    print(f"去 NaN 后: {len(df_all)}")

    feature_cols = [c for c in df_all.columns if c not in ('label', 'symbol', 'date')]
    X = df_all[feature_cols].astype(float)
    y = df_all['label'].astype(int)

    df_all['date'] = pd.to_datetime(df_all['date'])
    # 按 symbol 做 stratified split: val 看到没见过的股票 (更接近真实预测场景)
    unique_syms = df_all['symbol'].unique()
    np.random.shuffle(unique_syms)
    n_val_syms = max(int(len(unique_syms) * 0.2), 1)
    val_syms = set(unique_syms[:n_val_syms])
    train_mask = ~df_all['symbol'].isin(val_syms)
    X_train, X_val = X[train_mask], X[~train_mask]
    y_train, y_val = y[train_mask], y[~train_mask]
    print(f"\ntrain: {len(X_train)}, val: {len(X_val)}")
    print(f"train symbols: {train_mask.sum() // max(1, train_mask.sum() // len(unique_syms))} (实际 {(~train_mask).sum()} stocks)")
    print(f"val symbols: {len(val_syms)} (实际 {(~train_mask).sum()} 样本)")
    print(f"train pos rate: {y_train.mean():.3%}, val pos rate: {y_val.mean():.3%}")

    print("\n[LightGBM] ...")
    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    params = {
        'objective': 'binary',
        'metric': 'auc',
        'boosting_type': 'gbdt',
        'learning_rate': 0.05,
        'num_leaves': 63,
        'max_depth': 7,
        'min_child_samples': 50,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'verbose': -1,
        'seed': args.seed,
    }
    model = lgb.train(
        params, train_data,
        num_boost_round=500,
        valid_sets=[train_data, val_data],
        valid_names=['train', 'val'],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)]
    )

    y_pred = model.predict(X_val)
    auc = roc_auc_score(y_val, y_pred)
    print(f"\n=== 评估 ===")
    print(f"ROC-AUC: {auc:.4f}")
    y_pred_bin = (y_pred >= 0.5).astype(int)
    print(classification_report(y_val, y_pred_bin, digits=4))

    importance = pd.DataFrame({
        'feature': feature_cols,
        'importance_gain': model.feature_importance(importance_type='gain'),
        'importance_split': model.feature_importance(importance_type='split'),
    }).sort_values('importance_gain', ascending=False)
    print(f"\n=== Top 15 特征 (gain) ===")
    print(importance.head(15).to_string())

    model.save_model(f'{args.out_prefix}_model.txt')
    df_all.to_parquet(f'{args.out_prefix}_features.parquet')
    metrics = {
        'auc': float(auc),
        'pos_count': int(y.sum()),
        'neg_count': int((1 - y).sum()),
        'n_features': len(feature_cols),
        'best_iter': int(model.best_iteration),
        'params': params,
        'timestamp': pd.Timestamp.now().isoformat(),
    }
    with open(f'{args.out_prefix}_metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2)
    importance.to_csv(f'{args.out_prefix}_importance.csv', index=False)

    print(f"\n✓ 输出:")
    print(f"  - {args.out_prefix}_model.txt")
    print(f"  - {args.out_prefix}_features.parquet")
    print(f"  - {args.out_prefix}_metrics.json")
    print(f"  - {args.out_prefix}_importance.csv")


if __name__ == "__main__":
    main()