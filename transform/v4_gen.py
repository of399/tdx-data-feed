#!/usr/bin/env python3
"""v4 完整生成器（单位感知 + 滑动窗口 + 时序切分 80/10/10）。

用法: python transform/v4_gen.py [--dry]
  --dry: 只统计不写文件（默认全量生成到 data/v4/）
策略（与现有 v4 对齐）：
  - 每只标的：按日期排序，每 5 交易日滑动取近 20 日窗口，最多生成最近 50 交易日的 10 条
  - 单位感知：每行 volume 用 amount 物理校验（手→×100、股→保持）
  - 切分：全部样本按窗口末日日期排序 → 前 80% train、后 10% val、最后 10% test
  - 仅处理 A 股（sh60/68、sz00/30 段；跳过转债 11x/12x、ETF 51x/15x/16x/18x）
"""
import glob
import json
import os
import sys

import pandas as pd

PK = '/home/jiuben/tdx-data-feed/data/parquet/daily/'
OUT = '/home/jiuben/tdx-data-feed/data/v4/'
WINDOW = 20
STEP = 5
MAX_SAMPLES = 10

def is_a_stock(sym):
    c = sym[2:]
    if c.startswith(('11', '12')):  # 转债
        return False
    if c.startswith(('51', '56', '58', '15', '16', '18')):  # ETF
        return False
    return bool(c.startswith(('60', '68', '00', '30', '001', '002', '003')))

def unit_mult(amount, volume, close):
    try:
        a, v, c = float(amount), float(volume), float(close)
    except (TypeError, ValueError):
        return 100
    if a <= 0 or v <= 0 or c <= 0:
        return 100
    ratio = a / (v * c)
    if 30 < ratio < 300:
        return 100
    if 0.3 < ratio < 3:
        return 1
    return 100

def fmt_date(d):
    d = str(d).replace('-', '')
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

def build_samples(sym, df):
    df = df.sort_values('date').reset_index(drop=True)
    if len(df) < WINDOW:
        return []
    code = sym[-6:]
    samples = []
    n = len(df)
    # 从末尾向前滑动，每 STEP 个交易日一条，最多 MAX_SAMPLES 条
    starts = range(n - WINDOW, -1, -STEP)
    for st in list(starts)[:MAX_SAMPLES]:
        win = df.iloc[st:st + WINDOW]
        rows = []
        prev_close = None
        for _, b in win.iterrows():
            close = float(b['close'])
            pct = 0.0
            if prev_close:
                pct = round((close - prev_close) / prev_close * 100, 2)
            mult = unit_mult(b.get('amount'), b.get('volume'), close)
            vol = int(float(b['volume']))
            if mult == 100:
                vol = int(float(b['volume']) * 100)
            rows.append({
                '日期': fmt_date(b['date']),
                '开盘': round(float(b['open']), 2),
                '收盘': round(close, 2),
                '最高': round(float(b['high']), 2),
                '最低': round(float(b['low']), 2),
                '成交量': vol,
                '涨跌幅': pct,
            })
            prev_close = close
        last = win.iloc[-1]
        samples.append({
            'instruction': (f'你是专业的A股量化分析师。请基于以下截至{fmt_date(last["date"])}的'
                            f'近{WINDOW}个交易日行情数据，对股票 {code} 做技术面分析并给出操作建议。'),
            'input': json.dumps(rows, ensure_ascii=False),
            'output': '',
        })
    return samples

def main(dry=False):
    files = sorted(glob.glob(PK + '*.parquet'))
    all_samples = []
    skipped = 0
    for fp in files:
        sym = os.path.basename(fp)[:-8]
        if not is_a_stock(sym):
            continue
        df = pd.read_parquet(fp)
        ss = build_samples(sym, df)
        if not ss:
            skipped += 1
        all_samples.extend(ss)
    # 按窗口末日日期排序后时序切分 80/10/10
    def last_date(s):
        return json.loads(s['input'])[-1]['日期']
    all_samples.sort(key=last_date)
    n = len(all_samples)
    n_tr = int(n * 0.8)
    n_va = int(n * 0.1)
    parts = {
        'train.jsonl': all_samples[:n_tr],
        'val.jsonl': all_samples[n_tr:n_tr + n_va],
        'test.jsonl': all_samples[n_tr + n_va:],
    }
    print(f'A股标的样本总数: {n} | 跳过(数据不足): {skipped}')
    for fn, ss in parts.items():
        print(f'  {fn}: {len(ss)} 条')
    if dry:
        print('DRY-RUN (未写文件)')
        return
    os.makedirs(OUT, exist_ok=True)
    for fn, ss in parts.items():
        with open(OUT + fn, 'w', encoding='utf-8') as f:
            for s in ss:
                f.write(json.dumps(s, ensure_ascii=False) + '\n')
    print('V4-GEN-DONE ->', OUT)

if __name__ == '__main__':
    main(dry='--dry' in sys.argv)
