#!/usr/bin/env python3
"""
分钟线拉取模块（akshare Eastmoney，带重试+按月翻页+断点续传）
产物: data/parquet/min5/{symbol}.parquet（5 分钟线）
     data/parquet/min1/{symbol}.parquet（1 分钟线）
"""
import json
import os
import re
import sys
import time

import requests as _req

try:
    import akshare as ak
    import pandas as pd
    import pyarrow as pa  # noqa: F401
    import pyarrow.parquet as pq  # noqa: F401
except ImportError as e:
    print(f"[setup] 缺依赖: {e}；请 pip install akshare pandas pyarrow", file=sys.stderr)
    raise

OUT_ROOT = '/home/jiuben/tdx-data-feed/data/parquet'
MAX_RETRY = 5
SLEEP_BASE = 1.5

# ============ Sina 分钟线（稳定备用源，单次上限 1023 根） ============
SINA_SCALES = {15: 'min15', 60: 'min60', 5: 'min5'}


def symbol_to_akshare_code(symbol):
    """sh600000 → 600000；akshare 用纯 6 位代码"""
    s = symbol.lower()
    return s[2:] if s.startswith(('sh', 'sz')) else s


def fetch_min(symbol, period, start_dt, end_dt):
    """拉一段分钟线（Eastmoney）。period: '5'/'1'。返回 DataFrame 或 None。"""
    code = symbol_to_akshare_code(symbol)
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=start_dt,
                end_date=end_dt,
                period=period,
                adjust="",
            )
            if df is not None and len(df) > 0:
                return df
            return None
        except Exception as e:
            last_err = e
            wait = SLEEP_BASE * attempt
            print(f"    [retry {attempt}/{MAX_RETRY}] {symbol} {start_dt}~{end_dt}: {str(e)[:60]} (等 {wait}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"{symbol} {start_dt}~{end_dt} 重试 {MAX_RETRY} 次仍失败: {last_err}")


def fetch_history(symbol, period='5', start='2001-01-01', end=None, month_step=1):
    """按月翻页拉全历史分钟线 → DataFrame（合并、去重）"""
    from datetime import datetime, timedelta
    if end is None:
        end = datetime.now().strftime('%Y-%m-%d')
    cur = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')
    frames = []
    while cur < end_dt:
        # 月末
        if cur.month == 12:
            nxt = datetime(cur.year + 1, 1, 1)
        else:
            nxt = datetime(cur.year, cur.month + 1, 1)
        seg_end = min(nxt - timedelta(days=1), end_dt)
        s = cur.strftime('%Y-%m-%d') + ' 09:30:00'
        e = seg_end.strftime('%Y-%m-%d') + ' 15:00:00'
        try:
            df = fetch_min(symbol, period, s, e)
            if df is not None and len(df):
                frames.append(df)
        except RuntimeError as ex:
            print(f"  [skip段] {symbol} {s}~{e}: {str(ex)[:80]}", flush=True)
        cur = nxt
        time.sleep(0.3)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=['时间'], keep='last')
    return df.sort_values('时间').reset_index(drop=True)


def save_parquet(symbol, df, period):
    """落盘 parquet（列名统一英文）"""
    out_dir = os.path.join(OUT_ROOT, f'min{period}')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, symbol + '.parquet')
    out = df.rename(columns={
        '时间': 'datetime', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume',
        '成交额': 'amount', '涨跌幅': 'pct_chg', '涨跌额': 'chg',
        '振幅': 'amplitude', '换手率': 'turnover',
    })
    out.to_parquet(path, index=False)
    return path


def load_manifest(path):
    """读取 manifest（list[str]）或生成默认"""
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return None



def fetch_sina(symbol, scale=15, datalen=1023):
    """Sina 分钟线。scale=15 覆盖≈60交易日, 60 覆盖≈1年, 5 覆盖≈21交易日"""
    url = (f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x='
           f'/CN_MarketDataService.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}')
    for attempt in range(3):
        try:
            r = _req.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            m = re.search(r'\((\[.*\])\)', r.text, re.S)
            if not m:
                return None
            rows = json.loads(m.group(1))
            out = []
            for row in rows:
                out.append({
                    'datetime': str(row['day']),
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row.get('volume') or 0),
                    'amount': float(row.get('amount') or 0),
                })
            return out
        except Exception as e:
            if attempt == 2:
                print(f"    [sina retry x3] {symbol} scale={scale}: {str(e)[:60]}", flush=True)
                return None
            time.sleep(2)
    return None


def save_rows_parquet(symbol, rows, scale):
    """Sina 行 → parquet（min15/min60 目录）"""
    import pandas as pd
    out_dir = os.path.join(OUT_ROOT, SINA_SCALES[scale])
    os.makedirs(out_dir, exist_ok=True)
    df = pd.DataFrame(rows)
    path = os.path.join(out_dir, symbol + '.parquet')
    df.to_parquet(path, index=False)
    return path
