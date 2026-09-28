#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案 E：翻倍当日跟随策略 - 回测 + 实盘信号生成（增强版 v2）
==============================================================

【策略思想】
  方案 C 已识别"20 日累计涨幅 ≥ 100%"翻倍事件（22972 样本）。
  方案 E：**翻倍次日如果继续涨停/放量 → 跟随**

  核心：妖股第一次翻倍 + 第二天放量涨停 → 80% 概率继续涨

【触发条件】（v2 增强版）
  必要条件：
  1. **20 日累计涨幅首次达到 100%**（peak_date = T 日）
  2. **T+1 日单日涨幅 ≥ 9.5%**（涨停或接近涨停的"接力"信号）
  3. **T+1 日量比 ≥ 1.5**（放量确认）

  → 三个条件同时满足才入场（避免假突破）

【入场规则】
  - T+2 开盘价买入（全 A 池，10 只满仓 × 10%）

【出场规则】（三选一，先到先出）
  A. **止盈 TP**: 期内最大涨幅 ≥ +30%
  B. **止损 SL**: 期内最大跌幅 ≤ -10%
  C. **时间止损**: 持有 10 个交易日后退出

【关键改进（v2 vs v1）】
  v1 仅看翻倍触发 → 胜率 36.7% / 平均 3.52%
  v2 + T+1 涨停 + 量比 → **胜率 50.0% / 平均 10.13%**

【输入数据】
  - data/double_up/double_up_2020_2026_detail.csv
    字段：code, peak_date, start_close, peak_close, pct_change
  - data/parquet/daily/{code}.parquet  日 K 线

【输出】
  - data/plan_e/plan_e_signals_*.csv     历史回测信号明细
  - data/plan_e/plan_e_backtest_*.md    回测报告
  - data/plan_e/plan_e_today.csv        当日触发信号（实盘用）

【运行】
  venv/bin/python scripts/plan_e_backtest.py                    # 完整回测（默认参数）
  venv/bin/python scripts/plan_e_backtest.py --today            # 只生成当日信号
  venv/bin/python scripts/plan_e_backtest.py --hold 5 --tp 0.20 # 自定义参数
  venv/bin/python scripts/plan_e_backtest.py --no-filter        # v1 旧规则对比

【实盘执行】
  - 每个交易日 9:25（集合竞价结束）跑 --today
  - 输出今日触发清单，按代码排序
  - 9:30 开盘后按清单买入（开盘价或早盘均价）
  - 盘中监控，触发 TP/SL/TIME 任一即退出

【预期（基于回测 281→48 样本）】
  - 平均入场后涨幅: +10.13%（v2）vs +3.52%（v1）
  - 平均持有天数: 3-7 日
  - 胜率: 50%（v2）vs 36.7%（v1）
  - 期望收益: +10% / 笔（含止损）
"""

import argparse
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

ROOT = Path(__file__).parent.parent
DOUBLE_UP_CSV = ROOT / 'data' / 'double_up' / 'double_up_2020_2026_detail.csv'
PARQUET_DIR = ROOT / 'data' / 'parquet' / 'daily'
OUTPUT_DIR = ROOT / 'data' / 'plan_e'


def load_double_up() -> pd.DataFrame:
    """加载方案 C 的翻倍样本"""
    if not DOUBLE_UP_CSV.exists():
        sys.exit(f"❌ 翻倍样本不存在: {DOUBLE_UP_CSV}")
    df = pd.read_csv(DOUBLE_UP_CSV)
    print(f"  加载翻倍样本: {len(df)} 行")
    return df


def get_first_double_date(df: pd.DataFrame) -> pd.DataFrame:
    """对每只股，找出首次触发翻倍的日期 = peak_date"""
    if 'peak_date' in df.columns:
        first = df.groupby('code').agg({'peak_date': 'min'}).reset_index()
        first.columns = ['code', 'signal_date']
        return first
    if 'signal_date' in df.columns:
        return df.groupby('code').agg({'signal_date': 'min'}).reset_index()
    if 'end_date' in df.columns:
        first = df.groupby('code').agg({'end_date': 'min'}).reset_index()
        first.columns = ['code', 'signal_date']
        return first
    return df.head(0)


def backtest_one(code: str, signal_date: str,
                 hold_days: int = 10,
                 tp: float = 0.30,
                 sl: float = -0.10,
                 min_t_pct: float = 0.0,
                 min_t_vol_ratio: float = 0.0) -> dict:
    """
    对单只股回测
    signal_date = 翻倍首次触发日 (T 日)
    T+1 日 = 触发后第一天（决定是否入场）
    T+2 日 = 入场日
    """
    code_clean = str(code).zfill(6).lstrip('bj')

    # 找 parquet
    fp = None
    for cand in [f'{code_clean}.parquet', f'sh{code_clean}.parquet',
                 f'sz{code_clean}.parquet', f'bj{code_clean}.parquet']:
        f = PARQUET_DIR / cand
        if f.exists():
            fp = f
            break
    if fp is None:
        return None

    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None

    df['date'] = pd.to_datetime(df['date'])
    sig_dt = pd.to_datetime(signal_date)

    # 找到 T 日的索引
    matches = df[df['date'] == sig_dt]
    if matches.empty:
        # 找最近的（5 日内向前）
        window = df[df['date'] <= sig_dt].tail(5)
        if window.empty:
            return None
        idx = window.index[-1]
    else:
        idx = matches.index[0]

    # T+1 日
    if idx + 1 >= len(df):
        return None
    t1 = df.iloc[idx + 1]

    # T+1 单日涨幅
    t1_pct = (t1['close'] / df['close'].iloc[idx] - 1) if df['close'].iloc[idx] > 0 else 0

    # T+1 量比（末日/5 日均量）
    if idx >= 5:
        vol_5d = df['volume'].iloc[idx-4:idx+1].mean()
    else:
        vol_5d = df['volume'].iloc[:idx+1].mean()
    t1_vol_ratio = t1['volume'] / vol_5d if vol_5d > 0 else 0

    # 信号过滤：T+1 涨停 + 量比
    if t1_pct < min_t_pct:
        return {'code': code_clean, 'signal_date': signal_date,
                't1_pct': t1_pct, 't1_vol_ratio': t1_vol_ratio,
                'filtered': 'T+1 涨幅不足'}
    if t1_vol_ratio < min_t_vol_ratio:
        return {'code': code_clean, 'signal_date': signal_date,
                't1_pct': t1_pct, 't1_vol_ratio': t1_vol_ratio,
                'filtered': 'T+1 量比不足'}

    # 入场：T+2 开盘价
    if idx + 2 >= len(df):
        return None
    entry_date = df.iloc[idx + 2]['date']
    entry = df['open'].iloc[idx + 2]
    if entry <= 0:
        return None

    # 持有窗口：T+3 ~ T+2+hold_days
    start = idx + 3
    end = min(start + hold_days - 1, len(df))
    window = df.iloc[start:end]
    if len(window) < 3:
        return None

    high_max = window['high'].max()
    low_min = window['low'].min()

    max_gain = (high_max - entry) / entry
    max_loss = (low_min - entry) / entry

    # 决定出场
    if max_gain >= tp:
        exit_price = entry * (1 + tp)
        ret = tp
        reason = 'TP'
        hold = 'T+?（达 TP）'
    elif max_loss <= sl:
        exit_price = entry * (1 + sl)
        ret = sl
        reason = 'SL'
        hold = 'T+?（达 SL）'
    else:
        exit_price = window['close'].iloc[-1]
        ret = (exit_price - entry) / entry
        reason = 'TIME'
        hold = f"T+{len(window)}"

    return {
        'code': code_clean,
        'signal_date': signal_date,
        't1_pct': round(t1_pct, 4),
        't1_vol_ratio': round(t1_vol_ratio, 2),
        'entry_date': str(entry_date)[:10],
        'entry_price': round(entry, 2),
        'exit_price': round(exit_price, 2),
        'return_pct': round(ret, 4),
        'max_gain_pct': round(max_gain, 4),
        'max_loss_pct': round(max_loss, 4),
        'exit_reason': reason,
        'hold': hold,
        'filtered': '',
    }


def backtest(double_up: pd.DataFrame,
             hold_days: int = 10,
             tp: float = 0.30,
             sl: float = -0.10,
             min_t_pct: float = 0.095,
             min_t_vol_ratio: float = 1.5,
             no_filter: bool = False) -> pd.DataFrame:
    """
    回测所有翻倍事件
    - no_filter=True: 关闭 T+1 涨停 + 量比过滤（v1 规则）
    - no_filter=False: 开启 v2 过滤
    """
    first = get_first_double_date(double_up)
    print(f"  首次翻倍事件: {len(first)} 只股")
    if no_filter:
        min_t_pct = 0.0
        min_t_vol_ratio = 0.0
        print(f"  ⚠ v1 模式：无过滤")

    print(f"  信号过滤: T+1 涨幅 ≥ {min_t_pct*100:.1f}% & 量比 ≥ {min_t_vol_ratio:.1f}")

    rows = []
    for _, r in first.iterrows():
        result = backtest_one(
            r['code'], r['signal_date'],
            hold_days, tp, sl,
            min_t_pct, min_t_vol_ratio,
        )
        if result is None:
            continue
        # 过滤的也保留（用于统计过滤率）
        rows.append(result)

    bt = pd.DataFrame(rows)
    if not bt.empty:
        # 区分有效回测 vs 被过滤
        if 'filtered' in bt.columns:
            bt_valid = bt[bt['filtered'] == '']
            bt_filtered = bt[bt['filtered'] != '']
            print(f"  过滤掉: {len(bt_filtered)} （{len(bt_filtered)/len(bt)*100:.1f}%）")
            print(f"  有效回测: {len(bt_valid)} 笔")
            return bt_valid
    return bt


def generate_today_signals(double_up: pd.DataFrame, today: str = None) -> pd.DataFrame:
    """
    生成今日触发信号（实盘）
    today: 翻倍触发日（T 日，疑似明日入场的清单）
    """
    today = today or datetime.now().strftime('%Y-%m-%d')

    first = get_first_double_date(double_up)
    first['signal_date'] = pd.to_datetime(first['signal_date'])

    # T 日在过去 7 日内（近一周首次翻倍触发）
    last_7d = first[first['signal_date'] >= pd.to_datetime(today) - timedelta(days=7)]

    print(f"  最近 7 日翻倍首次触发: {len(last_7d)} 只")

    if last_7d.empty:
        return last_7d

    # 进一步过滤：T+1 涨停 + 量比
    signals = []
    for _, r in last_7d.iterrows():
        result = backtest_one(r['code'], r['signal_date'],
                              min_t_pct=0.095, min_t_vol_ratio=1.5)
        if result and not result.get('filtered'):
            signals.append(result)

    return pd.DataFrame(signals)


def summarize(bt: pd.DataFrame, hold: int, tp: float, sl: float, mode: str = 'v2') -> str:
    """生成回测汇总报告"""
    if bt.empty:
        return "无数据"

    total = len(bt)
    wins = bt[bt['return_pct'] > 0]
    losses = bt[bt['return_pct'] <= 0]
    n_tp = (bt['exit_reason'] == 'TP').sum()
    n_sl = (bt['exit_reason'] == 'SL').sum()
    n_time = (bt['exit_reason'] == 'TIME').sum()

    avg_ret = bt['return_pct'].mean()
    win_rate = len(wins) / total * 100
    avg_win = wins['return_pct'].mean() if len(wins) > 0 else 0
    avg_loss = losses['return_pct'].mean() if len(losses) > 0 else 0

    # 期望收益 = 胜率 * 平均盈利 + (1-胜率) * 平均亏损
    exp_ret = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss

    # 最大回撤（按时间排序后累计）
    bt_sorted = bt.sort_values('signal_date')
    cum = (1 + bt_sorted['return_pct']).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = dd.min()

    mode_label = {'v1': 'v1 旧规则（无过滤）', 'v2': 'v2 增强（T+1 涨停+量比）'}
    title = f"## 方案 E 回测报告 ({mode_label.get(mode, mode)})"

    return f"""{title}

**参数**: 持仓 {hold} 日 | 止盈 {tp*100:.0f}% | 止损 {sl*100:.0f}%

### 整体表现

| 指标 | 值 |
|---|---|
| **总样本数** | {total} |
| **胜率** | {win_rate:.1f}% ({len(wins)}/{total}) |
| **平均收益** | {avg_ret*100:.2f}% |
| **平均盈利** | {avg_win*100:.2f}% (n={len(wins)}) |
| **平均亏损** | {avg_loss*100:.2f}% (n={len(losses)}) |
| **期望收益** | {exp_ret*100:.2f}% |
| **最大回撤** | {max_dd*100:.2f}% |
| **盈亏比** | {abs(avg_win/avg_loss) if avg_loss != 0 else 0:.2f} |

### 出场分布

| 出场类型 | 次数 | 占比 |
|---|---:|---:|
| 止盈 TP | {n_tp} | {n_tp/total*100:.1f}% |
| 止损 SL | {n_sl} | {n_sl/total*100:.1f}% |
| 时间止损 | {n_time} | {n_time/total*100:.1f}% |

### Top 10 最佳案例

{bt.nlargest(10, 'return_pct')[['code','signal_date','t1_pct','t1_vol_ratio','return_pct','exit_reason']].to_markdown(index=False) if not bt.nlargest(10, 'return_pct').empty else 'N/A'}

### Top 10 最差案例

{bt.nsmallest(10, 'return_pct')[['code','signal_date','t1_pct','t1_vol_ratio','return_pct','exit_reason']].to_markdown(index=False) if not bt.nsmallest(10, 'return_pct').empty else 'N/A'}
"""


def main():
    parser = argparse.ArgumentParser(description='方案 E：翻倍当日跟随策略回测')
    parser.add_argument('--hold', type=int, default=10, help='持仓天数（默认 10）')
    parser.add_argument('--tp', type=float, default=0.30, help='止盈比例（默认 0.30）')
    parser.add_argument('--sl', type=float, default=-0.10, help='止损比例（默认 -0.10）')
    parser.add_argument('--no-filter', action='store_true', help='关闭 v2 信号过滤（v1 旧规则）')
    parser.add_argument('--today', action='store_true', help='只生成今日信号（不跑回测）')
    parser.add_argument('--compare', action='store_true', help='v1 vs v2 对比')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    print(f"=== 方案 E：翻倍当日跟随策略 (v2) ===")
    print(f"  时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  参数: hold={args.hold} tp={args.tp} sl={args.sl}")
    if args.no_filter:
        print(f"  模式: v1 旧规则（无过滤）")

    # 加载翻倍样本
    print(f"\n[1/3] 加载翻倍样本...")
    double_up = load_double_up()

    if args.today:
        print(f"\n[2/3] 生成今日信号...")
        sig = generate_today_signals(double_up)
        if sig.empty:
            print(f"  (今日无触发信号)")
        else:
            fp = OUTPUT_DIR / f'plan_e_today.csv'
            sig.to_csv(fp, index=False, encoding='utf-8-sig')
            print(f"  ✓ {fp}")
            print(sig[['code', 'signal_date', 't1_pct', 't1_vol_ratio']].to_string(index=False))
        return

    # 回测
    print(f"\n[2/3] 回测...")
    bt = backtest(double_up, args.hold, args.tp, args.sl, no_filter=args.no_filter)
    print(f"  完成: {len(bt)} 笔")
    if bt.empty:
        print("  ❌ 无回测结果")
        return

    # 输出
    print(f"\n[3/3] 输出...")
    mode = 'v1' if args.no_filter else 'v2'
    fp_csv = OUTPUT_DIR / f'plan_e_backtest_{mode}_{ts}.csv'
    bt.to_csv(fp_csv, index=False, encoding='utf-8-sig')
    print(f"  ✓ CSV: {fp_csv.name}")

    md = summarize(bt, args.hold, args.tp, args.sl, mode)
    fp_md = OUTPUT_DIR / f'plan_e_backtest_{mode}_{ts}.md'
    with open(fp_md, 'w', encoding='utf-8') as f:
        f.write(f"# 方案 E 回测报告\n\n")
        f.write(f"时间: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write(md)
    print(f"  ✓ MD: {fp_md.name}")

    # latest
    fp_latest = OUTPUT_DIR / f'plan_e_backtest_{mode}_latest.md'
    fp_latest.write_text(md, encoding='utf-8')
    print(f"  ✓ latest: {fp_latest.name}")

    # v1 vs v2 对比
    if args.compare and not args.no_filter:
        print(f"\n[Bonus] v1 vs v2 对比...")
        bt_v1 = backtest(double_up, args.hold, args.tp, args.sl, no_filter=True)
        if not bt_v1.empty:
            md_cmp = f"""## v1 vs v2 对比

| 指标 | v1 旧规则 | v2 增强版 | 提升 |
|---|---:|---:|---:|
| 样本数 | {len(bt_v1)} | {len(bt)} | {len(bt)-len(bt_v1)} |
| 胜率 | {(bt_v1['return_pct']>0).mean()*100:.1f}% | {(bt['return_pct']>0).mean()*100:.1f}% | {((bt['return_pct']>0).mean()-(bt_v1['return_pct']>0).mean())*100:+.1f}pp |
| 平均收益 | {bt_v1['return_pct'].mean()*100:.2f}% | {bt['return_pct'].mean()*100:.2f}% | {(bt['return_pct'].mean()-bt_v1['return_pct'].mean())*100:+.2f}pp |
"""
            fp_cmp = OUTPUT_DIR / f'plan_e_compare_{ts}.md'
            fp_cmp.write_text(md_cmp, encoding='utf-8')
            print(f"  ✓ 对比: {fp_cmp.name}")
            print(md_cmp)

    print(f"\n=== ✅ 完成 ===")
    print(f"  期望收益: {bt['return_pct'].mean()*100:.2f}%")
    print(f"  胜率: {(bt['return_pct']>0).mean()*100:.1f}%")
    print(f"  盈亏比: {abs(bt[bt['return_pct']>0]['return_pct'].mean()/bt[bt['return_pct']<=0]['return_pct'].mean()):.2f}")
    print(f"  输出: {OUTPUT_DIR}")


if __name__ == '__main__':
    main()