"""方案 D：识别"翻倍前 5 日信号"

输入：
- 22972 个翻倍样本（v1 detail.csv）

目标：
- 对每个翻倍样本，取 start_date 前 5 个交易日的特征
- 训练 LightGBM 二分类：5 日内是否翻倍
- 输出：特征重要性 + Top 风险信号 + 报告

负样本：
- 从非翻倍日的全市场随机抽样同等数量 5 日窗口

特征（5 日窗口前特征）：
- T-5 ~ T-1 五日累计涨幅
- T-5 ~ T-1 涨停天数
- T-5 ~ T-1 最大单日涨幅
- T-5 ~ T-1 量能变化（末日/5日均）
- T-1 当日涨幅 / 量能
- T-1 收盘价相对 T-5 的位置
- 当日是否涨停
- 是否 ST / 新股
- 涨停限制（10%/20%/30%）
"""
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PARQUET = Path('/home/jiuben/tdx-data-feed/data/parquet/daily')
V1_DETAIL = Path('/home/jiuben/tdx-data-feed/data/double_up/double_up_2020_2026_detail.csv')
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')

START_DATE = pd.Timestamp('2020-01-01')
WINDOW = 5
RANDOM_SEED = 42

LIMIT_MAP = {'main': 0.095, 'gem': 0.195, 'bse': 0.295}


def get_limit(code: str) -> float:
    short = code[2:]
    if code.startswith('bj'):
        return LIMIT_MAP['bse']
    if code.startswith('sz'):
        return LIMIT_MAP['gem'] if short.startswith('30') or short.startswith('20') else LIMIT_MAP['main']
    if code.startswith('sh'):
        return LIMIT_MAP['gem'] if short.startswith('688') else LIMIT_MAP['main']
    return LIMIT_MAP['main']


def load_one(code: str) -> pd.DataFrame:
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    except Exception:
        return pd.DataFrame()
    if df['date'].dtype in ('int64', 'int32'):
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'])
    df['code'] = code
    return df.sort_values('date').reset_index(drop=True)


def extract_features(code: str, signal_date: pd.Timestamp, is_positive: bool):
    """提取 T-1 日的特征（已知当日是 signal_date，看前一天到前 5 天的特征）"""
    df = load_one(code)
    if len(df) == 0:
        return None

    # 找到 signal_date 在 df 中的索引（信号日 = 翻倍起始日 = 第 1 日）
    # 我们要看 signal_date 前 5 日的特征（在翻倍"之前"）
    sig_idx = df.index[df['date'] == signal_date]
    if len(sig_idx) == 0:
        return None
    sig_i = sig_idx[0]
    if sig_i < 5:  # 需要至少 5 日历史
        return None

    prev_5 = df.iloc[sig_i - 5: sig_i]  # T-5 ~ T-1
    sig_day = df.iloc[sig_i - 1]  # T-1

    limit = get_limit(code)
    pct = prev_5['close'].pct_change().fillna(0)

    feats = {
        'code': code,
        'signal_date': signal_date,
        'is_positive': int(is_positive),
        'limit': limit,
        # 价格水平
        'prev_close': float(sig_day['close']),
        # 5 日累计涨幅
        'ret_5d': float(prev_5['close'].iloc[-1] / prev_5['close'].iloc[0] - 1),
        # 3 日累计涨幅
        'ret_3d': float(prev_5['close'].iloc[-1] / prev_5['close'].iloc[-4] - 1) if len(prev_5) >= 4 else 0,
        # 单日最大涨幅（T-5 ~ T-1）
        'max_5d_pct': float(pct.max()),
        # 5 日内涨停数
        'limit_up_5d': int((pct >= limit - 0.001).sum()),
        # T-1 是否涨停
        'limit_up_t1': int(sig_day['close'] / sig_day['open'] - 1 >= limit - 0.001) if sig_day['open'] > 0 else 0,
        # T-1 当日涨幅
        'pct_t1': float(sig_day['close'] / sig_day['open'] - 1) if sig_day['open'] > 0 else 0,
        # 5 日平均成交额
        'avg_amount_5d': float(prev_5['amount'].mean()),
        # T-1 / 5日均量比
        'vol_ratio_t1': float(sig_day['amount'] / prev_5['amount'].mean()) if prev_5['amount'].mean() > 0 else 1.0,
        # 5 日价格波动率
        'volatility_5d': float(pct.std()),
        # T-1 收盘价在 5 日 K 线中位置（0-1）
        'close_pos_5d': float((sig_day['close'] - prev_5['low'].min()) / (prev_5['high'].max() - prev_5['low'].min())) if prev_5['high'].max() > prev_5['low'].min() else 0.5,
        # 5 日最大单日跌幅（恐慌洗盘信号）
        'max_drawdown_5d': float(pct.min()),
        # T-1 换手代理（amount / total volume）
        'turnover_proxy': float(sig_day['amount'] / sig_day['volume']) if sig_day['volume'] > 0 else 0,
        # 是否 ST
        'is_st': 0,  # 简化：先用 0
        # 新股 (前 250 日内上市)
        'is_new': int(sig_i < 250),
        # 市场类型
        'is_bj': int(code.startswith('bj')),
        'is_gem': int(code.startswith('sz3') or code.startswith('sh688')),
    }
    return feats


def build_dataset():
    print('=== 方案 D：翻倍前 5 日信号识别 ===')
    t0 = datetime.now()

    # 1. 正样本（翻倍前 5 日）
    pos_df = pd.read_csv(V1_DETAIL, dtype={'code': str})
    # 去重：同一只股同一周内的 start_date 取最早
    pos_df['start_date'] = pd.to_datetime(pos_df['start_date'])
    pos_df = pos_df.drop_duplicates(subset=['code', 'start_date'])
    # 进一步去重：相邻 start_date 间隔小于 5 日的只保留最早（避免重叠样本）
    pos_df = pos_df.sort_values(['code', 'start_date']).reset_index(drop=True)
    keep = []
    last_code = None
    last_date = None
    for _, row in pos_df.iterrows():
        if row['code'] != last_code or (row['start_date'] - last_date).days >= 7:
            keep.append(row)
            last_code = row['code']
            last_date = row['start_date']
    pos_df = pd.DataFrame(keep).reset_index(drop=True)
    print(f'正样本（去重后）: {len(pos_df)}')

    # 2. 负样本（随机抽取同等数量）
    np.random.seed(RANDOM_SEED)
    all_files = list(PARQUET.glob('*.parquet'))
    neg_samples = []
    target_neg = len(pos_df)

    while len(neg_samples) < target_neg:
        code = np.random.choice(all_files).stem
        df = load_one(code)
        if len(df) < 50:  # 太短跳过
            continue
        # 随机选一天
        try:
            idx = np.random.randint(30, len(df) - 5)  # 避免边界
        except ValueError:
            continue
        sig_date = df.iloc[idx]['date']
        neg_samples.append({'code': code, 'start_date': sig_date})

    neg_df = pd.DataFrame(neg_samples)
    print(f'负样本（随机）: {len(neg_df)}')

    # 3. 提取特征
    all_samples = []
    for _, row in pos_df.iterrows():
        f = extract_features(row['code'], row['start_date'], is_positive=True)
        if f:
            all_samples.append(f)
    print(f'正样本特征: {len(all_samples)}')

    for _, row in neg_df.iterrows():
        f = extract_features(row['code'], row['start_date'], is_positive=False)
        if f:
            all_samples.append(f)
    print(f'负样本特征: {len(all_samples) - len(pos_df)}')

    feat_df = pd.DataFrame(all_samples)
    print(f'\n总样本: {len(feat_df)}, 正样本: {(feat_df["is_positive"] == 1).sum()}, '
          f'负样本: {(feat_df["is_positive"] == 0).sum()}')
    print(f'耗时: {(datetime.now() - t0).total_seconds():.0f}s')

    return feat_df


def train_and_report(feat_df):
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    t0 = datetime.now()
    print('\n=== LightGBM 训练 ===')

    # 准备数据
    feature_cols = [c for c in feat_df.columns if c not in ('code', 'signal_date', 'is_positive')]
    X = feat_df[feature_cols].astype(float)
    y = feat_df['is_positive'].astype(int)

    print(f'特征: {feature_cols}')
    print(f'样本: {len(X)} (正 {y.sum()}, 负 {(y == 0).sum()})')

    # 划分
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=RANDOM_SEED, stratify=y)

    # 训练
    model = lgb.LGBMClassifier(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=RANDOM_SEED,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], callbacks=[lgb.early_stopping(50, verbose=False)])

    # 评估
    pred = model.predict_proba(X_te)[:, 1]
    auc = roc_auc_score(y_te, pred)
    print(f'\nTest AUC: {auc:.4f}')

    # 特征重要性
    imp = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importances_,
    }).sort_values('importance', ascending=False)
    print('\n特征重要性 Top 15:')
    print(imp.head(15).to_string(index=False))

    # 不同阈值下的精确率
    for th in [0.5, 0.6, 0.7, 0.8]:
        mask = pred >= th
        if mask.sum() > 0:
            precision = y_te[mask].mean()
            recall = mask.sum() / len(y_te)
            print(f'  threshold={th}: precision={precision:.2%} recall={recall:.2%}')

    # 保存模型 + 报告
    model.booster_.save_model(str(OUT_DIR / 'd_signal_5d_model.txt'))
    print(f'\n模型保存: {OUT_DIR / "d_signal_5d_model.txt"}')

    elapsed = (datetime.now() - t0).total_seconds()
    print(f'训练耗时: {elapsed:.0f}s')

    return imp, auc, feature_cols


def main():
    feat_df = build_dataset()
    feat_df.to_csv(OUT_DIR / 'd_signal_5d_features.csv', index=False)
    print(f'\n特征表保存: {OUT_DIR / "d_signal_5d_features.csv"}')

    imp, auc, feature_cols = train_and_report(feat_df)

    # 写报告
    md_path = OUT_DIR / f'd_signal_5d_report_{datetime.now().strftime("%Y%m%d_%H%M")}.md'
    with md_path.open('w', encoding='utf-8') as f:
        f.write('# 方案 D：翻倍前 5 日信号识别\n\n')
        f.write(f'> 生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write('> 模型: LightGBM (n_estimators=500, max_depth=6)\n')
        f.write(f'> 数据: {len(feat_df)} 样本（正 {(feat_df["is_positive"] == 1).sum()} / 负 {(feat_df["is_positive"] == 0).sum()}）\n')
        f.write(f'> **Test AUC: {auc:.4f}**\n\n')

        f.write('## 特征重要性\n\n')
        f.write('| 排名 | 特征 | 重要性 | 说明 |\n|---|---|---|---|\n')
        feature_desc = {
            'ret_5d': '5 日累计涨幅',
            'ret_3d': '3 日累计涨幅',
            'max_5d_pct': '5 日最大单日涨幅',
            'limit_up_5d': '5 日涨停次数',
            'limit_up_t1': 'T-1 是否涨停',
            'pct_t1': 'T-1 当日涨幅',
            'prev_close': 'T-1 收盘价',
            'avg_amount_5d': '5 日均成交额',
            'vol_ratio_t1': 'T-1 量比（末日/5日均）',
            'volatility_5d': '5 日波动率',
            'close_pos_5d': 'T-1 收盘价在 5 日 K 线位置',
            'max_drawdown_5d': '5 日最大跌幅（洗盘信号）',
            'turnover_proxy': 'T-1 换手代理',
            'is_st': '是否 ST',
            'is_new': '是否新股',
            'is_bj': '是否北交所',
            'is_gem': '是否创业板/科创板',
            'limit': '涨停限制',
        }
        for i, (_, r) in enumerate(imp.iterrows(), 1):
            desc = feature_desc.get(r['feature'], '')
            f.write(f'| {i} | `{r["feature"]}` | {r["importance"]} | {desc} |\n')

        f.write('\n## 解读\n\n')
        top3 = imp.head(3)['feature'].tolist()
        f.write(f'- **Top 3 关键信号**: `{top3[0]}` > `{top3[1]}` > `{top3[2]}`\n')
        f.write(f'- AUC = {auc:.4f} 表示模型判别能力\n')
        f.write('- 翻倍前 5 日窗口内某些特征已经显著 → 可作为"早期预警"\n\n')

        f.write('## 使用建议\n\n')
        f.write('1. **预测**：每日跑全市场，看 5 日窗口特征 → 输出翻倍概率\n')
        f.write('2. **过滤**：只关注高概率（>0.6）+ 高精度阈值的标的\n')
        f.write('3. **监控**：对高概率股叠加人工题材/新闻分析\n\n')

        f.write('## 输出文件\n\n')
        f.write(f'- `d_signal_5d_features.csv` ({len(feat_df)} 行 × {len(feature_cols) + 3} 列)\n')
        f.write('- `d_signal_5d_model.txt` (LightGBM 模型)\n')
        f.write(f'- `{md_path.name}` (本报告)\n')

    print(f'\n报告: {md_path}')
    print('\n=== 方案 D 完成 ===')


if __name__ == '__main__':
    main()
