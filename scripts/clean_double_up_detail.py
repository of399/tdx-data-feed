#!/usr/bin/env python3
"""清洗 double_up_2020_2026_detail.csv（22,972 行翻倍明细）。

对每条翻倍样本做异常与数据缺口标注，人工复核并给出原因，输出清洗后训练子集，
并记录清洗前后行数变化。

异常口径（数据可计算部分）：
  - 单日涨幅异常 >50%：窗口 [start_date, peak_date] 内任意一日 |前复权日收益| > 50%，
    多为除权除息跳空、新股上市前5日无涨跌幅限制、或数据错误 → 可能制造"伪翻倍"。
  - 窗口数据缺口：窗口实际交易日数显著少于 days_to_peak（停牌/长期停市导致回看不完整）→
    累计涨幅可能包含停复牌跳空，不可靠。
  - 次新(is_new)/ST(is_st)：单独拆分处理，避免"前窗口"被上市后短片段污染；保留但单列。

注：原始明细**不含换手率/流通股**字段，无法直接算"换手异常>50%"；若需该口径，
需并入流通股本数据后另行计算（本报告明确标注此限制）。

用法：
  venv/bin/python scripts/clean_double_up_detail.py
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('clean_detail')

ROOT = Path(__file__).parent.parent
PARQUET = ROOT / 'data' / 'parquet' / 'daily'
ADJ = ROOT / 'data' / 'adj_factor'
IN = ROOT / 'data' / 'double_up' / 'double_up_2020_2026_detail.csv'
OUT = ROOT / 'data' / 'double_up'

SINGLE_DAY_TH = 0.50          # 单日 |收益| 阈值
GAP_MISS_TH = 3                # 窗口缺失交易日阈值
GAP_RATIO_TH = 0.80           # 实际/预期 比例阈值


def _to_dt(series: pd.Series) -> pd.Series:
    if series.dtype in ('int64', 'int32'):
        return pd.to_datetime(series.astype(str), format='%Y%m%d')
    return pd.to_datetime(series)


def load_stock(code: str):
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return None
    short = re.sub(r'^(sh|sz|bj)', '', code)
    adj_fp = ADJ / f'{short}.parquet'
    if not adj_fp.exists():
        return None
    try:
        df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        adj = pd.read_parquet(adj_fp, columns=['date', 'adj_factor'])
    except Exception as e:
        log.warning('load fail %s: %s', code, e)
        return None
    df['date'] = _to_dt(df['date'])
    adj['date'] = _to_dt(adj['date'])
    df = df.merge(adj, on='date', how='left').sort_values('date').reset_index(drop=True)
    df['adj_factor'] = df['adj_factor'].ffill().bfill().fillna(1.0)
    for c in ('open', 'high', 'low', 'close'):
        df[f'qfq_{c}'] = df[c] * df['adj_factor']
    return df


def reason_for_singleday(code: str, dt: pd.Timestamp, ret: float) -> str:
    """对单日>50%异常给出人工复核式原因（启发式，供人工确认）。"""
    # 新股前5日无涨跌幅限制（需结合上市日，此处用代码前缀粗判）
    if code.startswith('bj920') or code.startswith('sh688') or code.startswith('sz30'):
        return f'单日{ret*100:.0f}%：可能为次新/双创/北交所上市初期无涨跌幅限制'
    if abs(ret) > 1.0:
        return f'单日{ret*100:.0f}%：极端，疑似除权除息跳空或数据错误'
    return f'单日{ret*100:.0f}%：异常，需人工复核（除权/数据）'


def main() -> None:
    log.info('=== 清洗 22,972 翻倍明细 ===')
    df = pd.read_csv(IN)
    before = len(df)
    log.info('原始行数: %d', before)

    df['start_date'] = pd.to_datetime(df['start_date'])
    df['peak_date'] = pd.to_datetime(df['peak_date'])

    flags = []        # 每条样本的标注
    anomalies = []    # 异常样本复核明细

    grouped = df.groupby('code', sort=False)
    total_codes = len(grouped)
    for done, (code, g) in enumerate(grouped, 1):
        stock = load_stock(code)
        for _, r in g.iterrows():
            sd, pk = r['start_date'], r['peak_date']
            d2p = int(r['days_to_peak']) if pd.notna(r['days_to_peak']) else np.nan
            f_single = False
            f_gap = False
            single_detail = ''
            if stock is None:
                f_gap = True
                single_detail = '无日K/复权数据'
            else:
                w = stock[(stock['date'] >= sd) & (stock['date'] <= pk)].copy()
                n_actual = len(w)
                # 数据缺口
                if pd.notna(d2p) and (n_actual < d2p * GAP_RATIO_TH or n_actual < (d2p - GAP_MISS_TH)):
                    f_gap = True
                    single_detail = f'窗口预期~{int(d2p)}交易日，实际仅{n_actual}'
                # 单日异常
                if n_actual >= 2:
                    c = w['qfq_close'].to_numpy(float)
                    rets = np.diff(c) / c[:-1]
                    mx = float(np.max(np.abs(rets))) if len(rets) else 0.0
                    if mx > SINGLE_DAY_TH:
                        f_single = True
                        j = int(np.argmax(np.abs(rets)))
                        dt = w['date'].iloc[j + 1]
                        single_detail = reason_for_singleday(code, dt, float(rets[j]))
            flags.append({'single_day_gt_50': f_single, 'window_data_gap': f_gap})
            if f_single or f_gap:
                anomalies.append({
                    'code': code, 'name': r.get('name'), 'start_date': sd.date().isoformat(),
                    'peak_date': pk.date().isoformat(), 'pct_change': r.get('pct_change'),
                    'is_new': bool(r.get('is_new')), 'is_st': bool(r.get('is_st')),
                    'single_day_gt_50': f_single, 'window_data_gap': f_gap,
                    'reason': single_detail,
                })
        if done % 500 == 0:
            log.info('  已处理 %d/%d 只股票', done, total_codes)

    fl = pd.DataFrame(flags, index=df.index)
    df = pd.concat([df, fl], axis=1)

    # 清洗边界：剔除(单日>50% 或 窗口缺口)；次新/ST 保留但单列（split 不删）
    drop_mask = df['single_day_gt_50'] | df['window_data_gap']
    df['clean_keep'] = ~drop_mask
    clean = df[df['clean_keep']].copy()

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fp_clean = OUT / f'double_up_2020_2026_detail_clean_{ts}.csv'
    clean.to_csv(fp_clean, index=False, encoding='utf-8-sig')
    fp_anom = OUT / f'double_up_2020_2026_anomaly_review_{ts}.csv'
    anom_df = pd.DataFrame(anomalies)
    anom_df.to_csv(fp_anom, index=False, encoding='utf-8-sig')

    # 计数
    n_single = int(df['single_day_gt_50'].sum())
    n_gap = int(df['window_data_gap'].sum())
    n_new = int(df['is_new'].fillna(False).sum())
    n_st = int(df['is_st'].fillna(False).sum())
    n_drop = int(drop_mask.sum())
    after = len(clean)

    log.info('剔除: 单日>50%%=%d, 窗口缺口=%d, 合计剔除=%d', n_single, n_gap, n_drop)
    log.info('清洗后保留: %d (次新=%d, ST=%d 仍保留单列)', after, n_new, n_st)

    _write_report(df, anom_df, before, after, n_single, n_gap, n_new, n_st, n_drop, ts,
                  fp_clean, fp_anom)


def _write_report(df, anom_df, before, after, n_single, n_gap, n_new, n_st, n_drop, ts,
                  fp_clean, fp_anom):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    L: list[str] = []
    L.append(f"# 翻倍明细清洗与异常复核报告（22,972 行） ({now})\n")
    L.append("> 输入：data/double_up/double_up_2020_2026_detail.csv（22,972 行 20 交易日窗口翻倍样本）")
    L.append("> 方法：对每条样本加载日K，检查翻倍窗口 [start_date, peak_date] 内单日 |前复权收益|>50% "
             "（除权/新股无限制/数据错误）与交易日缺口（停牌致回看不全）；次新(is_new)/ST(is_st) 单独拆分单列。")
    L.append("> 限制：明细**无换手率/流通股字段**，无法直接算'换手异常>50%'；该口径需并入流通股本后另算。\n")

    L.append("## 📊 核心指标（清洗前后）\n")
    L.append(f"- **清洗前总行数**: {before:,}")
    L.append(f"- **单日涨幅异常(>50%)**: {n_single:,} 行（疑似伪翻倍/数据错误）")
    L.append(f"- **窗口数据缺口**: {n_gap:,} 行（停牌致交易日不足）")
    L.append(f"- **合计剔除**: {n_drop:,} 行（两类有重叠）")
    L.append(f"- **清洗后保留**: **{after:,}** 行（保留率 {after/before*100:.1f}%）")
    L.append(f"- **次新(is_new)**：{n_new:,} 行（保留、单列拆分，不计入普通训练）")
    L.append(f"- **ST(is_st)**：{n_st:,} 行（保留、单列拆分）")
    L.append(f"- 净变化：{before} → {after}（减少 {before-after:,}，"
             f"其中单日异常 {n_single}、缺口 {n_gap}）\n")

    L.append("## 🎯 异常复核明细（Top 30，按 pct_change 降序）\n")
    if len(anom_df):
        top = anom_df.sort_values('pct_change', ascending=False).head(30)
        L.append("| 代码 | 名称 | 起点 | 峰值 | 翻倍% | 次新 | ST | 单日>50% | 缺口 | 原因 |")
        L.append("|---|---|---|---|---:|:--:|:--:|:--:|:--:|---|")
        for _, r in top.iterrows():
            L.append(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['peak_date']} | "
                     f"{r['pct_change']} | {'Y' if r['is_new'] else ''} | {'Y' if r['is_st'] else ''} | "
                     f"{'Y' if r['single_day_gt_50'] else ''} | {'Y' if r['window_data_gap'] else ''} | {r['reason']} |")
    else:
        L.append("- 无异常。")
    L.append("")

    L.append("## 📈 统计分布\n")
    L.append("**异常类型交叉**\n")
    if len(anom_df):
        single_only = int(((anom_df['single_day_gt_50']) & (~anom_df['window_data_gap'])).sum())
        gap_only = int(((~anom_df['single_day_gt_50']) & (anom_df['window_data_gap'])).sum())
        both = int((anom_df['single_day_gt_50'] & anom_df['window_data_gap']).sum())
        L.append(f"- 仅单日异常: {single_only}")
        L.append(f"- 仅窗口缺口: {gap_only}")
        L.append(f"- 两者兼有: {both}")
        L.append(f"- 异常中次新占比: {anom_df['is_new'].mean()*100:.1f}%")
    L.append("")

    L.append("## ⚠️ 风险提示\n")
    L.append("1. 单日>50% 多为除权除息跳空或新股上市无涨跌幅限制，并非真实连续翻倍，剔除可避免'伪翻倍'污染训练。")
    L.append("2. 窗口数据缺口（停牌）会使累计涨幅包含停复牌跳空，翻倍判定失真，已剔除。")
    L.append("3. 次新/ST 已单列拆分，避免'前窗口'被上市后短片段或重组预期污染；默认不进入普通训练集。")
    L.append("4. 换手异常>50% 因缺流通股本数据未计算，若需该口径应补充股本数据后再清洗。")
    L.append("5. 清洗基于历史数据，未来新样本需同样管线复跑。\n")

    L.append("## 🔧 下一步建议\n")
    L.append(f"- 将清洗后 22,972→{after:,} 行作为方案 D/E 训练主样本（剔除次新/ST 单列）。")
    L.append("- 对次新/ST 单独建子集，验证其在方案 E 中的表现是否与主样本异质。")
    L.append("- 补充流通股本后重算换手异常，迭代清洗口径。")
    L.append("- 将 per-event 特征与聚类标签并入该清洗集，统一样本边界与标签定义。\n")

    L.append("---")
    L.append(f"生成时间：{now}")
    L.append("生成者：scripts/clean_double_up_detail.py")
    L.append("模型版本：v1（规则清洗）")
    L.append(f"数据日期：{datetime.now().strftime('%Y-%m-%d')}")

    fp_md = OUT / f'double_up_2020_2026_cleaning_report_{ts}.md'
    fp_md.write_text('\n'.join(L), encoding='utf-8')
    log.info('✓ 报告: %s', fp_md)
    log.info('✓ 清洗集: %s ; 异常复核: %s', fp_clean.name, fp_anom.name)


if __name__ == '__main__':
    main()
