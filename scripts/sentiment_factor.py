"""
资金/情绪因子计算（基于龙虎榜数据）
===================================

输入:
- data/sentiment/lhb_detail_2025_2026.parquet: 龙虎榜明细（33k 行）
- data/sentiment/lhb_jgmmtj_2025_2026.parquet: 机构买卖统计（18k 行）
- data/factors/event_factors.parquet: 已有事件因子（44k 行）

输出:
- data/factors/event_factors_with_sentiment.parquet: 加入情绪因子的事件矩阵

因子设计（基于龙虎榜）:
1. lhb_appear_5d/10d/20d: 近 N 日上龙虎榜次数
2. lhb_net_amt_5d/10d/20d: 近 N 日龙虎榜净买入额
3. lhb_buy_ratio_20d: 近 20 日买入额/总成交额
4. lhb_inst_net_20d: 近 20 日机构席位净买入
5. lhb_pct_chg_avg_20d: 上榜日平均涨跌幅
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')
LHB_DETAIL = ROOT / 'data/sentiment/lhb_detail_2025_2026.parquet'
LHB_JG = ROOT / 'data/sentiment/lhb_jgmmtj_2025_2026.parquet'
EVENT_FP = ROOT / 'data/factors/event_factors.parquet'
OUT_FP = ROOT / 'data/factors/event_factors_with_sentiment.parquet'


def load_lhb():
    detail = pd.read_parquet(LHB_DETAIL)
    jg = pd.read_parquet(LHB_JG)
    detail['上榜日'] = pd.to_datetime(detail['上榜日'], errors='coerce')
    jg['上榜日期'] = pd.to_datetime(jg['上榜日期'], errors='coerce')
    return detail, jg


def calc_sentiment_factors(event_df, lhb_daily, jg_daily, windows=(5, 10, 20)):
    """对每条事件计算近 N 日龙虎榜聚合"""
    event_df = event_df.copy()
    event_df['signal_date'] = pd.to_datetime(event_df['signal_date'])
    event_df['short_code'] = event_df['code'].str[2:]

    # 预聚合：每个 code 每天一行
    detail_daily = lhb_daily.groupby(['代码', '上榜日']).agg(
        net_amt=('龙虎榜净买额', 'sum'),
        buy_amt=('龙虎榜买入额', 'sum'),
        sell_amt=('龙虎榜卖出额', 'sum'),
        total_amt=('龙虎榜成交额', 'sum'),
        pct_chg=('涨跌幅', 'mean'),
        turnover=('换手率', 'mean'),
        market_total=('市场总成交额', 'first'),
    ).reset_index()

    jg_daily = jg_daily.groupby(['代码', '上榜日期']).agg(
        inst_net=('机构买入净额', 'sum'),
        inst_buy_count=('买方机构数', 'sum'),
        inst_sell_count=('卖方机构数', 'sum'),
    ).reset_index().rename(columns={'上榜日期': '上榜日'})

    # 合并
    lhb_daily_agg = detail_daily.merge(jg_daily, on=['代码', '上榜日'], how='outer')
    lhb_daily_agg = lhb_daily_agg.sort_values(['代码', '上榜日'])

    # 按 code 缓存
    code_to_lhb = {code: g.set_index('上榜日').sort_index()
                   for code, g in lhb_daily_agg.groupby('代码')}

    results = []
    for idx, row in event_df.iterrows():
        code = row['short_code']
        signal_date = row['signal_date']
        feats = {'code': row['code'], 'signal_date': signal_date}

        if code not in code_to_lhb:
            for w in windows:
                feats[f'lhb_appear_{w}d'] = 0
                feats[f'lhb_net_amt_{w}d'] = 0.0
                feats[f'lhb_buy_ratio_{w}d'] = 0.0
                feats[f'lhb_inst_net_{w}d'] = 0.0
            results.append(feats)
            continue

        lhb = code_to_lhb[code]
        for w in windows:
            start = signal_date - timedelta(days=w * 2)
            mask = (lhb.index >= start) & (lhb.index <= signal_date)
            window = lhb[mask]
            feats[f'lhb_appear_{w}d'] = int(len(window))
            feats[f'lhb_net_amt_{w}d'] = float(window['net_amt'].sum()) if 'net_amt' in window.columns and len(window) > 0 else 0.0
            if 'total_amt' in window.columns and window['total_amt'].sum() > 0:
                feats[f'lhb_buy_ratio_{w}d'] = float(window['buy_amt'].sum() / window['total_amt'].sum())
            else:
                feats[f'lhb_buy_ratio_{w}d'] = 0.0
            feats[f'lhb_inst_net_{w}d'] = float(window['inst_net'].sum()) if 'inst_net' in window.columns and len(window) > 0 else 0.0

        results.append(feats)

        if (idx + 1) % 5000 == 0:
            print(f'  [{idx+1}/{len(event_df)}] 进度 {(idx+1)/len(event_df)*100:.0f}%')

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()

    print(f'[{datetime.now():%H:%M:%S}] 加载龙虎榜...')
    detail, jg = load_lhb()
    print(f'  detail={len(detail):,}, jg={len(jg):,}')
    print(f'  日期: {detail["上榜日"].min()} ~ {detail["上榜日"].max()}')

    print(f'\n[{datetime.now():%H:%M:%S}] 加载事件矩阵...')
    event_df = pd.read_parquet(EVENT_FP)
    if args.limit > 0:
        event_df = event_df.sample(n=args.limit, random_state=42)
    print(f'  事件数: {len(event_df):,}')

    print(f'\n[{datetime.now():%H:%M:%S}] 计算情绪因子...')
    t0 = datetime.now()
    sent_df = calc_sentiment_factors(event_df, detail, jg)
    print(f'  耗时: {(datetime.now()-t0).total_seconds():.1f}s')

    print(f'\n[{datetime.now():%H:%M:%S}] 合并...')
    merged = event_df.merge(sent_df, on=['code', 'signal_date'], how='left')

    # 缺失率
    print(f'  合并: {len(merged):,} 行 × {len(merged.columns)} 列')
    sent_cols = [c for c in merged.columns if c.startswith('lhb_')]
    print(f'  新增情绪因子 ({len(sent_cols)}): {sent_cols}')
    for c in sent_cols:
        miss = merged[c].isna().mean() * 100
        zero = (merged[c] == 0).mean() * 100
        print(f'    {c}: 缺失 {miss:.1f}%  0值 {zero:.1f}%  均值 {merged[c].mean():.2f}')

    merged.to_parquet(OUT_FP, index=False)
    print(f'\n输出: {OUT_FP}')

    # 情绪因子在正负样本中的对比
    print(f'\n===== 情绪因子正负样本对比 =====')
    for c in sent_cols:
        pos = merged[merged['label']==1][c]
        neg = merged[merged['label']==0][c]
        print(f'  {c}: 正均值={pos.mean():.3f}  负均值={neg.mean():.3f}  '
              f'差={pos.mean()-neg.mean():+.3f}  正0率={(pos==0).mean()*100:.1f}%')


if __name__ == '__main__':
    main()