"""过去5年（30 个自然日）内前复权价翻倍的股票筛选。

数据来源：
  - data/parquet/daily/{prefix}{code}.parquet: 不复权日K（open/high/low/close/...）
  - data/adj_factor/{code}.parquet: 复权因子 adj_factor
  - 前复权价格 qfq = 不复权价 × adj_factor（close/high 均如此处理）

翻倍定义：
  - 以某交易日 t 的前复权收盘价 P_t 为起点；
  - 在 [t, t+30 自然日] 窗口内（含两端交易日），若存在交易日 d 使
    前复权收盘价 P_d ≥ 2 × P_t（即涨幅 ≥ 100%），记为一次翻倍事件；
  - 起点价 = P_t；最高价 = 该区间内前复权最高价（high×adj_factor）的最大值；
  - 结束日 = 前复权收盘价首次达 2× 的交易日；峰值日 = 区间内前复权最高价所在日。
  - 同一股票内的重叠事件会合并为非重叠区间（找到一个事件后跳到结束日之后继续扫描）。

时间范围：START_DATE 起（过去5年）至数据最大交易日（2026-09-30）。

输出：
  - data/double_up/double_30d_2021_2026.csv
  - data/double_up/double_30d_2021_2026_report_YYYYMMDD.md
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from _common import adj_factor_dir, daily_dir, universe_path

PARQUET = daily_dir()
ADJ = adj_factor_dir()
NAME_MAP_PATH = universe_path()
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')

WINDOW_CAL_DAYS = 30          # 自然日窗口
RATIO = 2.0                   # 翻倍 = 达到 2 倍
START_DATE = pd.Timestamp('2021-10-05')   # 过去5年起算（含）
DATA_END = pd.Timestamp('2026-09-30')     # 数据最大交易日（元数据确认）


def _to_dt(series: pd.Series) -> pd.Series:
    if series.dtype in ('int64', 'int32'):
        return pd.to_datetime(series.astype(str), format='%Y%m%d')
    return pd.to_datetime(series)


def load_qfq(code: str) -> pd.DataFrame | None:
    """加载某股票的前复权 close/high 序列。"""
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return None
    short = re.sub(r'^(sh|sz|bj)', '', code)
    adj_fp = ADJ / f'{short}.parquet'
    if not adj_fp.exists():
        return None
    try:
        df = pd.read_parquet(fp, columns=['date', 'close', 'high'])
        adj = pd.read_parquet(adj_fp, columns=['date', 'adj_factor'])
    except Exception:
        return None
    df['date'] = _to_dt(df['date'])
    adj['date'] = _to_dt(adj['date'])
    df = df.merge(adj, on='date', how='left').sort_values('date').reset_index(drop=True)
    df['adj_factor'] = df['adj_factor'].ffill().bfill().fillna(1.0)
    df['qfq_close'] = df['close'] * df['adj_factor']
    df['qfq_high'] = df['high'] * df['adj_factor']
    return df[['date', 'qfq_close', 'qfq_high']]


def find_episodes(code: str, name: str, df: pd.DataFrame) -> list[dict]:
    """检测非重叠的 30 自然日翻倍事件。"""
    df = df[df['date'] >= START_DATE].reset_index(drop=True)
    n = len(df)
    if n == 0:
        return []
    dts = df['date'].values                 # datetime64[ns]
    qc = df['qfq_close'].values.astype('float64')
    qh = df['qfq_high'].values.astype('float64')

    is_st = name.startswith('*ST') or name.startswith('ST')
    episodes: list[dict] = []
    i = 0
    while i < n:
        if dts[i] < np.datetime64(START_DATE):
            i += 1
            continue
        s = qc[i]
        if not (s > 0) or np.isnan(s):
            i += 1
            continue
        win_end = dts[i] + np.timedelta64(WINDOW_CAL_DAYS, 'D')
        j_max = int(np.searchsorted(dts, win_end, side='right')) - 1
        if j_max <= i:
            i += 1
            continue
        target = RATIO * s
        found = -1
        for j in range(i + 1, j_max + 1):
            if qc[j] >= target:
                found = j
                break
        if found == -1:
            i += 1
            continue
        seg = qh[i:found + 1]
        k = int(np.argmax(seg))
        peak_idx = i + k
        episodes.append({
            'code': code,
            'name': name,
            'start_date': pd.Timestamp(dts[i]).strftime('%Y-%m-%d'),
            'end_date': pd.Timestamp(dts[found]).strftime('%Y-%m-%d'),
            'peak_date': pd.Timestamp(dts[peak_idx]).strftime('%Y-%m-%d'),
            'start_price': round(float(s), 4),
            'peak_price': round(float(seg[k]), 4),
            'end_price': round(float(qc[found]), 4),
            'pct_gain': round((qc[found] / s - 1) * 100, 1),
            'peak_gain': round((seg[k] / s - 1) * 100, 1),
            'cal_days': int((pd.Timestamp(dts[found]) - pd.Timestamp(dts[i])).days),
            'is_st': is_st,
        })
        i = found + 1   # 跳到本次事件之后，避免重叠
    return episodes


def main() -> None:
    t0 = datetime.now()
    print('=== 过去5年 30自然日 前复权翻倍筛选 ===')
    print(f'  窗口: {WINDOW_CAL_DAYS} 自然日 | 翻倍阈值: ≥{int((RATIO-1)*100)}%')
    print(f'  时间范围: {START_DATE.date()} ~ {DATA_END.date()}')
    print('  价格类型: 前复权收盘价 / 前复权最高价 (qfq=不复权价×复权因子)')

    with open(NAME_MAP_PATH, encoding='utf-8') as f:
        name_map = json.load(f)

    files = sorted(p for p in PARQUET.glob('*.parquet') if '.bak' not in p.name)
    short_to_codes: dict[str, list[str]] = {}
    for f in files:
        short = re.sub(r'^(sh|sz|bj)', '', f.stem)
        short_to_codes.setdefault(short, []).append(f.stem)
    adj_files = sorted(p for p in ADJ.glob('*.parquet') if '.bak' not in p.name)
    print(f'  daily parquet: {len(files)} | adj_factor: {len(adj_files)}')

    all_eps: list[dict] = []
    scanned = 0
    for adj_fp in adj_files:
        short = adj_fp.stem
        codes = short_to_codes.get(short)
        if not codes:
            continue
        code = codes[0]
        name = name_map.get(short, '?').replace('　', '').strip()
        df = load_qfq(code)
        if df is None or len(df) == 0:
            continue
        eps = find_episodes(code, name, df)
        all_eps.extend(eps)
        scanned += 1
        if scanned % 500 == 0:
            el = (datetime.now() - t0).total_seconds()
            print(f'  [{scanned}] {el:.0f}s, episodes={len(all_eps)}')

    print(f'\n扫描完成: {scanned} 只股, {len(all_eps)} 个翻倍事件')
    print(f'耗时: {(datetime.now() - t0).total_seconds():.0f}s')

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ['code', 'name', 'start_date', 'end_date', 'peak_date',
            'start_price', 'peak_price', 'end_price',
            'pct_gain', 'peak_gain', 'cal_days', 'is_st']
    res = pd.DataFrame(all_eps, columns=cols)
    if len(res) > 0:
        res = res.sort_values(['start_date', 'code']).reset_index(drop=True)
        res['year'] = pd.to_datetime(res['start_date']).dt.year
    csv_path = OUT_DIR / 'double_30d_2021_2026.csv'
    res.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f'✓ CSV: {csv_path} ({len(res)} 行)')

    _write_report(res, t0)


def _write_report(res: pd.DataFrame, t0: datetime) -> None:
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    md = OUT_DIR / f'double_30d_2021_2026_report_{ts}.md'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    L: list[str] = []
    L.append('# 过去5年 30自然日内前复权价翻倍股票筛选\n')
    L.append(f'> 生成时间: {now}')
    L.append('> 窗口: 30 个自然日 | 翻倍阈值: 前复权收盘价 ≥ 起点 2 倍（涨幅≥100%）')
    L.append(f'> 时间范围: {START_DATE.date()} ~ {DATA_END.date()}')
    L.append('> 价格类型: 前复权价（qfq = 不复权价 × 复权因子）；起点价用前复权收盘价，最高价用前复权最高价')
    L.append('> 数据来源: data/parquet/daily + data/adj_factor（全市场 A 股，含沪/深/北交所）\n')

    n_eps = len(res)
    n_stocks = res['code'].nunique() if n_eps else 0
    st_eps = int(res['is_st'].sum()) if n_eps else 0
    L.append('## 📊 核心指标\n')
    L.append(f'- **翻倍事件总数**: {n_eps:,}（非重叠区间，按股票×事件计）')
    L.append(f'- **涉及股票数**: {n_stocks:,} 只')
    L.append(f'- **其中 ST/*ST**: {st_eps} 个事件')
    L.append(f'- **时间窗口**: {START_DATE.date()} ~ {DATA_END.date()}（过去5年）')
    L.append('- **价格口径**: 前复权收盘价 / 前复权最高价')
    L.append(f'- **分析耗时**: {(datetime.now() - t0).total_seconds():.0f}s\n')

    if n_eps:
        L.append('## 🎯 Top 30 明细（按峰值涨幅排序）\n')
        L.append('| 代码 | 名称 | 起点日 | 结束日 | 峰值日 | 起始价 | 最高价 | 翻倍涨幅 | 峰值涨幅 | 自然日 |')
        L.append('|---|---|---|---|---|---:|---:|---:|---:|---:|')
        top = res.sort_values('peak_gain', ascending=False).head(30)
        for _, r in top.iterrows():
            L.append(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['end_date']} | {r['peak_date']} | "
                     f"{r['start_price']:.2f} | {r['peak_price']:.2f} | {r['pct_gain']:.1f}% | {r['peak_gain']:.1f}% | {int(r['cal_days'])} |")
        L.append('')

        L.append('## 📈 统计分布\n')
        by_year = res.groupby('year').size().sort_index()
        L.append('**按起点年份分布**\n')
        L.append('| 年份 | 翻倍事件数 |')
        L.append('|---|---:|')
        for y, c in by_year.items():
            L.append(f'| {y} | {int(c):,} |')
        L.append('')
        # 市场分布（前缀）
        mkt = res['code'].str[:2].map({'sh': '沪市', 'sz': '深市', 'bj': '北交所'}).fillna('其他')
        bm = mkt.value_counts()
        L.append('**按市场分布**\n')
        L.append('| 市场 | 翻倍事件数 |')
        L.append('|---|---:|')
        for k, v in bm.items():
            L.append(f'| {k} | {int(v):,} |')
        L.append('')
        # 翻倍所用自然日分布
        bd = pd.cut(res['cal_days'], [0, 5, 10, 15, 20, 25, 30], include_lowest=True)
        L.append('**翻倍所用自然日区间分布**\n')
        L.append('| 自然日区间 | 事件数 |')
        L.append('|---|---:|')
        for iv, c in bd.value_counts().sort_index().items():
            L.append(f'| {iv} | {int(c):,} |')
        L.append('')

    L.append('## ⚠️ 风险提示\n')
    L.append('1. 历史翻倍不代表未来收益，高弹性个股波动与回撤极大。')
    L.append('2. 前复权价已剔除送转股影响，但除权除息日、停牌复牌跳空及数据异常仍可能制造伪翻倍，结论需人工复核。')
    L.append('3. 翻倍多出现在题材炒作/小市值/次新股，流动性与退市风险高，北交所（920XXX）流动性尤低、滑点可能放大。')
    L.append('4. 本筛选仅基于价格序列，未考虑基本面与估值，不构成投资建议。')
    L.append('5. ST/*ST 个股已被标记，此类标的交易受限、风险更高。\n')

    L.append('## 🔧 下一步建议\n')
    L.append('- 对 Top 样本结合 `scripts/top10_deep_dive.py` 做单票深度复盘（量能、涨停板数、题材）。')
    L.append('- 将满足条件个股接入方案 E 的翻倍跟随策略回测（`scripts/plan_e_backtest.py`）验证可交易性。')
    L.append('- 如需严格"可交易"口径，可叠加日均成交额阈值过滤小流动性标的。\n')

    L.append('---')
    L.append(f'生成时间：{now}')
    L.append('生成者：scripts/find_double_30d.py')
    L.append('模型版本：数据驱动（非模型）')
    L.append(f'数据日期：{DATA_END.date()}')

    md.write_text('\n'.join(L), encoding='utf-8')
    print(f'✓ 报告: {md}')


if __name__ == '__main__':
    main()
