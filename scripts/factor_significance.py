"""
因子显著性检验
==================

输入:
- data/factors/event_factors.parquet: 事件驱动因子矩阵（正负 1:1）

输出:
- data/factor_significance_report.md: Markdown 报告
- data/factor_significance_detail.csv: 详细数值（IC/RankIC/p-value/分层胜率）
- data/factor_significance_orthogonalized.csv: 正交化后因子显著性（原始/正交/残差 RankIC 对照）

检验方法:
1. IC / RankIC: 因子值与 label (1=翻倍) 的相关性
2. 分层回测: 按因子分 5 quintile，看每 quintile 的"实际正样本率"
3. 显著性: p-value < 0.01 为强显著，< 0.05 为显著
4. 正交化与增量显著性（P2-4）: 用对称正交化 Z(ZᵀZ)^(-1/2) 对相关因子去相关，
   并经 L² 投影残差度量每因子的"独有信号"（剔除同伴后的 RankIC）
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path('/home/jiuben/tdx-data-feed')
FACTOR_FP = ROOT / 'data/factors/event_factors.parquet'
REPORT_FP = ROOT / 'data/factor_significance_report.md'
DETAIL_FP = ROOT / 'data/factor_significance_detail.csv'
ORTH_FP = ROOT / 'data/factor_significance_orthogonalized.csv'

# 仓库根加入 sys.path，使 `import quant` 在直接运行脚本时可用（与 plan_e_journal.py 一致）。
sys.path.insert(0, str(ROOT))

from quant.multifactor.cv_utils import bh_fdr_correct  # noqa: E402  # P0-1 BH-FDR
from quant.multifactor.factors import (  # noqa: E402
    cross_sectional_projection,
    orthogonalize_factor_panel,
)


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


def _signif_brief(df: pd.DataFrame, factor_col: str) -> dict:
    """取 calc_factor_significance 的关键字段；数据不足时返回 nan 占位。"""
    r = calc_factor_significance(df, factor_col)
    if 'note' in r:
        return {'rank_ic': float('nan'), 'rank_ic_p': float('nan'),
                'sig_level': '○ 不显著', 'n': int(r.get('n', 0))}
    return {'rank_ic': float(r['rank_ic']), 'rank_ic_p': float(r['rank_ic_p']),
            'sig_level': r['sig_level'], 'n': int(r['n'])}


def orthogonalize_factor_significance(df: pd.DataFrame, factor_cols: list[str],
                                     method: str = "symmetric") -> pd.DataFrame | None:
    """对因子集做 P2-4 对称正交化，并重算每因子的「原始 / 正交 / 残差」显著性对照。

    目的：因子间高度共线时，独立检验会把同伴的解释力误归到自身。本函数
    1) 用 `symmetric_orthogonalize`（Z(ZᵀZ)^(-1/2)）对相关因子去相关，得到互不相关的因子集；
    2) 用 `cross_sectional_projection` 把每因子对其余因子做 L² 投影，取残差（=剔除同伴后的增量信号）；
    3) 对三类因子（原始 / 对称正交化列 / 残差）重算 RankIC 与显著性，在同一完整样本子集上对齐。

    返回 DataFrame（每因子一行）：rank_ic_raw/rank_ic_p_raw/sig_raw、
    rank_ic_orth/rank_ic_p_orth/sig_orth、rank_ic_resid/rank_ic_p_resid/sig_resid、
    redundant（原始显著但残差不显著=被同伴解释）、independent（残差仍显著=独特信号）。
    样本不足（完整子集 <50）或因子数 <2 时返回 None。
    """
    needed = [*factor_cols, 'label']
    complete = df[needed].dropna()
    if len(complete) < 50 or len(factor_cols) < 2:
        return None

    panel = complete[factor_cols].astype(float)
    orth = orthogonalize_factor_panel(panel, method=method)

    F = panel.to_numpy()
    resid = {}
    for j, c in enumerate(factor_cols):
        others = np.delete(F, j, axis=1)
        _, r = cross_sectional_projection(F[:, j], others)
        resid[c] = r

    data: dict[str, np.ndarray] = {}
    for c in factor_cols:
        data[f'raw__{c}'] = panel[c].to_numpy()
        data[f'orth__{c}'] = orth[c].to_numpy()
        data[f'resid__{c}'] = resid[c]
    data['label'] = complete['label'].to_numpy()
    scored = pd.DataFrame(data)

    rows = []
    resid_pvals: list[float] = []
    for c in factor_cols:
        raw = _signif_brief(scored, f'raw__{c}')
        o = _signif_brief(scored, f'orth__{c}')
        res = _signif_brief(scored, f'resid__{c}')
        # 数值退化保护：高度共线因子的 L² 投影残差数学上为 0，但浮点噪声
        # （量级正比于输入→与 label 相关）会被 Spearman 排名误判为显著正相关。
        # 残差标准差远小于原始因子（被同伴几乎完全解释）→ 视为无独立增量信号。
        resid_std = float(np.std(resid[c]))
        raw_std = float(panel[c].std())
        if resid_std < max(1e-9, 1e-6 * (raw_std if raw_std > 0 else 1.0)):
            res = {'rank_ic': float('nan'), 'rank_ic_p': float('nan'),
                   'sig_level': '○ 不显著', 'n': len(resid[c])}
        resid_pvals.append(res['rank_ic_p'])
        rows.append({
            'factor': c,
            'rank_ic_raw': raw['rank_ic'], 'rank_ic_p_raw': raw['rank_ic_p'], 'sig_raw': raw['sig_level'],
            'rank_ic_orth': o['rank_ic'], 'rank_ic_p_orth': o['rank_ic_p'], 'sig_orth': o['sig_level'],
            'rank_ic_resid': res['rank_ic'], 'rank_ic_p_resid': res['rank_ic_p'], 'sig_resid': res['sig_level'],
        })
    out = pd.DataFrame(rows)
    # === P0-1: 残差 RankIC 的 BH-FDR 校正 ===
    # 正交化后「独立信号」判定同样面临多重检验：对 k 个因子的残差 RankIC 同时做
    # 单点检验会制造假阳性。用与主表相同的 BH-FDR 压低 FDR，使「独立」判定与主表口径一致。
    rej_resid, q_resid = bh_fdr_correct(np.asarray(resid_pvals, dtype=float), alpha=0.05)
    out['q_value_resid'] = q_resid
    out['fdr_sig_resid'] = rej_resid

    raw_sig = out['rank_ic_p_raw'] < 0.05
    resid_sig = out['fdr_sig_resid']      # P0-1：以 FDR 校正后的残差判定为准
    # 残差 IC 为 NaN（残差恒为常量 = 被同伴完全解释）时 q=nan → reject=False → 归入冗余
    out['independent'] = raw_sig & resid_sig
    out['redundant'] = raw_sig & ~resid_sig
    return out


def format_orth_section(orth_res: pd.DataFrame, method: str) -> list[str]:
    """生成「六、正交化与增量显著性」Markdown 段落。"""
    md = []
    md.append('## 六、正交化与增量显著性（P2-4 对称正交化）')
    md.append('')
    md.append('> 因子间高度共线（如 `limit_up_5d/10d/20d`、`pct_chg_5d/10d/20d`）：独立检验会把同伴的解释力'
              '误归到自身。本阶段用 `quant.multifactor.factors` 的 **对称正交化**'
              ' `Z(ZᵀZ)^(-1/2)` 替代此前文档中未实现的 PCA 正交化建议，并经 **L² 投影残差**'
              '（`cross_sectional_projection`）度量每因子的「增量显著性」——剔除其余因子后该因子残差对 label 的 RankIC。')
    md.append('')
    md.append('### 6.1 判定口径')
    md.append('')
    md.append('| 指标 | 含义 |')
    md.append('|---|---|')
    md.append('| RankIC(原始) | 单因子与 label 的 Spearman（同主表，但在完整样本子集上重算以对齐维度） |')
    md.append('| RankIC(残差) | 因子对「其余因子」做 L² 投影后的残差与 label 的 RankIC = 该因子独有信号 |')
    md.append('| 冗余 | 原始显著(p<0.05) 但 残差 FDR 不显著 → 信号可由同伴解释 |')
    md.append('| 独立 | 残差仍 FDR 显著(q<0.05) → 提供独特信息，应优先保留 |')
    md.append('| 残差 FDR | 对「各因子残差 RankIC」同时做 BH-FDR 校正（P0-1），q<0.05 判独立 |')
    md.append('')
    md.append('### 6.2 正交化后因子显著性对照')
    md.append('')
    md.append('| 因子 | RankIC原 | p原 | RankIC残差 | p残差 | q(FDR)残差 | 判定 |')
    md.append('|---:|---:|---:|---:|---:|---:|:---:|')
    for _, r in orth_res.iterrows():
        if pd.isna(r['rank_ic_resid']):
            judge = '—'
        elif r['redundant']:
            judge = '冗余(被同伴解释)'
        elif r['independent']:
            judge = '独立信号'
        else:
            judge = '弱/不显著'
        q_disp = f"{r['q_value_resid']:.2e}" if pd.notna(r['q_value_resid']) else '—'
        md.append(
            f"| `{r['factor']}` | {r['rank_ic_raw']:.3f} | {r['rank_ic_p_raw']:.2e} | "
            f"{r['rank_ic_resid']:.3f} | {r['rank_ic_p_resid']:.2e} | {q_disp} | {judge} |"
        )
    md.append('')
    n_indep = int(orth_res['independent'].sum())
    n_redun = int(orth_res['redundant'].sum())
    md.append('### 6.3 结论')
    md.append('')
    md.append(f'- **独立信号因子**：**{n_indep}** 个（残差仍显著，提供独特信息，合成时优先保留）')
    md.append(f'- **冗余因子**：**{n_redun}** 个（原始显著但残差不显著，信号可由同伴解释，合成时去重或仅留代表）')
    if n_indep == 0:
        md.append('- ⚠️ 正交化后无因子残差显著：当前因子集在剔除共线后未贡献独特 alpha，'
                  '与 P2-3/P2-4「低信噪比、数学升级不创造 alpha」结论一致。')
    md.append('')
    return md


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--factor-cols', nargs='+', default=None,
                        help='指定因子列；默认自动检测')
    parser.add_argument('--orth-method', choices=['symmetric', 'gram_schmidt', 'none'],
                        default='symmetric',
                        help='因子正交化方法：symmetric=对称正交化(Z(ZᵀZ)^(-1/2))；'
                             'gram_schmidt=标准正交基；none=跳过（仅原始显著性）')
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
    factor_cols = args.factor_cols or [c for c in df.columns if c not in exclude_cols]

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

    # === P0-1: BH-FDR 多重检验校正 ===
    # 22 因子同时检验, 裸 p<0.05 会大量假阳性; Benjamini-Hochberg 控制 FDR=0.05
    rej, q = bh_fdr_correct(res_df['rank_ic_p'].to_numpy(), alpha=0.05)
    res_df['q_value'] = q
    res_df['fdr_sig'] = rej

    res_df.to_csv(DETAIL_FP, index=False)
    print(f'\n详细结果: {DETAIL_FP}')

    # === P2-4：对称正交化 + 增量显著性（因子集去相关后重算） ===
    orth_res = None
    if args.orth_method != 'none' and len(factor_cols) >= 2:
        print(f'[{datetime.now():%H:%M:%S}] 因子正交化与增量显著性分析 ({args.orth_method})...')
        orth_res = orthogonalize_factor_significance(df, factor_cols, method=args.orth_method)
        if orth_res is not None:
            orth_res.to_csv(ORTH_FP, index=False)
            n_indep = int(orth_res['independent'].sum())
            n_redun = int(orth_res['redundant'].sum())
            print(f'  正交化明细: {ORTH_FP}  (独立 {n_indep} / 冗余 {n_redun})')
        else:
            print('  样本不足或因子数<2，跳过正交化分析')

    # === 生成 Markdown 报告 ===
    sig_count = (res_df['rank_ic_p'] < 0.05).sum()
    strong_count = (res_df['rank_ic_p'] < 0.01).sum()
    very_strong = (res_df['rank_ic_p'] < 0.001).sum()
    fdr_sig_count = int(res_df['fdr_sig'].sum())

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
    md.append('| **正交化与增量显著性 (P2-4)** | 对称正交化去相关 + L² 投影残差度量"剔除同伴后的独有信号" |')
    md.append('')
    md.append('## 二、汇总统计')
    md.append('')
    md.append(f'- **总因子数**: {len(factor_cols)}')
    md.append(f'- **极强显著 (p<0.001)**: {very_strong} 个')
    md.append(f'- **强显著 (p<0.01)**: {strong_count} 个')
    md.append(f'- **显著 (p<0.05)**: {sig_count} 个')
    md.append(f'- **不显著**: {len(factor_cols) - sig_count} 个')
    md.append(f'- **FDR 校正后仍显著 (q<0.05)**: {fdr_sig_count} 个（裸显著 {sig_count} 个 → 校正后 {fdr_sig_count} 个）')
    md.append('')
    md.append('## 三、因子排序（按 |RankIC| 降序）')
    md.append('')
    md.append('| 排名 | 因子 | RankIC | p-value | q(FDR) | 显著性 | 单调 | Q1胜率 | Q5胜率 | 差距 | 样本数 |')
    md.append('|:---:|---|---:|---:|---:|:---:|:---:|---:|---:|---:|---:|')
    for i, r in res_df.head(30).iterrows():
        rank = i + 1
        q_disp = f"{r['q_value']:.2e}" if pd.notna(r['q_value']) else '—'
        md.append(
            f'| {rank} | `{r["factor"]}` | {r["rank_ic"]:.3f} | '
            f'{r["rank_ic_p"]:.2e} | {q_disp} | {r["sig_level"]} | '
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

    # === P2-4：正交化与增量显著性（第六节） ===
    if orth_res is not None:
        md.extend(format_orth_section(orth_res, args.orth_method))

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
    md.append('| **正交化** | 对称正交化 Z(ZᵀZ)^(-1/2) 去相关（P2-4，已实际执行，见第六节），再等权 |')
    md.append('| **模型融合** | LightGBM / 神经网络非线性组合 |')
    md.append('')
    md.append('### 5.3 风险提示')
    md.append('')
    md.append('- **多重检验偏差已校正（P0-1）**：BH-FDR 校正后仍有 '
              f'{fdr_sig_count}/{len(factor_cols)} 个因子 q<0.05；若校正后显著数远少于裸显著数，'
              '说明多数裸 p 显著是多重检验假阳性，须谨慎采信。')
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
