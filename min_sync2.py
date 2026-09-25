#!/usr/bin/env python3
"""
分钟线同步 v2（Sina 稳定源 + Eastmoney 全历史断点续传）
  --mode market-60d  : 全市场近60交易日 → Sina scale=15 (min15/)
  --mode core-1y     : 核心300近1年      → Sina scale=60 (min60/)
  --mode core-full-em: 核心300全历史     → Eastmoney 按月翻页 (min5/)，断点续传
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, '/home/jiuben/tdx-data-feed')
sys.path.insert(0, '/home/jiuben/tdx-data-feed/tdxfeed')

from kline import fetch_history, fetch_sina, save_parquet, save_rows_parquet

OUT = '/home/jiuben/tdx-data-feed/data/parquet'
CORE_FILE = '/home/jiuben/tdx-data-feed/core_stocks.json'


def get_core_symbols():
    with open(CORE_FILE, encoding='utf-8') as f:
        return [s['symbol'] for s in json.load(f)['stocks']]


def market_60d(limit=None):
    """全市场近60交易日（Sina 15min，稳定源）"""
    symbols = []
    for pref in ('sh', 'sz'):
        d = os.path.join('/home/workbuddy/vipdoc', pref, 'lday')
        if os.path.isdir(d):
            symbols += [f[:-4] for f in os.listdir(d) if f.endswith('.day')]
    symbols.sort()
    if limit:
        symbols = symbols[:limit]
    print(f"[market-60d] 全市场 {len(symbols)} 只, Sina 15min", flush=True)
    t0 = time.time()
    ok = skip = fail = 0
    for i, sym in enumerate(symbols):
        out_path = os.path.join(OUT, 'min15', sym + '.parquet')
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skip += 1
            continue
        rows = fetch_sina(sym, scale=15)
        if rows:
            save_rows_parquet(sym, rows, 15)
            ok += 1
        else:
            fail += 1
        if (i + 1) % 100 == 0 or i == len(symbols) - 1:
            el = time.time() - t0
            print(f"  [{i+1}/{len(symbols)}] ok={ok} fail={fail} skip={skip} 已用 {el:.0f}s", flush=True)
        time.sleep(0.3)
    print(f"[market-60d] 完成: ok={ok} fail={fail} skip={skip} 总耗时 {time.time()-t0:.0f}s", flush=True)


def core_1y(limit=None):
    """核心300近1年（Sina 60min）"""
    symbols = get_core_symbols()
    if limit:
        symbols = symbols[:limit]
    print(f"[core-1y] 核心 {len(symbols)} 只, Sina 60min", flush=True)
    t0 = time.time()
    ok = skip = fail = 0
    for i, sym in enumerate(symbols):
        out_path = os.path.join(OUT, 'min60', sym + '.parquet')
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skip += 1
            continue
        rows = fetch_sina(sym, scale=60)
        if rows:
            save_rows_parquet(sym, rows, 60)
            ok += 1
        else:
            fail += 1
        if (i + 1) % 50 == 0 or i == len(symbols) - 1:
            el = time.time() - t0
            print(f"  [{i+1}/{len(symbols)}] ok={ok} fail={fail} skip={skip} 已用 {el:.0f}s", flush=True)
        time.sleep(0.3)
    print(f"[core-1y] 完成: ok={ok} fail={fail} skip={skip} 总耗时 {time.time()-t0:.0f}s", flush=True)


def core_full_em(period='5', start='2001-01-01', limit=None,
                  consecutive_fail_max=5, max_total_minutes=30):
    """核心300全历史（Eastmoney，断点续传 + fail-fast）

    Fail-fast 机制：
    - 连续 consecutive_fail_max 段失败立即退出（默认 5）
    - 单次运行超过 max_total_minutes 分钟立即退出（默认 30）
    - 退出时返回 (ok, fail, skip, exit_status)，exit_status: 'success' / 'fail-fast'

    设计理由：避免 Eastmoney 断连时每个时段死循环重试浪费 CPU，
    让 watch_eastmoney.sh 下次恢复后重新触发。
    """
    symbols = get_core_symbols()
    if limit:
        symbols = symbols[:limit]
    print(f"[core-full-em] 核心 {len(symbols)} 只, Eastmoney period={period} start={start}", flush=True)
    print(f"[core-full-em] fail-fast: consecutive_fail_max={consecutive_fail_max} max_total_minutes={max_total_minutes}", flush=True)
    t0 = time.time()
    ok = skip = fail = 0
    consecutive_fail = 0
    exit_status = "success"

    for i, sym in enumerate(symbols):
        # 全局时间检查
        el_min = (time.time() - t0) / 60
        if el_min > max_total_minutes:
            print(f"[core-full-em] ⚠ 超过 {max_total_minutes} 分钟（{el_min:.1f}min），退出", flush=True)
            exit_status = "fail-fast-time"
            break

        out_path = os.path.join(OUT, f'min{period}', sym + '.parquet')
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skip += 1
            continue
        try:
            df = fetch_history(sym, period=period, start=start)
            if df is not None and len(df):
                save_parquet(sym, df, period)
                ok += 1
                consecutive_fail = 0  # 重置
                el = time.time() - t0
                print(f"  [{i+1}/{len(symbols)}] {sym}: {len(df)} 根 (已用 {el:.0f}s, 连续成功)", flush=True)
            else:
                fail += 1
                consecutive_fail += 1
                print(f"  [{i+1}/{len(symbols)}] {sym}: 空数据 (consec_fail={consecutive_fail}/{consecutive_fail_max})", flush=True)
        except Exception as e:
            fail += 1
            consecutive_fail += 1
            err_short = str(e)[:80]
            print(f"  [FAIL] {sym}: {err_short} (consec_fail={consecutive_fail}/{consecutive_fail_max})", flush=True)

        # 连续失败退出检查
        if consecutive_fail >= consecutive_fail_max:
            print(f"[core-full-em] ⚠ 连续失败 {consecutive_fail} 段（>={consecutive_fail_max}），exit_status=fail-fast", flush=True)
            print("[core-full-em]   通常意味着 Eastmoney 接口断连；下次 watch_eastmoney.sh 探测恢复后会自动重启", flush=True)
            exit_status = "fail-fast-consecutive"
            break

        time.sleep(0.5)

    print(f"[core-full-em] 完成: ok={ok} fail={fail} skip={skip} exit_status={exit_status} 总耗时 {time.time()-t0:.0f}s", flush=True)
    return ok, fail, skip, exit_status


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', required=True, choices=['market-60d', 'core-1y', 'core-full-em'])
    ap.add_argument('--period', default='5', choices=['5', '1'])
    ap.add_argument('--start', default='2001-01-01')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--consecutive-fail-max', type=int, default=5, help='连续失败 N 段退出（fail-fast）')
    ap.add_argument('--max-total-minutes', type=int, default=30, help='单次运行最大分钟数')
    args = ap.parse_args()
    if args.mode == 'market-60d':
        market_60d(args.limit)
    elif args.mode == 'core-1y':
        core_1y(args.limit)
    else:
        result = core_full_em(args.period, args.start, args.limit,
                              args.consecutive_fail_max, args.max_total_minutes)
        # result 是 (ok, fail, skip, exit_status)
        # 如果 fail-fast，用非 0 exit code 让 systemd/cron 知道
        if result and len(result) == 4 and result[3].startswith("fail-fast"):
            sys.exit(2)
        sys.exit(0)
