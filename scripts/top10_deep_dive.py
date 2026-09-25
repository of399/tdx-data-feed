"""Top 10 妖股深度复盘
- 翻倍样本分析
- 涨停节奏
- 成交量峰值
- 行业概念（akshare stock_zyjs_ths）
"""
import re
import socket
import time
import warnings
from datetime import datetime
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

socket.setdefaulttimeout(30)
warnings.filterwarnings('ignore')

PARQUET = Path('/home/jiuben/tdx-data-feed/data/parquet/daily')
VAULT_DIR = Path('/home/jiuben/StockVault/01-标的')
ALL_STOCKS = Path('/home/jiuben/tdx-data-feed/data/all_a_stocks.json')
V1_DETAIL = Path('/home/jiuben/tdx-data-feed/data/double_up/double_up_2020_2026_detail.csv')
V1_BY_CODE = Path('/home/jiuben/tdx-data-feed/data/double_up/double_up_2020_2026_by_code.csv')
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')

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


def get_industry(code: str) -> dict:
    short = re.sub(r'^(sh|sz|bj)', '', code)
    try:
        df = ak.stock_zyjs_ths(symbol=short)
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            return {
                'main': str(row.get('主营业务', ''))[:200],
                'product': str(row.get('产品类型', ''))[:100]
            }
    except Exception as e:
        return {'main': f'(获取失败: {type(e).__name__})', 'product': ''}
    return {'main': 'N/A', 'product': ''}


def get_vault_tags(code: str) -> list:
    md = list(VAULT_DIR.glob(f'{re.sub(r"^(sh|sz|bj)", "", code)}*.md'))
    if not md:
        return []
    for f in md:
        with f.open(encoding='utf-8') as fp:
            for line in fp:
                if line.startswith('tags:'):
                    tags = re.findall(r'\[([^\]]+)\]', line)
                    if tags:
                        return [t.strip() for t in tags[0].split(',')]
    return []


def analyze_one(code: str, hits_df: pd.DataFrame) -> dict:
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return {'error': f'parquet 不存在: {code}'}

    df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    if df['date'].dtype in ('int64', 'int32'):
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    limit = get_limit(code)
    df['pct_change'] = df['close'].pct_change()
    df['is_limit_up'] = df['pct_change'] >= (limit - 0.001)

    streak_groups = (df['is_limit_up'] != df['is_limit_up'].shift()).cumsum()
    streaks = df[df['is_limit_up']].groupby(streak_groups[df['is_limit_up']]).agg(
        start_date=('date', 'first'),
        end_date=('date', 'last'),
        n_days=('date', 'count'),
        cum_pct=('pct_change', lambda x: (1 + x).prod() - 1),
    ).reset_index(drop=True)
    max_streak = int(streaks['n_days'].max()) if len(streaks) else 0
    max_streak_pct = float(streaks.loc[streaks['n_days'].idxmax(), 'cum_pct'] * 100) if len(streaks) else 0

    peak_amount_idx = df['amount'].idxmax()
    peak_amount_row = df.iloc[peak_amount_idx]
    avg_amount = df['amount'].mean()
    peak_amount_ratio = peak_amount_row['amount'] / avg_amount if avg_amount > 0 else 0

    hit_dates = pd.Series(pd.to_datetime(sorted(hits_df['start_date'])))
    if len(hit_dates) >= 2:
        intervals = [(hit_dates.iloc[i+1] - hit_dates.iloc[i]).days for i in range(len(hit_dates) - 1)]
        avg_interval = float(np.mean(intervals))
        min_interval = int(min(intervals))
    else:
        avg_interval = 0
        min_interval = 0

    return {
        'total_days': len(df),
        'date_range': f"{df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}",
        'max_streak': max_streak,
        'max_streak_pct': max_streak_pct,
        'limit_up_count': int(df['is_limit_up'].sum()),
        'peak_amount': float(peak_amount_row['amount']),
        'peak_amount_date': peak_amount_row['date'].strftime('%Y-%m-%d'),
        'peak_amount_ratio': peak_amount_ratio,
        'avg_interval_days': avg_interval,
        'min_interval_days': min_interval,
        'streaks_table': streaks.sort_values('n_days', ascending=False).head(5),
    }


def main():
    t0 = datetime.now()
    print('=== Top 10 妖股深度复盘 ===')

    by_code = pd.read_csv(V1_BY_CODE, dtype={'code': str})
    top10 = by_code.head(10).reset_index(drop=True)
    print(f'Top 10: {top10["code"].tolist()}')

    v1_detail = pd.read_csv(V1_DETAIL, dtype={'code': str})

    md_path = OUT_DIR / f'top10_deep_dive_{datetime.now().strftime("%Y%m%d_%H%M")}.md'
    with md_path.open('w', encoding='utf-8') as f:
        f.write('# Top 10 妖股深度复盘\n\n')
        f.write(f'> 生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write('> 数据: v1 (不复权) 全 A 8035 只 / 22972 翻倍样本\n')
        f.write('> 时间窗口: 2020-01-01 ~ 2026-09-22\n\n')
        f.write('## Top 10 概览\n\n')
        f.write('| 排名 | 代码 | 名称 | 翻倍次数 | 涉及年份 | 最大涨幅 | 最多涨停 |\n')
        f.write('|---|---|---|---|---|---|---|\n')
        for _, r in top10.iterrows():
            f.write(f'| {int(r["rank"])} | {r["code"]} | {r["name"]} | {int(r["hit_count"])} | '
                    f'{r["years"]} | {r["max_pct"]:.1f}% | {int(r["max_limit_up"])} |\n')
        f.write('\n')

    for i, row in top10.iterrows():
        code = row['code']
        name = row['name']
        print(f'\n[{i+1}/10] {code} {name}...')

        ind = get_industry(code)
        vault_tags = get_vault_tags(code)
        hits = v1_detail[v1_detail['code'] == code].copy()
        ana = analyze_one(code, hits)

        with md_path.open('a', encoding='utf-8') as f:
            f.write('---\n\n')
            f.write(f'## {i+1}. {code} {name} · {int(row["hit_count"])} 次翻倍\n\n')
            f.write('### 基本信息\n\n')
            f.write(f'- **代码**: {code}\n')
            f.write(f'- **名称**: {name}\n')
            market = ("北交所" if code.startswith("bj")
                      else ("创业板" if code.startswith("sz3") or code.startswith("sh688")
                            else "沪深主板"))
            f.write(f'- **市场**: {market}\n')
            f.write(f'- **Vault tags**: {", ".join(vault_tags) if vault_tags else "无"}\n')
            f.write(f'- **数据范围**: {ana.get("date_range", "N/A")} ({ana.get("total_days", "N/A")} 个交易日)\n\n')

            f.write('### 主营业务\n\n')
            f.write(f'> {ind["main"]}\n\n')
            if ind['product'] and ind['product'] != 'nan':
                f.write(f'**产品类型**: {ind["product"]}\n\n')

            f.write('### 翻倍样本特征\n\n')
            f.write('| 指标 | 数值 |\n|---|---|\n')
            f.write(f'| 翻倍次数 | {int(row["hit_count"])} |\n')
            f.write(f'| 涉及年份 | {row["years"]} |\n')
            f.write(f'| 最大涨幅 | {row["max_pct"]:.1f}% |\n')
            f.write(f'| 单次最多涨停数 | {int(row["max_limit_up"])} |\n')
            f.write(f'| 累计成交额 | {row["total_amount"]/1e8:.1f} 亿 |\n\n')

            f.write('### 涨停节奏\n\n')
            f.write('| 指标 | 数值 |\n|---|---|\n')
            f.write(f'| 涨停日总数 | {ana.get("limit_up_count", 0)} |\n')
            f.write(f'| **最长连板数** | **{int(ana.get("max_streak", 0))} 连板** |\n')
            f.write(f'| 最长连板累计涨幅 | {ana.get("max_streak_pct", 0):.1f}% |\n')
            f.write(f'| 翻倍间平均间隔 | {ana.get("avg_interval_days", 0):.0f} 天 |\n')
            f.write(f'| 翻倍间最短间隔 | {int(ana.get("min_interval_days", 0))} 天 |\n\n')

            streaks = ana.get('streaks_table')
            if streaks is not None and len(streaks) > 0:
                f.write('**连板事件 Top 5**（按连板天数）:\n\n')
                f.write('| 起始日 | 结束日 | 连板数 | 累计涨幅 |\n|---|---|---|---|\n')
                for _, s in streaks.head(5).iterrows():
                    f.write(f'| {s["start_date"].strftime("%Y-%m-%d")} | '
                            f'{s["end_date"].strftime("%Y-%m-%d")} | '
                            f'{int(s["n_days"])} | {s["cum_pct"]*100:.1f}% |\n')
                f.write('\n')

            f.write('### 成交量峰值\n\n')
            f.write('| 指标 | 数值 |\n|---|---|\n')
            f.write(f'| 峰值成交额 | {ana.get("peak_amount", 0)/1e8:.2f} 亿 |\n')
            f.write(f'| 峰值日期 | {ana.get("peak_amount_date", "N/A")} |\n')
            f.write(f'| 峰值 / 平均 | {ana.get("peak_amount_ratio", 0):.2f}x |\n\n')

            f.write('### 翻倍明细（最近 8 次）\n\n')
            hits_sorted = hits.sort_values('start_date', ascending=False).head(8)
            f.write('| 起始日 | 峰值日 | 天数 | 起始价 | 峰值价 | 涨幅 | 涨停数 | 成交额(亿) |\n')
            f.write('|---|---|---|---|---|---|---|---|\n')
            for _, h in hits_sorted.iterrows():
                f.write(f'| {h["start_date"]} | {h["peak_date"]} | {int(h["days_to_peak"])} | '
                        f'{h["start_close"]:.2f} | {h["peak_close"]:.2f} | '
                        f'{h["pct_change"]:.1f}% | {int(h["limit_up_count"])} | '
                        f'{h["window_amount"]/1e8:.1f} |\n')
            f.write('\n')

        time.sleep(0.5)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f'\n=== 完成 === 耗时 {elapsed:.0f}s')
    print(f'报告: {md_path}')


if __name__ == '__main__':
    main()
