#!/usr/bin/env python3
"""
ETF adj_factor 抓取脚本（备用，当前 akshare 东财接口故障）【债券已下线：不再保留债券数据】

数据源：
  - ETF/REITs (5/56/58/15 等前缀)：akshare.fund_etf_hist_em(adjust='hfq')  vs  adjust=''
  - 可转债 (11/12/123/128 等前缀)：【已弃用】项目政策不再保留债券数据，不再生成 CB adj_factor

当前状态（2026-09-27）：akshare 东财接口持续 RemoteDisconnected
  - fund_etf_hist_em: ❌ Connection aborted
  - stock_zh_a_hist:  ❌ Connection aborted

用法：
  python scripts/backfill_adj_etf.py --dry-run   # 仅验证接口
  python scripts/backfill_adj_etf.py --limit 5   # 小样本测试
  python scripts/backfill_adj_etf.py             # 全量（需 akshare 接口恢复）

输出：data/adj_factor/{code}.parquet （与现有口径一致）
Schema：date, adj_factor, qfq_close, raw_close
"""
import argparse
import json
import os
import re
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings('ignore')

ROOT = Path('/home/jiuben/tdx-data-feed')
OUT = ROOT / 'data' / 'adj_factor'
OUT.mkdir(parents=True, exist_ok=True)
ALL_STOCKS = ROOT / 'data' / 'all_a_stocks.json'
ETF_CODES = ROOT / 'data' / 'etf_codes.txt'
LOG = Path('/tmp/adj_factor_etf.log')

START_DATE = '2020-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def classify(code):
    """判断是否是 ETF/可转债/REITs（在 all_a_stocks 之外但 daily 里有）"""
    # ETF 前缀
    etf_p = ('51', '56', '58', '15', '16', '18')
    # 可转债前缀
    cb_p = ('11', '12', '123', '128', '113')
    if code[:3] in ('159', '588', '516', '515', '561', '510', '512', '513', '588'):
        return 'etf'
    if code[:2] in etf_p or code[:3] in etf_p:
        return 'etf'
    if code[:3] in ('113', '123', '128') or code[:2] in ('11', '12'):
        return 'cb'
    return 'other'


def fetch_etf_adj(code_short, retries=3):
    """抓取 ETF 复权因子（akshare fund_etf_hist_em）
    返回：list of {date, qfq_close, raw_close} 或 None（失败）
    """
    import akshare as ak
    import pandas as pd
    last_err = None
    for attempt in range(retries):
        try:
            # hfq 前复权
            df_hfq = ak.fund_etf_hist_em(symbol=code_short, period='daily', adjust='hfq')
            # 不复权
            df_raw = ak.fund_etf_hist_em(symbol=code_short, period='daily', adjust='')
            if df_hfq is None or df_raw is None or len(df_hfq) == 0:
                raise ValueError('empty data')
            # 对齐日期
            m = df_hfq.set_index('日期')['收盘'].to_dict()
            out = []
            for _, row in df_raw.iterrows():
                d = row['日期'].strftime('%Y-%m-%d') if hasattr(row['日期'], 'strftime') else str(row['日期'])[:10]
                raw_close = float(row['收盘'])
                qfq_close = float(m.get(row['日期'], 0))
                if qfq_close > 0 and raw_close > 0:
                    out.append({'date': d, 'adj_factor': qfq_close / raw_close,
                                'qfq_close': qfq_close, 'raw_close': raw_close})
            return out
        except Exception as e:
            last_err = str(e)[:120]
            time.sleep(2)
    log(f'  ETF {code_short} 最终失败: {last_err}')
    return None


def fetch_cb_adj_dummy(code_short):
    """可转债没有除权除息事件，adj_factor 始终为 1.0
    返回：占位记录（日期范围来自 baostock 的交易日历，简化为 2020-01-01 至今每月一条）
    """
    # 实际可转债有利息支付但无除权除息，市价已包含利息影响，无需 adj
    return [{'date': d, 'adj_factor': 1.0, 'qfq_close': 0.0, 'raw_close': 0.0}
            for d in ['2020-01-02', '2024-01-02']]  # 仅占位


def save_one(code_short, rows):
    """写 parquet（与 v2 schema 一致）"""
    import pandas as pd
    if not rows:
        return False
    df = pd.DataFrame(rows)
    df['code'] = code_short
    df = df[['code', 'date', 'adj_factor', 'qfq_close', 'raw_close']]
    fp = OUT / f'{code_short}.parquet'
    df.to_parquet(fp, index=False)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes-file', default=str(ETF_CODES), help='一行一个 6 位 code')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true', help='只验证接口，不写文件')
    args = ap.parse_args()

    codes = [ln.strip() for ln in Path(args.codes_file).read_text().splitlines() if ln.strip()]
    if args.limit:
        codes = codes[:args.limit]

    log(f'开始 ETF adj 抓取（可转债已弃用，项目政策不再保留债券数据）, 共 {len(codes)} 只')

    # 分类
    etfs, cbs, others = [], [], []
    for c in codes:
        k = classify(c)
        if k == 'etf':
            etfs.append(c)
        elif k == 'cb':
            cbs.append(c)
        else:
            others.append(c)
    log(f'分类: ETF={len(etfs)}, 可转债(已弃用)={len(cbs)}, 其他={len(others)}')

    if args.dry_run:
        log('dry-run 模式：仅验证 akshare 接口')
        for c in etfs[:3]:
            log(f'  试抓 ETF {c}')
            rows = fetch_etf_adj(c)
            log(f'  {c}: {len(rows) if rows else 0} 行')
        return

    ok = fail = skip = 0
    for i, c in enumerate(etfs):
        if (OUT / f'{c}.parquet').exists():
            skip += 1
            continue
        rows = fetch_etf_adj(c)
        if rows and save_one(c, rows):
            ok += 1
        else:
            fail += 1
        if (i + 1) % 20 == 0 or i == len(etfs + cbs) - 1:
            log(f'  [{i+1}/{len(etfs + cbs)}] ok={ok} fail={fail} skip={skip}')
        time.sleep(0.5)

    log(f'完成: ok={ok} fail={fail} skip={skip}')


if __name__ == '__main__':
    main()
