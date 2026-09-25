#!/usr/bin/env python3
"""
分钟线全量同步
  --mode core-full   : 核心 300 标的全历史（按月翻页，断点续传）
  --mode market-60d  : 全市场近 60 个交易日（默认 5 分钟）
  --period 5|1       : 周期（默认 5）
  --start YYYY-MM-DD : core-full 历史起点（默认 2001-01-01）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, '/home/jiuben/tdx-data-feed')
sys.path.insert(0, '/home/jiuben/tdx-data-feed/tdxfeed')

from kline import fetch_history, save_parquet

OUT = '/home/jiuben/tdx-data-feed/data/parquet'
CORE_FILE = '/home/jiuben/tdx-data-feed/core_stocks.json'


def get_core_symbols():
    with open(CORE_FILE, encoding='utf-8') as f:
        return [s['symbol'] for s in json.load(f)['stocks']]


def core_full(period, start, limit=None):
    symbols = get_core_symbols()
    if limit:
        symbols = symbols[:limit]
    print(f"[core-full] 核心 {len(symbols)} 只, period={period}, start={start}", flush=True)
    t0 = time.time()
    ok = skip = fail = 0
    for i, sym in enumerate(symbols):
        out_path = os.path.join(OUT, f'min{period}', sym + '.parquet')
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skip += 1
            continue
        try:
            df = fetch_history(sym, period=period, start=start)
            if df is not None and len(df):
                save_parquet(sym, df, period)
                n = len(df)
                ok += 1
                el = time.time() - t0
                print(f"  [{i+1}/{len(symbols)}] {sym}: {n} 根 -> min{period}/{sym}.parquet (已用 {el:.0f}s)", flush=True)
            else:
                fail += 1
                print(f"  [{i+1}/{len(symbols)}] {sym}: 空数据", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [FAIL] {sym}: {str(e)[:100]}", flush=True)
    print(f"[core-full] 完成: ok={ok} fail={fail} skip={skip} 总耗时 {time.time()-t0:.0f}s", flush=True)


def market_60d(period, limit=None):
    """全市场近 60 交易日（≈3 个月分钟线，按月翻页拉 3 段）"""
    end = datetime.now()
    start = (end - timedelta(days=95)).strftime('%Y-%m-%d')  # 覆盖 60 交易日
    end_s = end.strftime('%Y-%m-%d')
    # 从全部 .day 文件收集标的
    symbols = []
    for pref in ('sh', 'sz'):
        d = os.path.join('/home/workbuddy/vipdoc', pref, 'lday')
        if os.path.isdir(d):
            symbols += [f[:-4] for f in os.listdir(d) if f.endswith('.day')]
    symbols.sort()
    if limit:
        symbols = symbols[:limit]
    print(f"[market-60d] 全市场 {len(symbols)} 只, period={period}, {start}~{end_s}", flush=True)
    t0 = time.time()
    ok = skip = fail = 0
    for i, sym in enumerate(symbols):
        out_path = os.path.join(OUT, f'min{period}', sym + '.parquet')
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skip += 1
            continue
        try:
            df = fetch_history(sym, period=period, start=start, end=end_s)
            if df is not None and len(df):
                save_parquet(sym, df, period)
                ok += 1
            else:
                fail += 1
            if (i + 1) % 200 == 0 or i == len(symbols) - 1:
                el = time.time() - t0
                print(f"  [{i+1}/{len(symbols)}] ok={ok} fail={fail} skip={skip} 已用 {el:.0f}s", flush=True)
        except Exception as e:
            fail += 1
            if (i + 1) % 200 == 0:
                print(f"  [{i+1}] {sym} FAIL {str(e)[:80]}", flush=True)
    print(f"[market-60d] 完成: ok={ok} fail={fail} skip={skip} 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', required=True, choices=['core-full', 'market-60d'])
    ap.add_argument('--period', default='5', choices=['5', '1'])
    ap.add_argument('--start', default='2001-01-01')
    ap.add_argument('--limit', type=int, default=None, help='只跑前 N 只（试点用）')
    args = ap.parse_args()
    if args.mode == 'core-full':
        core_full(args.period, args.start, args.limit)
    else:
        market_60d(args.period, args.limit)
