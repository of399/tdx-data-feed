#!/usr/bin/env python3
"""补全日线到 TARGET 只：pytdx 枚举全市场 -> 仅筛选沪深股票 -> Sina 源拉日线。
用法: python download_universe.py --target 10000
"""
import argparse
import glob
import os
import sys
import time

sys.path.insert(0, "/home/jiuben/tdx-data-feed")
import pandas as pd
from pytdx.hq import TdxHq_API

from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.symbols import group

OUT = "/home/jiuben/tdx-data-feed/data/parquet/daily/"
SRV = ("218.6.170.47", 7709)
SLEEP = 0.2

def sym_of(code):
    g = group(code)
    if g in ("sh", "sz"):
        return g + code
    return None

def get_codes():
    api = TdxHq_API()
    api.connect(*SRV, time_out=5)
    codes = []
    for market in (0, 1):
        try:
            count = api.get_security_count(market)
        except Exception:
            count = 0
        start = 0; miss = 0
        while start < count and miss < 3:
            lst = None
            for _ in range(3):
                try:
                    lst = api.get_security_list(market, start); break
                except Exception:
                    time.sleep(1)
            if not lst:
                miss += 1; start += 1000; continue
            miss = 0
            for it in lst:
                codes.append(str(it["code"]))
            start += 1000
    api.disconnect()
    return codes

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=10000)
    args = ap.parse_args()
    codes = get_codes()
    cand = [c for c in codes if group(c) in ("sh", "sz")]
    order = {"sh": 0, "sz": 1}
    cand.sort(key=lambda c: order[group(c)])
    existing = set(os.path.basename(p)[:-8] for p in glob.glob(OUT + "*.parquet") if ".bak" not in p)
    print(f"候选 {len(cand)} 只, 已有 daily {len(existing)} 只", flush=True)

    src = SinaSource()
    done = len(existing); added = 0
    for c in cand:
        if done >= args.target:
            break
        s = sym_of(c)
        if s in existing:
            continue
        try:
            bars = src.fetch_bars(s, scale=240, datalen=1023)
        except Exception:
            time.sleep(1); continue
        if not bars:
            time.sleep(0.1); continue
        df = pd.DataFrame(bars, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d").astype(int)
        df = df.sort_values("date")
        if len(df) < 20:
            time.sleep(0.1); continue
        df.to_parquet(OUT + s + ".parquet", index=False)
        done += 1; added += 1; time.sleep(SLEEP)
        if added % 50 == 0:
            print(f"added {added} total {done}", flush=True)
    print(f"DONE total {done} added {added}", flush=True)

if __name__ == "__main__":
    main()
