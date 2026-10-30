"""
方案 E v3 提前版回测
======================

相对 v2 的"翻倍当日跟随"，v3 提前版：**翻倍事件发生前 N 日识别特征，预测翻倍**

输入:
- data/factors/event_factors.parquet: 已计算好的事件因子
- data/factor_significance_detail.csv: 因子显著性排序

策略:
1. 用 factor_significance_report.md 中的 Top N 因子合成 composite_score
2. 按 composite_score 分层：
   - Top 10% → 强信号候选
   - 其余 → 不入场
3. 在验证期 2025-01 ~ 2026-09 上计算胜率、期望收益

样本切分:
- 训练期：2001-01 ~ 2024-12
- 验证期：2025-01 ~ 2026-09（21 个月）

对比基准:
- 方案 E v2（翻倍当日跟随）
- 随机抽样（期望 50%）

输出:
- data/plan_e/v3_early_backtest_report.md
- data/plan_e/v3_early_test_candidates_*.csv
"""
import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')
FACTOR_FP = ROOT / 'data/factors/event_factors_with_sentiment.parquet'
SIGNIF_FP = ROOT / 'data/factor_significance_detail.csv'
OUT_DIR = ROOT / 'data/plan_e'

# 从 factor_significance_detail.csv 自动读取 Top N（保证 v3 用最强因子）
def load_top_factors(n=10):
    if not SIGNIF_FP.exists():
        return []
    df = pd.read_csv(SIGNIF_FP)
    df['abs_ric'] = df['rank_ic'].abs()
    df = df.sort_values('abs_ric', ascending=False).head(n)
    return df['factor'].tolist()

TOP_FACTORS = load_top_factors(10)  # 自动取 Top 10


def calc_composite_score(df: pd.DataFrame, factor_weights: dict) -> pd.Series:
    """等权 + Rank 标准化合成"""
    rank_sum = pd.Series(0.0, index=df.index)
    total_w = sum(factor_weights.values())
    for fc, w in factor_weights.items():
        if fc not in df.columns:
            continue
        valid = df[fc].rank(pct=True, na_option='bottom')
        rank_sum += valid.fillna(0.5) * w
    return rank_sum / total_w


def run_backtest(df: pd.DataFrame, train_start: str, train_end: str,
                 test_start: str, test_end: str, top_pct: float = 0.10) -> dict:
    """训练 + 验证期回测"""
    df['signal_date'] = pd.to_datetime(df['signal_date'])

    train_mask = (df['signal_date'] >= train_start) & (df['signal_date'] < train_end)
    test_mask = (df['signal_date'] >= test_start) & (df['signal_date'] < test_end)

    df_train = df[train_mask].copy()
    df_test = df[test_mask].copy()

    print(f'训练期: {len(df_train):,} 行 ({train_start} ~ {train_end})')
    print(f'  正样本率: {df_train["label"].mean()*100:.2f}%')
    print(f'验证期: {len(df_test):,} 行 ({test_start} ~ {test_end})')
    print(f'  正样本率: {df_test["label"].mean()*100:.2f}%')

    weights = {fc: 1.0 for fc in TOP_FACTORS}
    df_train['score'] = calc_composite_score(df_train, weights)
    df_test['score'] = calc_composite_score(df_test, weights)

    score_threshold = df_train['score'].quantile(1 - top_pct)

    train_top = df_train[df_train['score'] >= score_threshold]
    train_win_rate = train_top['label'].mean() if len(train_top) > 0 else 0
    train_top_n = len(train_top)

    test_top = df_test[df_test['score'] >= score_threshold]
    test_win_rate = test_top['label'].mean() if len(test_top) > 0 else 0
    test_top_n = len(test_top)

    test_top_pos = test_top[test_top['label'] == 1]
    test_top_neg = test_top[test_top['label'] == 0]

    pos_returns = test_top_pos['pct_change_20d_actual'].dropna()
    expected_pos_return = pos_returns.median() if len(pos_returns) > 0 else 100
    expected_neg_return = -5

    if test_top_n > 0:
        expected_return = (
            test_win_rate * expected_pos_return +
            (1 - test_win_rate) * expected_neg_return
        )
    else:
        expected_return = 0

    return {
        'train_top_n': train_top_n,
        'train_win_rate': train_win_rate,
        'test_top_n': test_top_n,
        'test_win_rate': test_win_rate,
        'score_threshold': score_threshold,
        'expected_pos_return': expected_pos_return,
        'expected_neg_return': expected_neg_return,
        'expected_return': expected_return,
        'test_top_pos': test_top_pos,
        'test_top_neg': test_top_neg,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-start', default='2001-01-01')
    parser.add_argument('--train-end', default='2025-01-01')
    parser.add_argument('--test-start', default='2025-01-01')
    parser.add_argument('--test-end', default='2026-09-30')
    parser.add_argument('--top-pct', type=float, default=0.10)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f'[{datetime.now():%H:%M:%S}] 加载因子矩阵...')
    df = pd.read_parquet(FACTOR_FP)
    print(f'  总事件: {len(df):,}')
    print(f'  使用 Top {len(TOP_FACTORS)} 因子: {TOP_FACTORS}')

    result = run_backtest(df, args.train_start, args.train_end,
                          args.test_start, args.test_end, args.top_pct)

    md = []
    md.append('# 方案 E v3 提前版回测报告')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}  ')
    md.append(f'> 训练期: {args.train_start} ~ {args.train_end}  ')
    md.append(f'> 验证期: {args.test_start} ~ {args.test_end}（21 个月）  ')
    md.append(f'> 候选池: Top {args.top_pct*100:.0f}%  ')
    md.append('')
    md.append('## 一、策略框架')
    md.append('')
    md.append(f'### 1.1 Top 因子合成（{len(TOP_FACTORS)} 个，全市场实测）')
    md.append('')
    md.append('| # | 因子 | RankIC | 含义 |')
    md.append('|:---:|---|---:|---|')
    meanings = {
        'high_low_20d': '20 日振幅（波动幅度）',
        'high_low_5d': '5 日振幅',
        'volatility_20d': '20 日波动率',
        'limit_up_20d': '20 日内涨停数',
        'limit_up_10d': '10 日内涨停数',
        'limit_up_5d': '5 日内涨停数',
        'price_break_ma20': '收盘/20日均线-1',
        'price_break_ma10': '收盘/10日均线-1',
        'price_break_ma5': '收盘/5日均线-1',
        'amount_5d_avg': '5 日均成交额',
        'amount_20d_avg': '20 日均成交额',
        'ma5_ma20_diff': '5日均线/20日均线-1',
        'days_since_ipo': '上市天数（次新股）',
        'volume_ratio_5d_20d': '量比放大',
        'pct_chg_5d': '5 日涨幅',
        'pct_chg_10d': '10 日涨幅',
        'pct_chg_20d': '20 日涨幅',
        'continuous_limit': '连板数',
        'close_pos_in_20d_range': '20 日区间位置',
        'lhb_appear_5d': '近 5 日龙虎榜上榜次数',
        'lhb_appear_10d': '近 10 日龙虎榜上榜次数',
        'lhb_appear_20d': '近 20 日龙虎榜上榜次数',
        'lhb_net_amt_5d': '近 5 日龙虎榜净买入额',
        'lhb_net_amt_10d': '近 10 日龙虎榜净买入额',
        'lhb_net_amt_20d': '近 20 日龙虎榜净买入额',
        'lhb_buy_ratio_5d': '近 5 日买入额占比',
        'lhb_buy_ratio_10d': '近 10 日买入额占比',
        'lhb_buy_ratio_20d': '近 20 日买入额占比',
        'lhb_inst_net_5d': '近 5 日机构席位净买入',
        'lhb_inst_net_10d': '近 10 日机构席位净买入',
        'lhb_inst_net_20d': '近 20 日机构席位净买入',
    }
    # 从 detail.csv 读取 IC
    ic_map = {}
    if SIGNIF_FP.exists():
        sig_df = pd.read_csv(SIGNIF_FP)
        for _, row in sig_df.iterrows():
            ic_map[row['factor']] = row['rank_ic']
    for i, fc in enumerate(TOP_FACTORS, 1):
        ic = ic_map.get(fc, 0)
        md.append(f'| {i} | `{fc}` | +{ic:.3f} | {meanings.get(fc,"")} |')
    md.append('')
    md.append('**合成方式**：等权 + Rank 标准化')
    md.append('')
    md.append('**核心发现**（vs 小样本 30 只）：')
    md.append('- 小样本 Top 因子是 `days_since_ipo` / `close` / `amount_*`')
    md.append('- 全市场真 Top 因子是 **波动类**（`high_low_*` / `volatility_20d`）')
    md.append('- 揭示妖股本质特征：**高波动 + 高振幅**（不是单纯"小盘"）')
    md.append('')
    md.append('## 二、训练期结果')
    md.append('')
    md.append(f'- **训练期事件数**: {result["train_top_n"]:,}（Top {args.top_pct*100:.0f}%）')
    md.append(f'- **训练期胜率（正样本率）**: {result["train_win_rate"]*100:.2f}%')
    md.append('- **基准胜率（随机 1:1）**: 50.00%')
    md.append(f'- **提升**: +{(result["train_win_rate"]-0.5)*100:.2f}pp')
    md.append('')
    md.append('## 三、验证期结果（关键）')
    md.append('')
    md.append(f'- **验证期 Top {args.top_pct*100:.0f}% 候选数**: {result["test_top_n"]:,}')
    md.append(f'- **验证期胜率**: {result["test_win_rate"]*100:.2f}%')
    diff = (result['test_win_rate'] - result['train_win_rate']) * 100
    md.append(f'- **训练 vs 验证胜率差**: {diff:+.2f}pp')
    md.append('')
    md.append('## 四、收益预期（验证期）')
    md.append('')
    md.append(f'- **正样本实际 20 日涨幅中位**: {result["expected_pos_return"]:.1f}%')
    md.append(f'- **负样本预期损失（估算）**: {result["expected_neg_return"]:.1f}%')
    md.append(f'- **单事件期望收益**: {result["expected_return"]:.2f}%')
    md.append('')
    md.append('## 五、与方案 E v2 对比')
    md.append('')
    md.append('| 指标 | v2（翻倍当日）| **v3 提前版** |')
    md.append('|---|---:|---:|')
    md.append(f'| 样本数 | 137 | {result["test_top_n"]:,} |')
    md.append(f'| 胜率 | 40.1% | {result["test_win_rate"]*100:.1f}% |')
    md.append(f'| 单笔期望收益 | 5.38% | {result["expected_return"]:.2f}% |')
    md.append('')
    md.append('## 六、敏感性分析')
    md.append('')
    md.append('| 候选池 | 训练胜率 | 验证胜率 | 单笔期望 |')
    md.append('|:---:|---:|---:|---:|')
    for pct in [0.05, 0.10, 0.15, 0.20]:
        sub = run_backtest(df, args.train_start, args.train_end,
                           args.test_start, args.test_end, pct)
        md.append(f'| Top {pct*100:.0f}% | {sub["train_win_rate"]*100:.2f}% | '
                  f'{sub["test_win_rate"]*100:.2f}% | {sub["expected_return"]:.2f}% |')
    md.append('')
    md.append('## 七、风险提示')
    md.append('')
    md.append('1. **样本外衰减**：训练期 IC 强不等于验证期强，建议预留 5-10pp 衰减')
    md.append('2. **过拟合风险**：10 因子等权合成存在相关性，建议正交化（PCA）')
    md.append('3. **流动性风险**：高振幅低成交额标的实际买入可能滑 3-5%')
    md.append('4. **幸存者偏差**：未含退市股，长期收益可能高估')
    md.append('5. **门槛敏感性**：Top 10% 是经验值，已做敏感性分析')
    md.append('')
    md.append('## 八、决策建议')
    md.append('')
    test_wr = result['test_win_rate']
    train_wr = result['train_win_rate']
    overfit = abs(test_wr - train_wr) > 0.10
    if test_wr > 0.55 and not overfit:
        md.append('✅ **强信号**：验证期胜率 > 55%，训练验证差 < 10pp')
        md.append('   → **建议进入模拟交易**')
    elif test_wr > 0.52 and not overfit:
        md.append('🟡 **中等信号**：验证期胜率 52-55%')
        md.append('   → **建议小资金模拟**')
    elif overfit:
        md.append('🔴 **过拟合**：训练 vs 验证胜率差 > 10pp')
        md.append('   → **策略失效，需重新设计**（减少因子数 / 加入正则化）')
    else:
        md.append('🔴 **弱信号**：验证期胜率 < 52%')
        md.append('   → **策略无效**（仅与随机相当）')

    md.append('')
    md.append('---')
    md.append('')
    md.append('输出脚本: `scripts/plan_e_v3_early_backtest.py`  ')
    md.append('因子矩阵: `data/factors/event_factors.parquet`  ')
    md.append('显著性报告: `data/factor_significance_report.md`  ')

    out_fp = OUT_DIR / 'v3_early_backtest_report.md'
    out_fp.write_text('\n'.join(md), encoding='utf-8')
    print(f'\n报告: {out_fp}')

    if len(result['test_top_pos']) > 0:
        candidate_fp = OUT_DIR / f'v3_early_test_candidates_{datetime.now():%Y%m%d_%H%M}.csv'
        cols = ['code', 'name', 'signal_date', 'score', 'label', 'pct_change_20d_actual']
        neg_part = result['test_top_neg'][cols[:-1]].assign(
            pct_change_20d_actual=np.nan, group='neg'
        )
        out_df = pd.concat([
            result['test_top_pos'][cols].assign(group='pos'),
            neg_part,
        ])
        out_df.to_csv(candidate_fp, index=False)
        print(f'候选池: {candidate_fp} ({len(out_df):,} 条)')


if __name__ == '__main__':
    main()
