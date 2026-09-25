#!/usr/bin/env python3
"""
Scan A-share stocks for 20-day rolling window with ≥100% price range.

数据来源:
  - tdx-data-feed/data/parquet/daily/*.parquet (raw OHLCV)
  - tdx-data-feed/data/adj_factor/*.parquet (qfq close, 仅 2020+)
  - tdx-data-feed/data/all_a_stocks.json (代码→名称 map)
  - baostock.query_stock_industry / query_stock_basic (行业/IPO)

时间范围: 2001-01-01 ~ 2026-09-24
计算窗口: 连续 20 个交易日 (≈ 1 个自然月)
复权方式: RAW close (未复权, 简单透明; adj_factor 仅 2020+)

算法:
  对每只股票 S:
    对每 i =20..N:
      window = S[i-19 : i+1] # 20 个连续交易日
      low_min_idx = argmin(window.low)
      high_max_idx = argmax(window.high)
      触发条件: low_min_idx ≤ high_max_idx
                AND high[high_max_idx] / low[low_min_idx] - 1 ≥ 1.0
                AND span_days(start, end) ≤ 60

输出:
  <out_prefix>_raw.csv         raw 触发 (含 span_days)
  <out_prefix>_enriched.csv    enriched (含 baostock 元数据)
  <out_prefix>_candidate.csv   候选 (每只股票 1 个最早 trigger)
"""
import pandas as pd
import glob
import json
import time
from pathlib import Path
import argparse
import numpy as np

START_DATE = "2001-01-01"
WINDOW = 20
THRESHOLD = 1.0  # 100%
MAX_SPAN_DAYS = 60  # 20 个交易日 ≈ 30 自然日 + 长假 buffer


def load_meta():
    with open("data/all_a_stocks.json") as f:
        return json.load(f)


def scan_one(symbol, daily_path):
    df = pd.read_parquet(daily_path)
    if df.empty:
        return []
    # date 处理（兼容 int YYYYMMDD / str / Timestamp）
    if df['date'].dtype != 'datetime64[ns]':
        s = df['date'].astype(str)
        mask = s.str.match(r'^\d{8}$')
        d1 = pd.to_datetime(s.where(mask), format='%Y%m%d', errors='coerce')
        d2 = pd.to_datetime(s.where(~mask), errors='coerce')
        df['date'] = d1.fillna(d2)
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    df = df[df['date'] >= START_DATE].reset_index(drop=True)
    df = df[df['low'] > 0].reset_index(drop=True)
    if len(df) < WINDOW + 1:
        return []
    df['rmin'] = df['low'].rolling(WINDOW, min_periods=WINDOW).min()
    df['rmax'] = df['high'].rolling(WINDOW, min_periods=WINDOW).max()
    df['range_pct'] = df['rmax'] / df['rmin'] - 1
    triggers = df[df['range_pct'] >= THRESHOLD]
    if triggers.empty:
        return []
    out = []
    seen = set()
    for idx in triggers.index:
        if idx < WINDOW - 1:
            continue
        win = df.iloc[idx - WINDOW + 1: idx + 1]
        min_row = win.loc[win['low'].idxmin()]
        max_row = win.loc[win['high'].idxmax()]
        if min_row.name > max_row.name:
            continue
        start_dt = min_row['date']
        end_dt = max_row['date']
        span_days = (end_dt - start_dt).days
        if span_days > MAX_SPAN_DAYS:
            continue
        rp = (max_row['high'] / min_row['low']) - 1
        if not np.isfinite(rp) or rp < THRESHOLD:
            continue
        key = (str(min_row['date'].date()), str(max_row['date'].date()))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'symbol': symbol,
            'start_date': min_row['date'].date(),
            'end_date': max_row['date'].date(),
            'start_price': float(min_row['low']),
            'end_price': float(max_row['high']),
            'range_pct': float(rp),
            'win_days': len(win),
            'span_days': int(span_days),
            'win_low_sum_vol': float(win['volume'].sum()),
            'win_amount': float(win['amount'].sum()),
        })
    return out


def fetch_baostock_meta():
    """拉 baostock 行业 + basic 缓存到 /tmp/baostock_{industry,basic}.parquet"""
    import baostock as bs
    bs.login()
    # industry
    rs = bs.query_stock_industry()
    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    industry = pd.DataFrame(rows, columns=rs.fields)
    industry.to_parquet('/tmp/baostock_industry.parquet')
    # basic
    rs = bs.query_stock_basic()
    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    basic = pd.DataFrame(rows, columns=rs.fields)
    basic.to_parquet('/tmp/baostock_basic.parquet')
    bs.logout()
    return industry, basic


def enrich(raw_df, name_map):
    """合并 baostock 元数据 + name + 推断字段"""
    industry = pd.read_parquet('/tmp/baostock_industry.parquet')
    basic = pd.read_parquet('/tmp/baostock_basic.parquet')

    def parquet_to_baostock(s):
        if s.startswith('sh'): return 'sh.' + s[2:]
        if s.startswith('sz'): return 'sz.' + s[2:]
        if s.startswith('bj'): return 'bj.' + s[2:]
        return s

    df = raw_df.copy()
    df['bs_code'] = df['symbol'].apply(parquet_to_baostock)
    df = df.merge(industry[['code', 'industry']], left_on='bs_code', right_on='code', how='left')
    df = df.merge(basic[['code', 'code_name', 'ipoDate', 'status', 'type']],
                  left_on='bs_code', right_on='code', how='left', suffixes=('', '_basic'))

    def get_name(s):
        s_short = s[3:] if s.startswith(('sh', 'sz', 'bj')) else s
        return name_map.get(s_short, '')

    df['stock_name'] = df['bs_code'].apply(get_name)
    df['is_st'] = df['stock_name'].fillna('').str.startswith(('ST', '*ST', 'st', '*st'))
    df['ipo_dt'] = pd.to_datetime(df['ipoDate'], errors='coerce')
    df['start_dt'] = pd.to_datetime(df['start_date'])
    df['days_since_ipo'] = (df['start_dt'] - df['ipo_dt']).dt.days
    df['is_new'] = (df['days_since_ipo'] >= 0) & (df['days_since_ipo'] <= 60)
    df['has_zero_vol'] = (df['win_low_sum_vol'] == 0)

    def market(s):
        if s.startswith('sh'): return 'SH'
        if s.startswith('sz'): return 'SZ'
        if s.startswith('bj'): return 'BJ'
        return '?'
    df['market'] = df['symbol'].apply(market)
    df['avg_amount'] = df['win_amount'] / 20.0

    cols = ['symbol', 'stock_name', 'industry', 'market',
            'start_date', 'end_date', 'start_price', 'end_price',
            'range_pct', 'win_days', 'span_days',
            'win_low_sum_vol', 'win_amount', 'avg_amount',
            'is_st', 'is_new', 'has_zero_vol',
            'ipo_dt', 'days_since_ipo', 'status']
    df = df[[c for c in cols if c in df.columns]]
    return df.sort_values(['start_date', 'symbol']).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None, help='限 N 只股票测试')
    ap.add_argument('--out-prefix', default='v5/audit/double_triggers',
                    help='输出文件前缀 (生成 _raw.csv / _enriched.csv / _candidate.csv)')
    ap.add_argument('--no-enrich', action='store_true', help='只跑 scan, 不 enrich')
    args = ap.parse_args()

    out_raw = f'{args.out_prefix}_raw.csv'
    out_enr = f'{args.out_prefix}_enriched.csv'
    out_cand = f'{args.out_prefix}_candidate.csv'

    files = sorted(glob.glob('data/parquet/daily/*.parquet'))
    if args.limit:
        files = files[:args.limit]

    name_map = load_meta()
    print(f"开始扫描 {len(files)} 只股票 (window={WINDOW}, threshold={THRESHOLD*100}%, span_days≤{MAX_SPAN_DAYS})")

    t0 = time.time()
    all_triggers = []
    for i, f in enumerate(files):
        symbol = Path(f).stem
        try:
            ts = scan_one(symbol, f)
            all_triggers.extend(ts)
        except Exception as e:
            pass
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  [{i+1}/{len(files)}] {elapsed:.0f}s, triggers={len(all_triggers)}")

    raw_df = pd.DataFrame(all_triggers)
    if not raw_df.empty:
        raw_df = raw_df.sort_values(['symbol', 'start_date']).reset_index(drop=True)
    raw_df.to_csv(out_raw, index=False)
    elapsed = time.time() - t0
    print(f"\n完成: {len(raw_df)} triggers (含 span≤{MAX_SPAN_DAYS} 约束), 耗时 {elapsed:.0f}s, 输出 {out_raw}")

    if args.no_enrich or raw_df.empty:
        return

    # enrich
    print("\n[enrich] 拉 baostock 元数据...")
    try:
        fetch_baostock_meta()
    except Exception as e:
        print(f"  ⚠ baostock 失败: {e}, 跳过 enrich")
        return

    enriched = enrich(raw_df, name_map)
    enriched.to_csv(out_enr, index=False)
    print(f"✓ enriched: {len(enriched)} triggers → {out_enr}")

    # candidate: 每个股票最早 trigger
    first = (enriched.sort_values(['symbol', 'start_date'])
             .drop_duplicates('symbol', keep='first')
             .reset_index(drop=True))
    first.to_csv(out_cand, index=False)
    print(f"✓ candidate: {len(first)} unique stocks → {out_cand}")

    # 快速统计
    print(f"\n=== 摘要 ===")
    print(f"  总 trigger: {len(enriched)}")
    print(f"  候选股票: {len(first)}")
    print(f"  按市场: {enriched['market'].value_counts().to_dict()}")
    print(f"  ST: {enriched['is_st'].sum()}, 次新股: {enriched['is_new'].sum()}")
    print(f"  range_pct top-1: {enriched['range_pct'].max():.2%} ({enriched.loc[enriched['range_pct'].idxmax(), 'symbol']})")


if __name__ == "__main__":
    main()