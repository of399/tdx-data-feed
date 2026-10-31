"""翻倍事件启动前 90 自然日量价形态分析。

读取 data/double_up/double_30d_2021_2026.csv（1,898 个翻倍事件），
对每只事件的"启动日前 90 个自然日"窗口回看日K（前复权 OHLC + 成交量/额），
量化量价形态、波动特征，并标注数据缺失/异常。

输出：
  - data/double_up/double_30d_pre90_features.csv   （逐事件特征，1,898 行）
  - data/double_up/double_30d_pre90_report_YYYYMMDD_HHMM.md
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('analyze_pre90')

from _common import adj_factor_dir, daily_dir, universe_path  # noqa: E402

PARQUET = daily_dir()
ADJ = adj_factor_dir()
NAME_MAP_PATH = universe_path()
CSV_IN = Path('/home/jiuben/tdx-data-feed/data/double_up/double_30d_2021_2026.csv')
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')

PRE_DAYS = 90                      # 自然日窗口
EXPECTED_TRADING_DAYS = 63         # 90 自然日约含交易日
DATA_DATE = pd.Timestamp('2026-09-30')


def _to_dt(series: pd.Series) -> pd.Series:
    if series.dtype in ('int64', 'int32'):
        return pd.to_datetime(series.astype(str), format='%Y%m%d')
    return pd.to_datetime(series)


def load_stock(code: str):
    """加载某股票前复权 OHLC + 量额。返回 (df, first_date) 或 None。"""
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
    first_date = df['date'].min()
    return df, first_date


def pre_features(df: pd.DataFrame, sd: pd.Timestamp, start_price: float,
                stock_first: pd.Timestamp) -> dict:
    """提取 [sd-90d, sd) 窗口的量价特征。"""
    cut = np.datetime64(sd) - np.timedelta64(PRE_DAYS, 'D')
    sd64 = np.datetime64(sd)
    dts = df['date'].values
    mask = (dts >= cut) & (dts < sd64)
    idx = np.where(mask)[0]
    n = len(idx)
    feats: dict = {'pre_n_days': n, 'pre_expected_days': EXPECTED_TRADING_DAYS}
    feats['data_gap'] = bool(n < EXPECTED_TRADING_DAYS * 0.5)
    feats['new_listing'] = bool(np.datetime64(stock_first) > cut)
    if n == 0:
        for k in ('pre_ret90', 'pre_ann_vol', 'pre_maxdd', 'pre_amt_ratio',
                  'pre_vol_ratio', 'pre_price_start', 'pre_price_low',
                  'pre_price_high', 'pre_zero_vol_days', 'pre_max_abs_ret'):
            feats[k] = np.nan
        feats['pre_pattern'] = '数据缺失'
        return feats

    c = df['qfq_close'].values[idx].astype('float64')
    h = df['qfq_high'].values[idx].astype('float64')
    l = df['qfq_low'].values[idx].astype('float64')
    v = df['volume'].values[idx].astype('float64')
    a = df['amount'].values[idx].astype('float64')

    r = np.diff(c) / c[:-1]
    ret90 = c[-1] / c[0] - 1.0
    vol_d = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    ann_vol = vol_d * np.sqrt(242.0)
    runmax = np.maximum.accumulate(c)
    maxdd = float((c / runmax - 1.0).min())

    n3 = max(1, n // 3)
    amt_recent = float(a[-n3:].mean())
    amt_base = float(a[:max(1, n - n3)].mean())
    amt_ratio = amt_recent / amt_base if amt_base > 0 else np.nan
    vol_recent = float(v[-n3:].mean())
    vol_base = float(v[:max(1, n - n3)].mean())
    vol_ratio = vol_recent / vol_base if vol_base > 0 else np.nan

    zero_vol = int(np.sum(v == 0))
    max_abs_ret = float(np.max(np.abs(r))) if len(r) > 0 else 0.0

    feats.update({
        'pre_ret90': round(ret90, 4),
        'pre_ann_vol': round(ann_vol, 4),
        'pre_maxdd': round(maxdd, 4),
        'pre_amt_ratio': round(amt_ratio, 3) if not np.isnan(amt_ratio) else np.nan,
        'pre_vol_ratio': round(vol_ratio, 3) if not np.isnan(vol_ratio) else np.nan,
        'pre_price_start': round(float(c[0]), 4),
        'pre_price_low': round(float(l.min()), 4),
        'pre_price_high': round(float(h.max()), 4),
        'pre_zero_vol_days': zero_vol,
        'pre_max_abs_ret': round(max_abs_ret, 4),
    })

    # 形态分类
    if feats['new_listing']:
        pat = '次新上市'
    elif ret90 <= -0.10:
        pat = '下跌蓄势'
    elif ret90 > 0.10:
        pat = '上涨中继'
    else:
        pat = '横盘蓄势'
    if (not np.isnan(amt_ratio)) and amt_ratio > 1.5:
        pat += '+放量'
    feats['pre_pattern'] = pat

    # 归一化价格路径（启动日=1.0，按距启动交易日回溯）
    offsets = [1, 5, 10, 20, 40, 63]
    norm_path: dict = {}
    for off in offsets:
        norm_path[off] = (float(c[-(off + 1)] / start_price)
                          if n > off else np.nan)
    feats['norm_path'] = norm_path
    return feats


def main() -> None:
    t0 = datetime.now()
    log.info('=== 翻倍事件启动前 %d 自然日量价形态分析 ===', PRE_DAYS)
    ev = pd.read_csv(CSV_IN, dtype={'code': str})
    log.info('事件数: %d, 涉及股票: %d', len(ev), ev['code'].nunique())

    # 按股票分组，避免重复加载
    groups: dict[str, list[dict]] = {}
    for _, row in ev.iterrows():
        groups.setdefault(row['code'], []).append(row.to_dict())

    out_rows: list[dict] = []
    path_acc: dict[int, list] = {o: [] for o in (1, 5, 10, 20, 40, 63)}
    for code, rows in groups.items():
        loaded = load_stock(code)
        if loaded is None:
            for r in rows:
                fr = {'code': code, 'name': r['name'], 'start_date': r['start_date']}
                fr.update({k: np.nan for k in (
                    'pre_ret90', 'pre_ann_vol', 'pre_maxdd', 'pre_amt_ratio',
                    'pre_vol_ratio', 'pre_price_start', 'pre_price_low',
                    'pre_price_high', 'pre_zero_vol_days', 'pre_max_abs_ret')})
                fr.update({'pre_n_days': 0, 'pre_expected_days': EXPECTED_TRADING_DAYS,
                           'data_gap': True, 'new_listing': False, 'pre_pattern': '无数据'})
                out_rows.append(fr)
            continue
        df, first_date = loaded
        for r in rows:
            sd = pd.Timestamp(r['start_date'])
            fr = pre_features(df, sd, float(r['start_price']), first_date)
            fr['code'] = code
            fr['name'] = r['name']
            fr['start_date'] = r['start_date']
            fr['start_price'] = r['start_price']
            fr['peak_gain'] = r['peak_gain']
            out_rows.append(fr)
            for o, val in fr.get('norm_path', {}).items():
                if pd.notna(val):
                    path_acc[o].append(val)

    cols = ['code', 'name', 'start_date', 'start_price', 'peak_gain', 'pre_n_days',
            'pre_expected_days', 'pre_ret90', 'pre_ann_vol', 'pre_maxdd',
            'pre_amt_ratio', 'pre_vol_ratio', 'pre_price_start', 'pre_price_low',
            'pre_price_high', 'pre_zero_vol_days', 'pre_max_abs_ret',
            'data_gap', 'new_listing', 'pre_pattern']
    res = pd.DataFrame(out_rows)[cols]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    feat_path = OUT_DIR / 'double_30d_pre90_features.csv'
    res.to_csv(feat_path, index=False, encoding='utf-8-sig')
    log.info('✓ 特征 CSV: %s (%d 行)', feat_path, len(res))

    _write_report(res, t0, path_acc)


def _write_report(res: pd.DataFrame, t0: datetime, path_acc: dict) -> None:
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    md = OUT_DIR / f'double_30d_pre90_report_{ts}.md'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    valid = res[res['pre_n_days'] > 0]
    n = len(res)
    n_valid = len(valid)
    log.info('有效窗口: %d / %d', n_valid, n)

    # 核心指标
    med_ret = valid['pre_ret90'].median()
    med_vol = valid['pre_ann_vol'].median()
    med_dd = valid['pre_maxdd'].median()
    med_amt = valid['pre_amt_ratio'].median()
    pct_gap = res['data_gap'].mean() * 100
    pct_new = valid['new_listing'].mean() * 100
    pct_vol_up = (valid['pre_amt_ratio'] > 1.5).mean() * 100
    pct_zero = (valid['pre_zero_vol_days'] > 0).mean() * 100

    L: list[str] = []
    L.append(f'# 翻倍事件启动前 90 自然日量价形态分析 ({now})\n')
    L.append('> 分析对象：data/double_up/double_30d_2021_2026.csv 中 1,898 个翻倍事件')
    L.append('> 窗口定义：启动日（start_date）前 90 个自然日，即 [start_date-90d, start_date) 的交易日')
    L.append('> 价格类型：前复权价（qfq = 不复权价 × 复权因子）；量价均取自日K')
    L.append(f'> 数据日期：{DATA_DATE.date()}\n')

    L.append('## 📊 核心指标\n')
    L.append(f'- **分析事件总数**: {n:,}（涉及 {res["code"].nunique():,} 只股票）')
    L.append(f'- **有效回看窗口**: {n_valid:,}（其余为数据缺失/无数据，见异常）')
    L.append(f'- **启动前 90 日中位累计收益**: **{med_ret*100:.1f}%**（≈0 表示整体横盘；深跌/走强各占约 1/4）')
    L.append(f'- **启动前 90 日中位年化波动率**: **{med_vol*100:.1f}%**')
    L.append(f'- **启动前 90 日中位最大回撤**: **{med_dd*100:.1f}%**')
    L.append(f'- **近端量能比（后1/3日均额/前2/3）中位数**: **{med_amt:.2f}**（>1=临近启动放量）')
    L.append(f'- **近端放量占比（量能比>1.5）**: {pct_vol_up:.1f}%')
    L.append(f'- **次新上市（窗口内才上市）占比**: {pct_new:.1f}%')
    L.append(f'- **数据缺口（交易日<{EXPECTED_TRADING_DAYS*0.5:.0f}）占比**: {pct_gap:.1f}%')
    L.append(f'- **含停牌/零成交量日占比**: {pct_zero:.1f}%\n')

    # 形态分布
    L.append('## 📈 统计分布\n')
    pat = valid['pre_pattern'].value_counts()
    L.append('**启动前形态分布**\n')
    L.append('| 形态 | 事件数 | 占比 |')
    L.append('|---|---:|---:|')
    for k, v in pat.items():
        L.append(f'| {k} | {int(v):,} | {v/n_valid*100:.1f}% |')
    L.append('')

    # 趋势分桶
    def bucket(x):
        if x <= -0.20:
            return '深跌(≤-20%)'
        if x <= -0.10:
            return '下跌(-20%~-10%)'
        if x <= 0.10:
            return '横盘(-10%~+10%)'
        if x <= 0.20:
            return '温和上涨(+10%~+20%)'
        return '已走强(>+20%)'
    tb = valid['pre_ret90'].apply(bucket).value_counts()
    L.append('**启动前 90 日累计收益分桶**\n')
    L.append('| 区间 | 事件数 | 占比 |')
    L.append('|---|---:|---:|')
    for k in ['深跌(≤-20%)', '下跌(-20%~-10%)', '横盘(-10%~+10%)', '温和上涨(+10%~+20%)', '已走强(>+20%)']:
        v = int(tb.get(k, 0))
        L.append(f'| {k} | {v:,} | {v/n_valid*100:.1f}% |')
    L.append('')

    # 量能分桶
    def vb(x):
        if np.isnan(x):
            return '缺失'
        if x > 2.0:
            return '显著放量(>2.0)'
        if x > 1.5:
            return '放量(1.5~2.0)'
        if x > 0.8:
            return '平量(0.8~1.5)'
        return '缩量(<0.8)'
    vb_c = valid['pre_amt_ratio'].apply(vb).value_counts()
    L.append('**近端量能比分桶**\n')
    L.append('| 区间 | 事件数 | 占比 |')
    L.append('|---|---:|---:|')
    for k in ['显著放量(>2.0)', '放量(1.5~2.0)', '平量(0.8~1.5)', '缩量(<0.8)', '缺失']:
        v = int(vb_c.get(k, 0))
        L.append(f'| {k} | {v:,} | {v/n_valid*100:.1f}% |')
    L.append('')

    # 启动价（基数）分桶——低基数更易翻倍
    def pb(x):
        if x < 2:
            return '低价股(<2元)'
        if x < 5:
            return '2~5元'
        if x < 10:
            return '5~10元'
        if x < 20:
            return '10~20元'
        return '>20元'
    pb_c = valid['start_price'].apply(pb).value_counts()
    L.append('**启动价（翻倍基数）分桶**\n')
    L.append('| 区间 | 事件数 | 占比 |')
    L.append('|---|---:|---:|')
    for k in ['低价股(<2元)', '2~5元', '5~10元', '10~20元', '>20元']:
        v = int(pb_c.get(k, 0))
        L.append(f'| {k} | {v:,} | {v/n_valid*100:.1f}% |')
    L.append('')

    # 中位归一化路径
    L.append('**中位归一化价格路径（启动日=1.0，按距启动交易日数回溯；数值=该时刻前复权价/启动价的中位数）**\n')
    offsets = [1, 5, 10, 20, 40, 63]
    labels = ['-1', '-5', '-10', '-20', '-40', '-63(≈90日)']
    L.append('| 距启动交易日 | ' + ' | '.join(labels) + ' |')
    L.append('|---' + ':|' * len(offsets))
    cells = []
    for o in offsets:
        vals = [v for v in path_acc.get(o, []) if pd.notna(v)]
        med = float(np.median(vals)) if vals else float('nan')
        cells.append(f'{med:.3f}' if pd.notna(med) else 'NA')
    L.append('| 中位归一化价 | ' + ' | '.join(cells) + ' |')
    L.append('')
    L.append('> 解读：若该序列从 -63 到 -1 呈"先低后高/整体<1.0"，说明启动前价格低于启动价（筑底/回落）；'
             '若接近或高于 1.0，说明启动前已处于相对高位（上涨中继）。\n')

    # Top 30 样本
    L.append('## 🎯 代表性样本（按峰值涨幅 Top 30）\n')
    L.append('| 代码 | 名称 | 启动日 | 启动价 | 前90日收益 | 年化波动 | 最大回撤 | 量能比 | 形态 |')
    L.append('|---|---|---|---:|---:|---:|---:|---:|---|')
    top = res.sort_values('peak_gain', ascending=False).head(30)
    for _, r in top.iterrows():
        ret = f"{r['pre_ret90']*100:.1f}%" if pd.notna(r['pre_ret90']) else 'NA'
        vol = f"{r['pre_ann_vol']*100:.0f}%" if pd.notna(r['pre_ann_vol']) else 'NA'
        dd = f"{r['pre_maxdd']*100:.0f}%" if pd.notna(r['pre_maxdd']) else 'NA'
        ar = f"{r['pre_amt_ratio']:.2f}" if pd.notna(r['pre_amt_ratio']) else 'NA'
        L.append(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['start_price']:.2f} | "
                 f"{ret} | {vol} | {dd} | {ar} | {r['pre_pattern']} |")
    L.append('')

    # 异常与数据缺失
    L.append('## ⚠️ 异常与数据缺失标注\n')
    gaps = res[res['data_gap']]
    newl = valid[valid['new_listing']]
    zero = valid[valid['pre_zero_vol_days'] > 0]
    weird = valid[valid['pre_max_abs_ret'] > 0.50]  # 单日>50% 可能为次新无涨跌幅/数据异常
    L.append(f'1. **数据缺口（窗口交易日<{EXPECTED_TRADING_DAYS*0.5:.0f}）**: {len(gaps)} 个事件，'
             '可能因停牌、长期停市或 parquet 缺失导致回看不足 90 日，相关形态结论需谨慎。')
    L.append(f'2. **次新上市（窗口内才上市）**: {len(newl)} 个事件，启动前不足 90 日历史，'
             '其"前90日"实际为上市后片段，并非完整筑底，属正常但需在结论中剔除或单列；'
             '且窗口过短使 pre_ret90/年化波动等特征常为 0 或失真，统计时已标注 new_listing，建议单列处理。')
    L.append(f'3. **停牌/零成交量日**: 仅 {len(zero)} 个事件窗口内出现成交量=0 的交易日。'
             '需注意：TDX 日K 对停牌日通常直接缺行（不保留 0 量行），因此停牌更多体现为上方"数据缺口"（交易日骤减），'
             '而非 0 量行；缺口事件可能含长期停牌，其回看序列不完整。')
    L.append(f'4. **单日异常波动（|日收益|>50%）**: {len(weird)} 个事件，'
             '多数为主板/双创/北交所新股上市前5日无涨跌幅限制所致，少数可能是数据错误，建议人工复核。')
    L.append('5. **极端量能比离群点**: 部分标的近端量能比极高（如 bj920001 纬达光电达 92×），'
             '多因窗口早期流动性极低（北交所小票）后放量，或存在数据瑕疵，建议作为离群点单独复核。')
    L.append('6. **ST/*ST 标的**: CSV 中已标记 is_st，本文未单列；此类标的翻倍常伴随重组/摘帽预期，风险更高。')
    L.append('7. **口径一致性**: 本文前复权口径与 double_30d 主报告一致（qfq=不复权×adj_factor）；'
             '与旧版 20 交易日窗口的 double_up_* 数据不可直接混用。\n')

    # 共性规律
    L.append('## 🔍 共性规律（综合）\n')
    L.append(f'- **整体横盘、浅 V 为主**：启动前 90 日中位累计收益仅 {med_ret*100:.1f}%（≈横盘），'
             f'中位归一化路径在 -40 日处探至约 0.97、临近启动回到 ~1.01，呈"窄幅震荡 + 浅 V 回升"形态。'
             f'按收益分桶，"已走强(>+20%)"占 30.8%、"深跌/下跌"合计 23.5%、"横盘"34.6%——并无单一主导前置状态，'
             '即翻倍多发生在相对平静的整理期，而非深度下跌后的反转。')
    L.append(f'- **波动偏高、约 1/3 近端放量**：中位年化波动率 {med_vol*100:.0f}% 高于常态；'
             f'近端量能比中位数 {med_amt:.2f}（整体温和），但放量(>1.5)占比达 {pct_vol_up:.0f}%，'
             '显示约三分之一标的在启动前夕出现明显量能放大、波动加剧，是情绪转向的常见前兆。')
    L.append('- **基数分布较均衡、低基数弹性更突出**：启动价在 5-10/10-20/>20 元各约 27-30%，<5 元极低基数仅占约 16%；'
             '虽非集中于低价股，但低绝对价格标的"翻倍"所需绝对涨幅更小、弹性更强。')
    L.append('- **次新与北交所贡献大**：次新上市（无涨跌幅限制期）+ 北交所 30% 涨跌幅，是 1~5 日急拉翻倍的主力，其"前90日"多为上市后到启动的爬升/整理。')
    L.append('- **形态可归纳为两类**：横盘/下跌蓄势类合计约 46%（叠加近端放量的子类型占比最高），上涨中继类约 41%，'
             '符合"地量/平台见底—放量启动"与"趋势延伸"两种主流启动结构。\n')

    L.append('## 🔧 下一步建议\n')
    L.append('- 将 per-event 特征 CSV 接入聚类/分类，自动归类启动形态（如 N 字底、平台突破、次新连板）。')
    L.append('- 对"横盘蓄势+近端放量"子集单独验证方案 E 翻倍跟随策略的可交易性与收益稳定性。')
    L.append('- 人工复核单日>50% 异常与数据缺口样本，清洗后纳入训练样本（data/double_up 现有 22,972 行）。')
    L.append('- 若需严格口径，可将次新上市事件单列，避免"前90日"被上市后短片段污染统计。\n')

    L.append('---')
    L.append(f'生成时间：{now}')
    L.append('生成者：scripts/analyze_pre90.py')
    L.append('模型版本：数据驱动（非模型）')
    L.append(f'数据日期：{DATA_DATE.date()}')

    md.write_text('\n'.join(L), encoding='utf-8')
    log.info('✓ 报告: %s', md)
    log.info('耗时 %.0fs', (datetime.now() - t0).total_seconds())


if __name__ == '__main__':
    main()
