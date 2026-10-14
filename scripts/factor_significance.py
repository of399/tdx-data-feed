"""
因子显著性检验
==================

输入:
- data/factors/event_factors.parquet: 事件驱动因子矩阵（正负 1:1）

输出:
- data/factor_significance_report.md: Markdown 报告
- data/factor_significance_detail.csv: 详细数值（IC/RankIC/p-value/分层胜率）

检验方法:
1. IC / RankIC: 因子值与 label (1=翻倍) 的相关性
2. 分层回测: 按因子分 5 quintile，看每 quintile 的"实际正样本率"
3. 显著性: p-value < 0.01 为强显著，< 0.05 为显著
"""
import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path('/home/jiuben/tdx-data-feed')
FACTOR_FP = ROOT / 'data/factors/event_factors.parquet'
REPORT_FP = ROOT / 'data/factor_significance_report.md'
DETAIL_FP = ROOT / 'data/factor_significance_detail.csv'


def calc_factor_significance(df: pd.DataFrame, factor_col: str) -> dict:
    """计算单因子的 IC / RankIC / 分层胜率"""
    valid = df[[factor_col, 'label']].dropna()
    if len(valid) < 50 or valid[factor_col].std() == 0:
        return {'factor': factor_col, 'n': len(valid), 'note': '数据不足/无方差'}

    # IC (Pearson)
    ic, ic_p = stats.pearsonr(valid[factor_col], valid['label'])

    # RankIC (Spearman)
    ric, ric_p = stats.spearmanr(valid[factor_col], valid['label'])

    # 分层（5 quintile）
    try:
        valid = valid.copy()
        valid['quintile'] = pd.qcut(valid[factor_col], 5, labels=[1, 2, 3, 4, 5], duplicates='drop')
        q_stats = valid.groupby('quintile').agg(
            n=('label', 'size'),
            pos_rate=('label', 'mean'),
            mean_factor=(factor_col, 'mean'),
        )
        # 单调性: 相邻 quintile 是否单调
        monotonic = all(
            q_stats['pos_rate'].iloc[i] <= q_stats['pos_rate'].iloc[i + 1]
            for i in range(len(q_stats) - 1)
        ) or all(
            q_stats['pos_rate'].iloc[i] >= q_stats['pos_rate'].iloc[i + 1]
            for i in range(len(q_stats) - 1)
        )
        q5_pos_rate = q_stats['pos_rate'].iloc[-1]
        q1_pos_rate = q_stats['pos_rate'].iloc[0]
        spread = q5_pos_rate - q1_pos_rate
    except Exception:
        q_stats = None
        monotonic = False
        q5_pos_rate = q1_pos_rate = spread = np.nan

    # 显著性分级
    if ric_p < 0.001:
        sig_level = '★★★ 极强'
    elif ric_p < 0.01:
        sig_level = '★★ 强'
    elif ric_p < 0.05:
        sig_level = '★ 显著'
    else:
        sig_level = '○ 不显著'

    return {
        'factor': factor_col,
        'n': len(valid),
        'ic': ic,
        'ic_p': ic_p,
        'rank_ic': ric,
        'rank_ic_p': ric_p,
        'sig_level': sig_level,
        'monotonic': monotonic,
        'q1_pos_rate': q1_pos_rate,
        'q5_pos_rate': q5_pos_rate,
        'spread': spread,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--factor-cols', nargs='+', default=None,
                        help='指定因子列；默认自动检测')
    args = parser.parse_args()

    print(f'[{datetime.now():%H:%M:%S}] 加载因子矩阵...')
    df = pd.read_parquet(FACTOR_FP)
    if args.limit > 0:
        df = df.sample(n=args.limit, random_state=42)
    print(f'  总行数: {len(df):,}')
    print(f'  正样本: {(df["label"]==1).sum():,}  负样本: {(df["label"]==0).sum():,}')
    print(f'  正负比: {(df["label"]==1).mean()*100:.1f}%')

    # 自动检测因子列
    exclude_cols = {
        'code', 'name', 'signal_date', 'label', 'is_st', 'board', 'limit',
        'pct_change_20d_actual',
    }
    if args.factor_cols:
        factor_cols = args.factor_cols
    else:
        factor_cols = [c for c in df.columns if c not in exclude_cols]

    print(f'\n因子数: {len(factor_cols)}')
    print(f'[{datetime.now():%H:%M:%S}] 开始 IC 检验...')

    results = []
    for i, fc in enumerate(factor_cols):
        res = calc_factor_significance(df, fc)
        results.append(res)
        if (i + 1) % 5 == 0:
            print(f'  [{i+1}/{len(factor_cols)}] 完成')

    res_df = pd.DataFrame(results)
    # 按 |RankIC| 排序
    res_df['abs_rank_ic'] = res_df['rank_ic'].abs()
    res_df = res_df.sort_values('abs_rank_ic', ascending=False).reset_index(drop=True)
    res_df.to_csv(DETAIL_FP, index=False)
    print(f'\n详细结果: {DETAIL_FP}')

    # === 生成 Markdown 报告 ===
    sig_count = (res_df['rank_ic_p'] < 0.05).sum()
    strong_count = (res_df['rank_ic_p'] < 0.01).sum()
    very_strong = (res_df['rank_ic_p'] < 0.001).sum()

    md = []
    md.append('# 因子显著性检验报告')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}  ')
    md.append('> 数据: `data/factors/event_factors.parquet`  ')
    md.append('> 训练期: 2001-01 ~ 2026-08  ')
    md.append('> 验证期: 2025-01 ~ 2026-09（21 个月）  ')
    md.append(f'> 正样本: {(df["label"]==1).sum():,}  负样本: {(df["label"]==0).sum():,}  ')
    md.append('')
    md.append('## 一、检验方法')
    md.append('')
    md.append('| 方法 | 说明 |')
    md.append('|---|---|')
    md.append('| **IC (Pearson)** | 因子值与 label 的 Pearson 相关系数 |')
    md.append('| **RankIC (Spearman)** | 因子值与 label 的 Spearman 等级相关 |')
    md.append('| **分层胜率** | 按因子分 5 quintile，看每 quintile 的正样本率 |')
    md.append('| **单调性** | 相邻 quintile 正样本率是否单调 |')
    md.append('| **显著性** | p < 0.001 极强 / < 0.01 强 / < 0.05 显著 / ≥ 0.05 不显著 |')
    md.append('')
    md.append('## 二、汇总统计')
    md.append('')
    md.append(f'- **总因子数**: {len(factor_cols)}')
    md.append(f'- **极强显著 (p<0.001)**: {very_strong} 个')
    md.append(f'- **强显著 (p<0.01)**: {strong_count} 个')
    md.append(f'- **显著 (p<0.05)**: {sig_count} 个')
    md.append(f'- **不显著**: {len(factor_cols) - sig_count} 个')
    md.append('')
    md.append('## 三、因子排序（按 |RankIC| 降序）')
    md.append('')
    md.append('| 排名 | 因子 | RankIC | p-value | 显著性 | 单调 | Q1胜率 | Q5胜率 | 差距 | 样本数 |')
    md.append('|:---:|---|---:|---:|:---:|:---:|---:|---:|---:|---:|')
    for i, r in res_df.head(30).iterrows():
        rank = i + 1
        md.append(
            f'| {rank} | `{r["factor"]}` | {r["rank_ic"]:.3f} | '
            f'{r["rank_ic_p"]:.2e} | {r["sig_level"]} | '
            f'{"✓" if r["monotonic"] else "✗"} | '
            f'{r["q1_pos_rate"]*100:.1f}% | {r["q5_pos_rate"]*100:.1f}% | '
            f'{r["spread"]*100:+.1f}pp | {int(r["n"]):,} |'
        )

    md.append('')
    md.append('## 四、Top 5 强因子详细分层')
    md.append('')
    top5 = res_df.head(5)['factor'].tolist()
    for fc in top5:
        md.append(f'### 因子: `{fc}`')
        md.append('')
        valid = df[[fc, 'label']].dropna().copy()
        try:
            valid['quintile'] = pd.qcut(valid[fc], 5, labels=['Q1(低)', 'Q2', 'Q3', 'Q4', 'Q5(高)'])
            q = valid.groupby('quintile').agg(
                n=('label', 'size'),
                pos_rate=('label', 'mean'),
                mean_value=(fc, 'mean'),
            )
            md.append('| 分层 | 样本数 | 正样本率 | 均值 |')
            md.append('|:---:|---:|---:|---:|')
            for qname, row in q.iterrows():
                md.append(f'| {qname} | {int(row["n"]):,} | {row["pos_rate"]*100:.2f}% | {row["mean_value"]:.3f} |')
            md.append('')
        except Exception:
            md.append('*分层失败*')
            md.append('')

    md.append('## 五、结论与建议')
    md.append('')
    strong_factors = res_df[res_df['rank_ic_p'] < 0.01]['factor'].tolist()
    if strong_factors:
        md.append('### 5.1 可入选因子（p < 0.01）')
        md.append('')
        md.append('以下因子在 1:1 配对检验中显著：**' + ' / '.join(strong_factors[:10]) + '**')
        md.append('')
        md.append('**使用建议**：')
        md.append('- 等权合成 Rank 标准分（避免量纲差异）')
        md.append('- 优先选择 RankIC 绝对值 ≥ 0.05 的因子')
        md.append('- 单调性 ✓ 的因子更稳健（避免被噪音干扰）')
    else:
        md.append('### 5.1 无显著因子')
        md.append('')
        md.append('所有因子 p ≥ 0.01，建议：')
        md.append('- 增加新因子（如情绪、资金、行业轮动）')
        md.append('- 调整因子计算窗口')
        md.append('- 尝试非线性组合（决策树/神经网络）')

    md.append('')
    md.append('### 5.2 多因子组合建议')
    md.append('')
    md.append('| 组合方式 | 说明 |')
    md.append('|---|---|')
    md.append('| **等权求和** | 各因子 Rank 标准化后等权求和 |')
    md.append('| **IC 加权** | 以 RankIC 为权重加权求和 |')
    md.append('| **正交化** | 先用 PCA 去掉相关性，再等权 |')
    md.append('| **模型融合** | LightGBM / 神经网络非线性组合 |')
    md.append('')
    md.append('### 5.3 风险提示')
    md.append('')
    md.append('- **多重检验偏差**：22 个因子同时检验，至少 1 个假阳性概率 ~ 70%')
    md.append('- **幸存者偏差**：基于现存 8035 只，未含退市股')
    md.append('- **过拟合**：IC 在 in-sample 上统计显著但 out-of-sample 可能衰减 30-50%')
    md.append('- **建议**：Top 10 因子必须做 2025-2026 验证期检验')

    md.append('')
    md.append('---')
    md.append('')
    md.append(f'详细数值: `{DETAIL_FP.relative_to(ROOT)}`  ')
    md.append('生成脚本: `scripts/factor_significance.py`  ')

    REPORT_FP.write_text('\n'.join(md), encoding='utf-8')
    print(f'报告: {REPORT_FP}')

    # 控制台简短输出
    print('\n=== Top 10 因子 (按 |RankIC| 排序) ===')
    print(res_df[['factor', 'rank_ic', 'rank_ic_p', 'sig_level', 'monotonic', 'spread']].head(10).to_string(index=False))


if __name__ == '__main__':
    main()
