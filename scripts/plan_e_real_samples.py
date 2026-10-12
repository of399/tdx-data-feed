#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘 journal → 实盘样本集（回灌 data/double_up + 单独维护）。

把 plan_e_journal.py 的逐笔交易（journal.csv 的 SELL 行 = 已实现交易）抽取为
与方案 E 回测 bt 兼容的样本集：

  data/double_up/real_trades.csv        # 回灌到 double_up 目录（累积、去重）
  data/plan_e/live/real_samples.csv     # 单独维护的实盘样本工作副本

随后 plan_e_backtest.py --real 直接消费该样本集，按**观测收益**跑
--by-year / 滚动窗口，并沿用 n≥30 统计显著性闸口（✓/⚠️）。

列：code,signal_date,entry_date,exit_date,hold_days,return_pct,reason,exit_reason,exch,yr
  - return_pct : 实际成交收益率（pnl_pct），非 K 线重算
  - yr         : signal_date 年份（用于按年分层 / 滚动窗口）
  - exit_reason: 兼容 compute_metrics 的出场统计

实盘/模拟已完全分离：plan_e_journal.py 的 --mode simulate 现写入独立的
journal_sim.csv，实盘 journal.csv 仅含 source='real' 的手动买卖。本脚本只读取
实盘 journal.csv 的 SELL 行；并二次过滤非实盘来源，进一步防止模拟数据污染。

如需在回灌前强制清理 journal.csv 中历史遗留的模拟记录，可加 --clean
（调用 plan_e_journal.py --mode purge-sim）。
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
LIVE_DIR = ROOT / 'data' / 'plan_e' / 'live'
JOURNAL_CSV = LIVE_DIR / 'journal.csv'
REAL_BACKFILL = ROOT / 'data' / 'double_up' / 'real_trades.csv'
REAL_WORKING = LIVE_DIR / 'real_samples.csv'

COLS = ['code', 'signal_date', 'entry_date', 'exit_date', 'hold_days',
        'return_pct', 'reason', 'exit_reason', 'exch', 'yr']


def _exchange_of(code) -> str:
    c = str(code).lower().lstrip('bj').lstrip('sh').lstrip('sz')
    if c.startswith('920') or c[0] == '8':
        return '北交所'
    if c.startswith('60') or c.startswith('68'):
        return '上海'
    if c.startswith('00') or c.startswith('30'):
        return '深圳'
    return '其他'


def build(journal: Path, out: Path) -> pd.DataFrame:
    """从 journal.csv 抽取已实现交易 → 样本集 DataFrame（含去重累积）。"""
    if not journal.exists():
        print(f"  ⚠ journal 不存在: {journal}（尚无实盘交易）")
        new = pd.DataFrame(columns=COLS)
    else:
        j = pd.read_csv(journal)
        if 'pnl_pct' not in j.columns:
            print("  ⚠ journal 缺少 pnl_pct 列，无法抽取已实现收益")
            new = pd.DataFrame(columns=COLS)
        else:
            sells = j[(j['action'] == 'SELL') & j['pnl_pct'].notna()].copy()
            # 仅保留实盘记录，排除模拟污染：
            # - 含 source 列：保留 source=='real'
            # - 旧版无 source：实盘手动 reason='MANUAL'，模拟为 ENTRY/TP/SL/END
            if 'source' in sells.columns:
                n0 = len(sells)
                sells = sells[sells['source'] == 'real']
                if n0 != len(sells):
                    print(f"  ℹ 已过滤非实盘来源 {n0 - len(sells)} 行（模拟 SELL）")
            else:
                n0 = len(sells)
                sells = sells[sells['reason'] == 'MANUAL']
                if n0 != len(sells):
                    print(f"  ℹ 已过滤模拟 SELL（reason!=MANUAL）{n0 - len(sells)} 行")
            if sells.empty:
                print(f"  ⚠ journal 无实盘已实现 SELL 记录（{len(j)} 行）")
                new = pd.DataFrame(columns=COLS)
            else:
                new = pd.DataFrame({
                    'code': sells['code'].astype(str).str.zfill(6),
                    'signal_date': sells['signal_date'].astype(str),
                    'entry_date': sells['entry_date'].astype(str),
                    'exit_date': sells['date'].astype(str),
                    'hold_days': sells['hold_days'].fillna(0).astype(int),
                    'return_pct': sells['pnl_pct'].astype(float),
                    'reason': sells['reason'].astype(str),
                    'exit_reason': sells['reason'].astype(str),
                })
                new['exch'] = new['code'].apply(_exchange_of)
                new['yr'] = pd.to_datetime(new['signal_date']).dt.year
                new = new[COLS]

    # 与既有回灌样本累积、去重（按 code+entry_date+exit_date）
    if out.exists():
        old = pd.read_csv(out)
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new
    if not combined.empty:
        before = len(combined)
        combined = combined.drop_duplicates(subset=['code', 'entry_date', 'exit_date'], keep='last')
        combined = combined.sort_values(['exit_date', 'code']).reset_index(drop=True)
        print(f"  样本累积：新增 {len(new)} 笔，去重后共 {len(combined)} 笔（原 {before}）")
    else:
        print("  样本累积：0 笔")
    return combined


def main():
    ap = argparse.ArgumentParser(description='实盘 journal → 实盘样本集（回灌 data/double_up）')
    ap.add_argument('--journal', default=str(JOURNAL_CSV), help='journal.csv 路径')
    ap.add_argument('--out', default=str(REAL_BACKFILL), help='回灌目标（data/double_up/real_trades.csv）')
    ap.add_argument('--clean', action='store_true',
                    help='构建前先清理 journal.csv 中的模拟交易记录（调用 plan_e_journal.py --mode purge-sim）')
    args = ap.parse_args()

    print(f"=== 实盘样本集构建 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===")
    print(f"  journal : {args.journal}")
    print(f"  回灌目标: {args.out}")
    if args.clean:
        print("  → 先清理 journal.csv 中的模拟污染记录...")
        subprocess.run([sys.executable, 'scripts/plan_e_journal.py', '--mode', 'purge-sim'],
                       cwd=ROOT, check=False)
    combined = build(Path(args.journal), Path(args.out))

    # 写回灌目标（累积）
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(Path(args.out), index=False, encoding='utf-8-sig')
    print(f"  ✓ 回灌: {args.out}")

    # 写单独维护的工作副本
    REAL_WORKING.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(REAL_WORKING, index=False, encoding='utf-8-sig')
    print(f"  ✓ 工作副本: {REAL_WORKING}")

    # 摘要 + n≥30 进度
    if not combined.empty:
        by_year = combined.groupby('yr').size()
        by_exch = combined.groupby('exch').size()
        win = (combined['return_pct'] > 0).mean() * 100
        print(f"\n  总样本: {len(combined)} 笔 | 胜率 {win:.1f}% | 均收益 {combined['return_pct'].mean()*100:+.2f}%")
        print("  按年份（✓=已达 n≥30 可作统计推断）:")
        for y, n in by_year.items():
            print(f"    {y}: {n} 笔 {'✓' if n >= 30 else '⚠️'}")
        print("  按交易所:")
        for e, n in by_exch.items():
            print(f"    {e}: {n} 笔")
    else:
        print("  （空样本集，待实盘成交后重跑本脚本累积）")
    print(f"\n=== ✅ 完成 ===")


if __name__ == '__main__':
    main()
