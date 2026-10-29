"""
v3 提前版盘前实时打分（每日开盘前自动跑）
============================================

设计目标：
- 每日 09:00 开盘前自动执行
- 基于最新一日日 K（默认 T-1 收盘）算 Top 5% 候选
- 应用二次过滤（排除 ST / 仙股 / 追高）
- 输出今日 Top 10 买入清单

【集成方式】
1. 手动：python scripts/v3_premarket_top.py
2. systemd 定时：见 LIVE_TRACKING_GUIDE §2.3
3. plan_e_v3_live_track.py --mode premarket（统一入口）

【输出】
- data/plan_e/live_v3/signals/top5_premarket_YYYYMMDD.csv
- data/plan_e/live_v3/signals/top10_buy_YYYYMMDD.csv（过滤后实盘清单）
- data/plan_e/live_v3/daily_reports/premarket_YYYYMMDD.md
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')

from _common import adj_factor_dir, daily_dir, get_limit  # noqa: E402

PARQUET = daily_dir()
ADJ = adj_factor_dir()
ALL_STOCKS = ROOT / 'data/all_a_stocks.json'
SIGNALS_DIR = ROOT / 'data/plan_e/live_v3/signals'
DAILY_DIR = ROOT / 'data/plan_e/live_v3/daily_reports'
SIGNIF_FP = ROOT / 'data/factor_significance_detail.csv'

# Top 10 因子（方向 + 期望 IC，来自 factor_significance 全市场报告）
TOP_FACTORS = {
    'high_low_20d':       ('high', 0.296),
    'volatility_20d':     ('high', 0.266),
    'high_low_5d':        ('high', 0.261),
    'amount_5d_avg':      ('high', 0.168),
    'amount_20d_avg':     ('high', 0.156),
    'limit_up_20d':       ('high', 0.147),
    'days_since_ipo':     ('high', 0.142),
    'limit_up_10d':       ('high', 0.128),
    'lhb_buy_ratio_20d':  ('high', 0.123),
    'lhb_appear_20d':     ('high', 0.123),
}


def calc_stock_factors(df: pd.DataFrame, code: str, ref_date: pd.Timestamp) -> dict | None:
    """对单只股票计算 Top 10 因子（基于 ref_date 前一日收盘）"""
    df = df[df['date'] <= ref_date].sort_values('date').tail(60).reset_index(drop=True)
    if len(df) < 30:
        return None

    close = df['close']
    high = df['high']
    low = df['low']
    vol = df['volume']
    amt = df['amount']
    pct = close.pct_change()
    limit = get_limit(code)

    feat = {
        'code': code,
        'date': df['date'].iloc[-1],
        'close': float(close.iloc[-1]),
        'limit': limit,
        'high_low_20d': (high.rolling(20).max().iloc[-1] - low.rolling(20).min().iloc[-1]) / close.iloc[-1],
        'high_low_5d':  (high.rolling(5).max().iloc[-1] - low.rolling(5).min().iloc[-1]) / close.iloc[-1],
        'volatility_20d': float(pct.rolling(20).std().iloc[-1]),
        'amount_5d_avg': float(amt.rolling(5).mean().iloc[-1]),
        'amount_20d_avg': float(amt.rolling(20).mean().iloc[-1]),
        'limit_up_20d': float((pct >= limit - 0.001).rolling(20).sum().iloc[-1]),
        'limit_up_10d': float((pct >= limit - 0.001).rolling(10).sum().iloc[-1]),
        'days_since_ipo': (ref_date - df['date'].iloc[0]).days,
        # 龙虎榜因子（先用 0 占位，后续可接入）
        'lhb_buy_ratio_20d': 0.0,
        'lhb_appear_20d': 0.0,
        'pct_chg_20d': float(close.iloc[-1] / close.iloc[-20] - 1) if len(close) >= 20 else 0.0,
        'pct_chg_1d': float(pct.iloc[-1]) if not pd.isna(pct.iloc[-1]) else 0.0,
    }
    return feat


def calc_composite_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(0.0, index=df.index)
    for fc, (direction, _) in TOP_FACTORS.items():
        if fc in df.columns and df[fc].std() > 0:
            rank = df[fc].rank(pct=True, na_option='bottom')
            if direction == 'high':
                score += rank.fillna(0.5)
            else:
                score += (1 - rank.fillna(0.5))
    return score / len(TOP_FACTORS)


def apply_filters(df: pd.DataFrame, name_map: dict, top_n: int = 10) -> pd.DataFrame:
    """二次过滤：排除 ST/仙股/追高"""
    df = df.copy()
    df['short_code'] = df['code'].str[2:]
    df['name'] = df['short_code'].map(name_map).fillna('?')
    df['name'] = df['name'].str.replace('　', '').str.strip()

    # 1. 排除 ST/*ST/退
    mask_st = df['name'].str.contains(r'^ST|^\*ST|退$|退市', regex=True, na=False)
    df = df[~mask_st].copy()

    # 2. 排除仙股（close < 2.0）
    df = df[df['close'] >= 2.0]

    # 3. 排除已大涨（20 日 > 30%）
    df = df[df['pct_chg_20d'] <= 0.30]

    # 4. 排除当日涨停（追高）
    df = df[df['pct_chg_1d'] < df['limit'] - 0.001]

    # 5. 排除上市 < 5 日（新新股波动过大）
    df = df[df['days_since_ipo'] >= 5]

    return df.sort_values('score', ascending=False).head(top_n)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=None,
                        help='信号日期 YYYY-MM-DD（默认今天）')
    parser.add_argument('--top-n', type=int, default=10,
                        help='买入清单数量（默认 10）')
    parser.add_argument('--limit-stocks', type=int, default=0,
                        help='限制股票数（测试用）')
    args = parser.parse_args()

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    today = pd.Timestamp(args.date) if args.date else pd.Timestamp.now().normalize()
    # 默认取前一交易日（T-1，因为 T 当日无收盘）
    ref_date = today - pd.Timedelta(days=1)
    # 跳过周末：若 T-1 是周六/周日，再退到周五
    while ref_date.weekday() >= 5:
        ref_date -= pd.Timedelta(days=1)

    print(f'[{datetime.now():%H:%M:%S}] v3 盘前实时打分')
    print(f'  今日: {today.date()} ({today.day_name()})')
    print(f'  参考日: {ref_date.date()} ({ref_date.day_name()})')

    name_map = json.load(open(ALL_STOCKS))
    files = sorted(p for p in PARQUET.glob('*.parquet') if '.bak' not in p.name)
    if args.limit_stocks > 0:
        files = files[:args.limit_stocks]
    print(f'  扫描股票: {len(files)}')

    # 加载全市场日 K 并算因子
    print(f'\n[{datetime.now():%H:%M:%S}] 计算 Top 10 因子...')
    records = []
    for i, fp in enumerate(files):
        code = fp.stem
        try:
            df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
            if df['date'].dtype in ('int64', 'int32'):
                df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
            else:
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
            feat = calc_stock_factors(df, code, ref_date)
            if feat:
                records.append(feat)
        except Exception:
            continue

        if (i + 1) % 1000 == 0:
            print(f'  [{i+1}/{len(files)}] 已处理 {len(records):,}')

    df_all = pd.DataFrame(records)
    print(f'  覆盖: {len(df_all):,} 只')

    # 算 composite score
    df_all['score'] = calc_composite_score(df_all)

    # Top 5%（先粗筛）
    threshold = df_all['score'].quantile(0.95)
    top5 = df_all[df_all['score'] >= threshold].sort_values('score', ascending=False)
    print(f'  Top 5% 候选: {len(top5):,}')

    # 二次过滤 → 买入清单
    buy_list = apply_filters(top5, name_map, top_n=args.top_n)
    print(f'  二次过滤后 Top {args.top_n}: {len(buy_list)}')

    # 写文件
    date_str = today.strftime('%Y%m%d')
    top5_fp = SIGNALS_DIR / f'top5_premarket_{date_str}.csv'
    top5.to_csv(top5_fp, index=False)
    print(f'\n  Top 5% 候选: {top5_fp}')

    buy_fp = SIGNALS_DIR / f'top10_buy_{date_str}.csv'
    buy_list.to_csv(buy_fp, index=False)
    print(f'  今日买入清单: {buy_fp}')

    # 生成盘前报告
    md = []
    md.append(f'# v3 盘前打分报告 - {today.strftime("%Y-%m-%d")}')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%H:%M:%S}  ')
    md.append(f'> 参考日: {ref_date.date()} ({ref_date.day_name()})  ')
    md.append(f'> 覆盖股票: {len(df_all):,} 只  ')
    md.append(f'> Top 5% 候选: {len(top5):,} 只  ')
    md.append(f'> 二次过滤后买入清单: {len(buy_list)} 只  ')
    md.append('')
    md.append('## 一、买入清单（Top 10，二次过滤后）')
    md.append('')
    md.append('| # | 代码 | 名称 | 收盘价 | score | 20日振幅 | 涨停数 |')
    md.append('|:---:|---|---|---:|---:|---:|---:|')
    for i, (_, row) in enumerate(buy_list.iterrows(), 1):
        md.append(f'| {i} | `{row["code"]}` | {row["name"]} | '
                  f'{row["close"]:.2f} | {row["score"]:.3f} | '
                  f'{row["high_low_20d"]*100:.1f}% | {int(row["limit_up_20d"])} |')
    md.append('')
    md.append('## 二、Top 5% 候选池（粗筛）')
    md.append('')
    md.append('| 排名 | 代码 | 名称 | 收盘价 | score |')
    md.append('|:---:|---|---|---:|---:|')
    for i, (_, row) in enumerate(top5.head(30).iterrows(), 1):
        md.append(f'| {i} | `{row["code"]}` | {name_map.get(row["code"][2:], "?")} | '
                  f'{row["close"]:.2f} | {row["score"]:.3f} |')
    md.append('')
    md.append('## 三、二次过滤标准')
    md.append('')
    md.append('- ✅ 排除 ST / *ST / 退')
    md.append('- ✅ 排除仙股（close < 2.0）')
    md.append('- ✅ 排除已大涨（20 日 > 30%）')
    md.append('- ✅ 排除当日涨停')
    md.append('- ✅ 排除新股（< 5 日）')
    md.append('')
    md.append('## 四、关键提示')
    md.append('')
    md.append('- 今日买入清单为**策略输出，不构成投资建议**')
    md.append('- 开盘 09:30:00-09:30:30 限价单（开盘价 ±1%）')
    md.append('- 单只仓位 3%（¥30,000），取整 100 股')
    md.append('- 总仓位上限 70%（¥700,000）')
    md.append('- 严格执行 -10% 止损 / +30% 止盈 / 30 日时间止损')
    md.append('')
    md.append('---')
    md.append('')
    md.append(f'候选池: `{top5_fp.relative_to(ROOT)}`  ')
    md.append(f'买入清单: `{buy_fp.relative_to(ROOT)}`  ')
    md.append('脚本: `scripts/v3_premarket_top.py`  ')

    md_fp = DAILY_DIR / f'premarket_{date_str}.md'
    md_fp.write_text('\n'.join(md), encoding='utf-8')
    print(f'  盘前报告: {md_fp}')

    # 控制台简短输出
    print('\n=== 今日 Top 10 买入清单 ===')
    print(buy_list[['code', 'name', 'close', 'score', 'high_low_20d', 'limit_up_20d']].to_string(index=False))


if __name__ == '__main__':
    main()
