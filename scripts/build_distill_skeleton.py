import json
import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：让 tdxfeed.atomic 可导入

from _common import daily_files

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

OUT = '/home/jiuben/tdx-data-feed/data/distill/skeleton.jsonl'
WINDOW = 20
STEP = 5
MAX_SAMPLES = int(sys.argv[1]) if len(sys.argv) > 1 else 3

def unit_mult(amount, volume, close):
    try:
        a, v, c = float(amount), float(volume), float(close)
    except (TypeError, ValueError):
        _ERROR_COUNT['unit_mult'] += 1
        return 100
    if a <= 0 or v <= 0 or c <= 0:
        return 100
    r = a / (v * c)
    if 30 < r < 300:
        return 100
    if 0.3 < r < 3:
        return 1
    return 100

def fmt(d):
    d = str(d).replace('-', '')
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

def build(sym, df):
    df = df.sort_values('date').reset_index(drop=True)
    if len(df) < WINDOW:
        return []
    code = sym[-6:]
    n = len(df)
    starts = list(range(n - WINDOW, -1, -STEP))[:MAX_SAMPLES]
    out = []
    for st in starts:
        win = df.iloc[st:st + WINDOW]
        rows, prev = [], None
        for _, b in win.iterrows():
            close = float(b['close'])
            pct = round((close - prev) / prev * 100, 2) if prev else 0.0
            mult = unit_mult(b.get('amount'), b.get('volume'), close)
            vol = int(float(b['volume']))
            if mult == 100:
                vol = int(float(b['volume']) * 100)
            rows.append({'日期': fmt(b['date']), '开盘': round(float(b['open']), 2),
                         '收盘': round(close, 2), '最高': round(float(b['high']), 2),
                         '最低': round(float(b['low']), 2), '成交量': vol, '涨跌幅': pct})
            prev = close
        last = win.iloc[-1]
        out.append({
            'instruction': (f'你是专业的A股量化分析师。请基于以下截至{fmt(last["date"])}的'
                            f'近{WINDOW}个交易日行情数据，对股票 {code} 做技术面分析并给出操作建议。'),
            'input': json.dumps(rows, ensure_ascii=False),
            'output': '',
        })
    return out

def main():
    files = daily_files()
    print('parquet 文件数:', len(files))
    if files:
        print('首文件列名:', list(pd.read_parquet(files[0]).columns))
    all_s = []
    for fp in files:
        sym = os.path.basename(fp)[:-8]
        df = pd.read_parquet(fp)
        all_s.extend(build(sym, df))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(f"{OUT}.tmp", 'w', encoding='utf-8') as f:
        for s in all_s:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
    os.replace(f"{OUT}.tmp", OUT)
    print('SKELETON_DONE 条数:', len(all_s), '->', OUT)

main()
