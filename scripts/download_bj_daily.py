"""下载北交所 bj920XXX 日 K 到 data/parquet/daily/bj{code}.parquet
格式与现有 sh/sz parquet 一致：date, open, high, low, close, volume, amount
"""
import socket
import time
import warnings
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

socket.setdefaulttimeout(30)
warnings.filterwarnings('ignore')

PARQUET_DIR = Path('/home/jiuben/tdx-data-feed/data/parquet/daily')
ALL_STOCKS = Path('/home/jiuben/tdx-data-feed/data/all_a_stocks.json')
LOG = Path('/tmp/bj_daily_download.log')


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def fetch_one(code: str, max_retries: int = 3) -> str:
    cache_fp = PARQUET_DIR / f'bj{code}.parquet'
    sym = f'bj{code}'

    last_err = None
    for attempt in range(max_retries):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust='')
            if df is None or len(df) == 0:
                last_err = 'empty'
                time.sleep(2 ** attempt)
                continue

            df['date'] = pd.to_datetime(df['date'])
            df = df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount']].copy()
            df['open'] = pd.to_numeric(df['open'], errors='coerce')
            df['high'] = pd.to_numeric(df['high'], errors='coerce')
            df['low'] = pd.to_numeric(df['low'], errors='coerce')
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])

            df.to_parquet(cache_fp, index=False)
            return 'ok'

        except Exception as e:
            last_err = f'{type(e).__name__}: {str(e)[:50]}'
            time.sleep(2 ** attempt + 0.5)

    return last_err


def main():
    LOG.unlink(missing_ok=True)
    log('=== 北交所日 K 下载启动 (akshare) ===')

    import json
    with open(ALL_STOCKS) as _f:
        name_map = json.load(_f)
    codes = sorted(name_map.keys())

    targets = [c for c in codes if c.startswith('920') and not (PARQUET_DIR / f'bj{c}.parquet').exists()]
    log(f'目标: {len(targets)} 只')

    fail_codes = []
    t0 = time.time()
    for i, code in enumerate(targets):
        result = fetch_one(code)
        if result != 'ok':
            fail_codes.append((code, result))

        elapsed = time.time() - t0
        speed = (i + 1) / max(elapsed, 1)
        eta = (len(targets) - i - 1) / max(speed, 0.01) / 60
        if (i + 1) % 30 == 0 or i < 5:
            log(f'  [{i+1}/{len(targets)}] bj{code} {result} speed={speed:.2f}/s ETA={eta:.0f}min')

        time.sleep(0.3)

    ok = len(targets) - len(fail_codes)
    log(f'\n=== 完成 === ok={ok} fail={len(fail_codes)}  耗时={(time.time()-t0)/60:.1f}min')
    if fail_codes:
        log('失败列表:')
        for code, err in fail_codes[:30]:
            log(f'  {code}: {err}')


if __name__ == '__main__':
    main()
