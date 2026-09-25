#!/usr/bin/env python3
"""
财务数据 JSONL → Parquet 转换 + 增量出数（供 full_sync 主链路调用）
- 已有 jsonl → 转 parquet（finance/xdxr 各一张表）
- 主链路增量：核心 300 只已存在 parquet 则跳过
"""
import contextlib
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, '/home/jiuben/tdx-data-feed')
sys.path.insert(0, '/home/jiuben/tdx-data-feed/tdxfeed')

OUT = '/home/jiuben/tdx-data-feed/data/parquet'
FIN_JSON = os.path.join(OUT, 'finance')
XDR_JSON = os.path.join(OUT, 'xdxr')
FIN_PQ = os.path.join(OUT, 'finance.parquet')
XDR_PQ = os.path.join(OUT, 'xdxr.parquet')
CORE_FILE = '/home/jiuben/tdx-data-feed/core_stocks.json'


def jsonl_to_df(d):
    """读取目录下全部 jsonl → 单 DataFrame（带 symbol 列）"""
    frames = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.jsonl'):
            continue
        sym = fn[:-6]
        rows = []
        with open(os.path.join(d, fn), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if rows:
            df = pd.DataFrame(rows)
            df.insert(0, 'symbol', sym)
            frames.append(df)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def convert():
    t0 = time.time()
    print("[convert] finance jsonl → parquet...")
    df = jsonl_to_df(FIN_JSON)
    if df is not None:
        df.to_parquet(FIN_PQ, index=False)
        print(f"  finance.parquet: {len(df)} 行, 列 {list(df.columns)[:8]}...")
    else:
        print("  finance: 无数据")

    print("[convert] xdxr jsonl → parquet...")
    dfx = jsonl_to_df(XDR_JSON)
    if dfx is not None:
        dfx.to_parquet(XDR_PQ, index=False)
        print(f"  xdxr.parquet: {len(dfx)} 行, 列 {list(dfx.columns)}")
    else:
        print("  xdxr: 无数据")
    print(f"[convert] 完成 耗时 {time.time()-t0:.0f}s")


def incremental():
    """主链路增量：核心300只 finance/xdxr，parquet 已存在则跳过（断点续传）"""
    from corp import fetch_xdxr
    from finance import fetch_finance
    with open(CORE_FILE, encoding='utf-8') as f:
        core = json.load(f)['stocks']

    os.makedirs(FIN_JSON, exist_ok=True)
    os.makedirs(XDR_JSON, exist_ok=True)
    ok = skip = fail = 0
    t0 = time.time()
    for i, s in enumerate(core):
        sym = s['symbol']
        fin_p = os.path.join(FIN_JSON, sym + '.jsonl')
        xdr_p = os.path.join(XDR_JSON, sym + '.jsonl')
        if os.path.exists(fin_p) and os.path.exists(xdr_p):
            skip += 1
            continue
        with contextlib.suppress(ImportError):
            from finance import fin_to_records  # noqa: F401  # 若 finance.py 已提供

        try:
            fin = fetch_finance(sym)
            xd = fetch_xdxr(sym)
            recs = []
            if fin:
                keys = list(fin[0].keys())
                for r in fin:
                    opt = r.get('选项', '')
                    metric = r.get('指标', '')
                    for k in keys[2:]:
                        v = r.get(k)
                        if v is not None and str(v) not in ('', 'nan'):
                            recs.append({'report_date': str(k), 'option': opt, 'metric': metric, 'value': v})
            with open(fin_p, 'w', encoding='utf-8') as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')
            with open(xdr_p, 'w', encoding='utf-8') as f:
                for r in xd:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [FAIL] {sym}: {str(e)[:80]}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(core)}] ok={ok} fail={fail} skip={skip} 已用 {time.time()-t0:.0f}s", flush=True)
        time.sleep(0.5)
    print(f"[incremental] 完成: ok={ok} fail={fail} skip={skip}")
    convert()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'incremental':
        incremental()
    else:
        convert()
