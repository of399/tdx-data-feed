#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案 E 实盘跟踪系统 v1
=====================

【功能】
  1. 每日 18:30 自动跑方案 E 当日信号（plan_e_backtest.py --today）
  2. 维护交易日志（journal.csv）：每笔交易的买卖价、收益、出场原因
  3. 维护持仓状态（positions.json）：当前持仓 + 监控价格
  4. 每日盈亏统计 + 周报
  5. 模拟交易模式 + 真实交易模式可切换

【目录结构】
  data/plan_e/live/
  ├── journal.csv                 # 实盘交易日志（仅实盘：手动 buy/sell，source=real）
  ├── journal_sim.csv             # 模拟交易日志（--mode simulate 独立写入，与实盘完全分离）
  ├── positions.json              # 当前持仓
  ├── daily_signals/              # 每日信号归档
  │   ├── signals_20260928.json
  │   ├── signals_20260930.json
  │   └── ...
  ├── weekly_reports/             # 周报
  │   ├── week_20260928.md
  │   └── ...
  └── pnl_summary.csv             # 累计 P&L

【运行模式】
  --mode daily           # 每日跑（生成信号 + 更新持仓）
  --mode weekly          # 周报生成
  --mode status          # 查看当前状态（持仓 + 累计 P&L）
  --mode buy --code 600519 --price 10.5  # 手动买入（实盘用）
  --mode sell --code 600519 --price 12.0 --reason TP  # 手动卖出
  --mode simulate         # 模拟模式（用历史数据回测 1 个月）

【示例】
  # 1. 每日定时跑（systemd timer）
  venv/bin/python scripts/plan_e_journal.py --mode daily

  # 2. 模拟模式（用历史 2026-08-27 ~ 2026-09-27 回测一个月，写入独立的 journal_sim.csv）
  venv/bin/python scripts/plan_e_journal.py --mode simulate --start 2026-08-27 --end 2026-09-27 --capital 100000

  # 7. 清理实盘 journal.csv 中残留的模拟记录（如历史遗留污染）
  venv/bin/python scripts/plan_e_journal.py --mode purge-sim

  # 3. 手动买入（实盘）
  venv/bin/python scripts/plan_e_journal.py --mode buy --code 600519 --price 10.50 --qty 10000

  # 4. 手动卖出
  venv/bin/python scripts/plan_e_journal.py --mode sell --code 600519 --price 12.30 --reason TP

  # 5. 查看状态
  venv/bin/python scripts/plan_e_journal.py --mode status

  # 6. 生成周报
  venv/bin/python scripts/plan_e_journal.py --mode weekly

【输出】
  - journal.csv：实盘交易流水（仅 source=real 的买/卖，与模拟完全分离）
  - journal_sim.csv：模拟交易流水（--mode simulate 专用，不会污染实盘）
  - positions.json：当前持仓 + 当日信号
  - weekly_reports/week_YYYYMMDD.md：周报

【资金规则】
  - 初始资金：默认 100,000 RMB（可配置）
  - 单股仓位：默认 10% (10,000)
  - 最大持仓：10 只
  - 触发顺序：TP > SL > TIME
"""

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

warnings.filterwarnings('ignore')

ROOT = Path(__file__).parent.parent

# 仓库根目录加入 sys.path，使 `import tdxfeed` 在直接运行脚本时可用。
# tdxfeed 未以可编辑方式安装；predict.py/plan_e_backtest.py 借 _common 间接引导，
# 本脚本未导入 _common，需显式引导，否则 ModuleNotFoundError: No module named 'tdxfeed'。
sys.path.insert(0, str(ROOT))

LIVE_DIR = ROOT / 'data' / 'plan_e' / 'live'
JOURNAL_FILE = LIVE_DIR / 'journal.csv'          # 实盘交易日志（仅实盘，手动 buy/sell）
SIM_JOURNAL_FILE = LIVE_DIR / 'journal_sim.csv'   # 模拟交易独立日志（与实盘完全分离）
POSITIONS_FILE = LIVE_DIR / 'positions.json'
SIGNALS_DIR = LIVE_DIR / 'daily_signals'
WEEKLY_DIR = LIVE_DIR / 'weekly_reports'
PNL_FILE = LIVE_DIR / 'pnl_summary.csv'

# journal 来源标记：实盘=real（写入 journal.csv），模拟=sim（写入 journal_sim.csv）
SOURCE_REAL = 'real'
SOURCE_SIM = 'sim'

# 默认参数
INITIAL_CAPITAL = 100000       # 初始资金 10 万
POSITION_PCT = 0.10            # 单股 10%
MAX_POSITIONS = 10             # 最大持仓
TP_PCT = 0.30                  # 止盈
SL_PCT = -0.10                 # 止损
HOLD_DAYS = 10                 # 时间止损


def ensure_dirs():
    for d in [LIVE_DIR, SIGNALS_DIR, WEEKLY_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ============== 持仓管理 ==============

def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text(encoding='utf-8'))
    return {
        'cash': INITIAL_CAPITAL,
        'positions': {},  # code: {qty, entry_price, entry_date, signal_date, target_tp, target_sl, hold_until}
        'history': [],    # 已平仓记录
    }


def save_positions(pos: dict):
    POSITIONS_FILE.write_text(json.dumps(pos, ensure_ascii=False, indent=2),
                              encoding='utf-8')


# ============== journal ==============

def append_journal(records: list):
    """追加记录到 journal.csv（实盘日志），统一标记 source=real"""
    if not records:
        return
    df = pd.DataFrame(records)
    if 'source' not in df.columns:
        df['source'] = SOURCE_REAL
    if JOURNAL_FILE.exists():
        old = pd.read_csv(JOURNAL_FILE)
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(JOURNAL_FILE, index=False, encoding='utf-8-sig')


def append_sim_journal(records: list):
    """追加模拟交易记录到独立的 journal_sim.csv（与实盘 journal.csv 完全分离）"""
    if not records:
        return
    df = pd.DataFrame(records)
    df['source'] = SOURCE_SIM
    if SIM_JOURNAL_FILE.exists():
        old = pd.read_csv(SIM_JOURNAL_FILE)
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(SIM_JOURNAL_FILE, index=False, encoding='utf-8-sig')


# ============== 当日信号 ==============

def get_daily_signals(today: str = None) -> pd.DataFrame:
    """调方案 E 脚本获取当日信号"""
    import subprocess
    today = today or datetime.now().strftime('%Y-%m-%d')
    # 调 plan_e_backtest --today
    result = subprocess.run(
        ['venv/bin/python', 'scripts/plan_e_backtest.py', '--today'],
        cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    print(result.stdout)

    sig_file = LIVE_DIR.parent / 'plan_e_today.csv'
    if sig_file.exists():
        sig = pd.read_csv(sig_file)
        # 归档
        archive = SIGNALS_DIR / f'signals_{today.replace("-", "")}.csv'
        sig.to_csv(archive, index=False, encoding='utf-8-sig')
        return sig
    return pd.DataFrame()


# ============== 模拟交易 ==============

def simulate_one_month(start_date: str, end_date: str, capital: float = INITIAL_CAPITAL):
    """
    用历史 1 个月数据模拟
    - 每天跑方案 E 信号
    - 触发即开仓（按当日开盘价）
    - 监控 TP/SL/TIME 退出
    """
    print(f"=== 方案 E 模拟交易 {start_date} ~ {end_date} ===")
    print(f"  初始资金: {capital:,.0f}")
    print(f"  单股仓位: {POSITION_PCT*100:.0f}%")
    print(f"  TP/SL/TIME: {TP_PCT*100:.0f}% / {SL_PCT*100:.0f}% / {HOLD_DAYS}日")

    ensure_dirs()
    pos = {
        'cash': capital,
        'positions': {},
        'history': [],
    }

    # 用 parquet/daily 数据
    dates = pd.date_range(start=start_date, end=end_date, freq='B')  # 工作日
    all_a = json.loads((ROOT / 'data' / 'all_a_stocks.json').read_text())
    all_codes = [str(c).zfill(6) for c in all_a.get('codes', [])]

    PARQUET = ROOT / 'data' / 'parquet' / 'daily'
    journal_records = []

    for trade_date in dates:
        date_str = trade_date.strftime('%Y-%m-%d')
        print(f"\n[{date_str}] ", end='')

        # Step 1: 检查现有持仓是否触发 TP/SL/TIME
        closed = []
        for code, p in list(pos['positions'].items()):
            fp = PARQUET / f'{code}.parquet'
            if not fp.exists():
                fp = PARQUET / f'sh{code}.parquet'
            if not fp.exists():
                fp = PARQUET / f'sz{code}.parquet'
            if not fp.exists():
                fp = PARQUET / f'bj{code}.parquet'
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
                df['date'] = pd.to_datetime(df['date'])
                today_data = df[df['date'] == trade_date]
                if today_data.empty:
                    continue
                t = today_data.iloc[0]
                high = float(t['high'])
                low = float(t['low'])
                entry = p['entry_price']
                # 检查 TP/SL
                max_gain = (high - entry) / entry
                max_loss = (low - entry) / entry
                hold_days = (trade_date - pd.to_datetime(p['entry_date'])).days

                reason = None
                exit_price = None
                if max_gain >= TP_PCT:
                    reason = 'TP'
                    exit_price = entry * (1 + TP_PCT)
                elif max_loss <= SL_PCT:
                    reason = 'SL'
                    exit_price = entry * (1 + SL_PCT)
                elif hold_days >= HOLD_DAYS:
                    reason = 'TIME'
                    exit_price = float(t['close'])

                if reason:
                    qty = p['qty']
                    proceeds = exit_price * qty
                    pnl = proceeds - entry * qty
                    pnl_pct = (exit_price - entry) / entry
                    pos['cash'] += proceeds
                    closed.append(code)
                    journal_records.append({
                        'date': date_str,
                        'code': code,
                        'action': 'SELL',
                        'qty': qty,
                        'price': round(exit_price, 2),
                        'amount': round(proceeds, 2),
                        'pnl': round(pnl, 2),
                        'pnl_pct': round(pnl_pct, 4),
                        'reason': reason,
                        'hold_days': hold_days,
                        'entry_date': p['entry_date'],
                        'signal_date': p['signal_date'],
                    })
                    print(f"SELL {code} {reason} {pnl_pct*100:+.1f}% ", end='')
            except Exception as e:
                print(f"ERR {code}: {e} ", end='')

        for c in closed:
            del pos['positions'][c]

        # Step 2: 今日触发信号 → 买入
        # 简化：用方案 C 的 peak_date 当日作为信号触发日（真实环境用 --today）
        # 这里用 end_date 前 7 日内的所有 peak_date
        if len(pos['positions']) < MAX_POSITIONS and pos['cash'] > 0:
            # 简化为：每个交易日固定买 1 只（按代码顺序遍历，找到翻倍的就买）
            # 生产环境应该用 plan_e_backtest --today 信号
            double_up = pd.read_csv(ROOT / 'data' / 'double_up' / 'double_up_2020_2026_detail.csv')
            double_up['peak_date'] = pd.to_datetime(double_up['peak_date'])

            # 找今日之前的 peak_date（v2 过滤）
            todays = double_up[double_up['peak_date'] == trade_date - timedelta(days=1)]
            if not todays.empty:
                # 每个交易日最多开仓 1 只（避免过曝）
                for _, row in todays.head(1).iterrows():
                    code = str(row['code']).zfill(6).lstrip('bj')
                    if code in pos['positions']:
                        continue
                    # 找今日开盘价
                    fp = PARQUET / f'{code}.parquet'
                    if not fp.exists():
                        fp = PARQUET / f'sh{code}.parquet'
                    if not fp.exists():
                        fp = PARQUET / f'sz{code}.parquet'
                    if not fp.exists():
                        fp = PARQUET / f'bj{code}.parquet'
                    if not fp.exists():
                        continue
                    try:
                        df = pd.read_parquet(fp)
                        df['date'] = pd.to_datetime(df['date'])
                        today_data = df[df['date'] == trade_date]
                        if today_data.empty:
                            continue
                        t = today_data.iloc[0]
                        open_price = float(t['open'])
                        if open_price <= 0:
                            continue
                        # 检查 T+1 涨停 + 量比（v2 过滤）
                        # 简化：跳过此步骤
                        # 开仓
                        amount = pos['cash'] * POSITION_PCT
                        qty = int(amount / open_price / 100) * 100  # 整手
                        if qty <= 0:
                            continue
                        cost = qty * open_price
                        if cost > pos['cash']:
                            continue
                        pos['cash'] -= cost
                        pos['positions'][code] = {
                            'qty': qty,
                            'entry_price': round(open_price, 2),
                            'entry_date': date_str,
                            'signal_date': str(row['peak_date'])[:10],
                            'target_tp': TP_PCT,
                            'target_sl': SL_PCT,
                        }
                        journal_records.append({
                            'date': date_str,
                            'code': code,
                            'action': 'BUY',
                            'qty': qty,
                            'price': round(open_price, 2),
                            'amount': round(cost, 2),
                            'pnl': 0,
                            'pnl_pct': 0,
                            'reason': 'ENTRY',
                            'hold_days': 0,
                            'entry_date': date_str,
                            'signal_date': str(row['peak_date'])[:10],
                        })
                        print(f"BUY {code}@{open_price:.2f}×{qty} ", end='')
                        break
                    except Exception:
                        continue

        # 日终状态
        n_pos = len(pos['positions'])
        if n_pos > 0:
            print(f"| {n_pos} holdings, cash={pos['cash']:,.0f}")
        else:
            print(f"| no holdings, cash={pos['cash']:,.0f}")

    # 最终平仓
    if pos['positions']:
        # 用最后一天的收盘价平仓
        last_date = dates[-1].strftime('%Y-%m-%d')
        for code, p in list(pos['positions'].items()):
            fp = PARQUET / f'{code}.parquet'
            for c in [f'{code}.parquet', f'sh{code}.parquet', f'sz{code}.parquet', f'bj{code}.parquet']:
                f = PARQUET / c
                if f.exists():
                    fp = f
                    break
            try:
                df = pd.read_parquet(fp)
                df['date'] = pd.to_datetime(df['date'])
                today_data = df[df['date'] == dates[-1]]
                if today_data.empty:
                    continue
                t = today_data.iloc[0]
                exit_price = float(t['close'])
                qty = p['qty']
                proceeds = exit_price * qty
                pnl = proceeds - p['entry_price'] * qty
                pnl_pct = (exit_price - p['entry_price']) / p['entry_price']
                pos['cash'] += proceeds
                journal_records.append({
                    'date': last_date,
                    'code': code,
                    'action': 'SELL',
                    'qty': qty,
                    'price': round(exit_price, 2),
                    'amount': round(proceeds, 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 4),
                    'reason': 'END',
                    'hold_days': (dates[-1] - pd.to_datetime(p['entry_date'])).days,
                    'entry_date': p['entry_date'],
                    'signal_date': p['signal_date'],
                })
            except Exception:
                pass

    # 保存（模拟交易写入独立的 journal_sim.csv，与实盘 journal.csv 完全分离）
    ensure_dirs()
    append_sim_journal(journal_records)

    # 汇总
    df_j = pd.DataFrame(journal_records)
    print(f"\n=== 模拟交易汇总 ===")
    print(f"  初始资金: {capital:,.0f}")
    print(f"  期末资金: {pos['cash']:,.0f}")
    print(f"  总盈亏: {pos['cash'] - capital:+,.0f} ({(pos['cash']/capital-1)*100:+.2f}%)")
    print(f"  总交易笔数: {len(df_j[df_j['action']=='BUY'])}")
    sells = df_j[df_j['action']=='SELL']
    wins = sells[sells['pnl_pct'] > 0]
    print(f"  胜率: {(sells['pnl_pct']>0).mean()*100:.1f}%")
    print(f"  平均收益: {sells['pnl_pct'].mean()*100:+.2f}%")
    print(f"  交易明细 → {SIM_JOURNAL_FILE}（与实盘 journal.csv 分离）")

    return df_j


# ============== 实盘每日模式 ==============

def mode_daily():
    """每日模式：生成信号 + 检查持仓"""
    ensure_dirs()
    pos = load_positions()

    today = datetime.now().strftime('%Y-%m-%d')
    print(f"=== 方案 E 每日模式 {today} ===")
    print(f"  当前持仓: {len(pos['positions'])} 只")
    print(f"  可用现金: {pos['cash']:,.0f}")

    # 生成信号
    sig = get_daily_signals(today)
    if not sig.empty:
        print(f"\n  今日触发信号: {len(sig)} 只")
        print(sig[['code', 'signal_date', 't1_pct', 't1_vol_ratio']].to_string(index=False))
    else:
        print(f"\n  今日无触发信号")


def mode_status():
    """查看当前状态"""
    pos = load_positions()
    print(f"=== 方案 E 状态 ===")
    print(f"  现金: {pos['cash']:,.0f}")
    print(f"  持仓: {len(pos['positions'])} 只")
    if pos['positions']:
        for code, p in pos['positions'].items():
            print(f"    {code} {p['qty']}@{p['entry_price']:.2f} 入场 {p['entry_date']}")

    if JOURNAL_FILE.exists():
        df = pd.read_csv(JOURNAL_FILE)
        sells = df[df['action']=='SELL']
        buys = df[df['action']=='BUY']
        print(f"\n  历史交易: {len(buys)} 买入 / {len(sells)} 卖出")
        if len(sells) > 0:
            print(f"  累计盈亏: {sells['pnl'].sum():+,.0f}")
            print(f"  胜率: {(sells['pnl_pct']>0).mean()*100:.1f}%")
            print(f"  平均收益: {sells['pnl_pct'].mean()*100:+.2f}%")


def mode_buy(code: str, price: float, qty: int):
    """手动买入"""
    ensure_dirs()
    pos = load_positions()

    if len(pos['positions']) >= MAX_POSITIONS:
        print(f"❌ 已达最大持仓 {MAX_POSITIONS}")
        return

    cost = price * qty
    if cost > pos['cash']:
        print(f"❌ 现金不足: 需要 {cost:,.0f}, 可用 {pos['cash']:,.0f}")
        return

    pos['cash'] -= cost
    pos['positions'][code] = {
        'qty': qty,
        'entry_price': price,
        'entry_date': datetime.now().strftime('%Y-%m-%d'),
        'signal_date': datetime.now().strftime('%Y-%m-%d'),
        'target_tp': TP_PCT,
        'target_sl': SL_PCT,
    }
    save_positions(pos)

    append_journal([{
        'date': datetime.now().strftime('%Y-%m-%d'),
        'code': code,
        'action': 'BUY',
        'qty': qty,
        'price': price,
        'amount': cost,
        'pnl': 0,
        'pnl_pct': 0,
        'reason': 'MANUAL',
        'hold_days': 0,
        'entry_date': datetime.now().strftime('%Y-%m-%d'),
        'signal_date': datetime.now().strftime('%Y-%m-%d'),
        'source': SOURCE_REAL,
    }])
    print(f"✓ 买入 {code} {qty}@{price} 花费 {cost:,.0f}")
    print(f"  剩余现金: {pos['cash']:,.0f}")


def mode_sell(code: str, price: float, reason: str = 'MANUAL'):
    """手动卖出"""
    pos = load_positions()
    if code not in pos['positions']:
        print(f"❌ 未持有 {code}")
        return

    p = pos['positions'][code]
    proceeds = price * p['qty']
    pnl = proceeds - p['entry_price'] * p['qty']
    pnl_pct = (price - p['entry_price']) / p['entry_price']

    pos['cash'] += proceeds
    del pos['positions'][code]
    save_positions(pos)

    entry_date = p['entry_date']
    hold_days = (datetime.now() - pd.to_datetime(entry_date)).days

    append_journal([{
        'date': datetime.now().strftime('%Y-%m-%d'),
        'code': code,
        'action': 'SELL',
        'qty': p['qty'],
        'price': price,
        'amount': proceeds,
        'pnl': round(pnl, 2),
        'pnl_pct': round(pnl_pct, 4),
        'reason': reason,
        'hold_days': hold_days,
        'entry_date': entry_date,
        'signal_date': p['signal_date'],
        'source': SOURCE_REAL,
    }])
    print(f"✓ 卖出 {code} {p['qty']}@{price} 收入 {proceeds:,.0f}")
    print(f"  本笔盈亏: {pnl:+,.0f} ({pnl_pct*100:+.2f}%)")
    print(f"  当前现金: {pos['cash']:,.0f}")


def mode_weekly():
    """生成周报"""
    if not JOURNAL_FILE.exists():
        print("❌ 无交易记录")
        return
    ensure_dirs()
    df = pd.read_csv(JOURNAL_FILE)
    df['date'] = pd.to_datetime(df['date'])
    last_week = df[df['date'] >= datetime.now() - timedelta(days=7)]

    if last_week.empty:
        print("本周无交易")
        return

    week_start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    week_end = datetime.now().strftime('%Y-%m-%d')
    week_id = week_start.replace('-', '')

    buys = last_week[last_week['action']=='BUY']
    sells = last_week[last_week['action']=='SELL']

    md = f"""# 方案 E 周报 ({week_start} ~ {week_end})

## 交易概况

| 指标 | 值 |
|---|---|
| 买入笔数 | {len(buys)} |
| 卖出笔数 | {len(sells)} |
| 净盈亏 | {sells['pnl'].sum():+,.0f} |
| 胜率 | {(sells['pnl_pct']>0).mean()*100:.1f}% |
| 平均收益 | {sells['pnl_pct'].mean()*100:+.2f}% |
| 最大单笔盈利 | {sells['pnl'].max():+,.0f} |
| 最大单笔亏损 | {sells['pnl'].min():+,.0f} |

## 出场类型分布

{sells.groupby('reason').size().to_frame('count').to_markdown() if not sells.empty else '无'}

## 交易明细

{last_week[['date','code','action','qty','price','pnl','pnl_pct','reason','hold_days']].to_markdown(index=False)}

---
生成时间: {datetime.now().isoformat(timespec='seconds')}
"""

    fp = WEEKLY_DIR / f'week_{week_id}.md'
    fp.write_text(md, encoding='utf-8')
    print(f"✓ 周报已生成: {fp}")
    print(md[:500])


def mode_purge_sim():
    """清理实盘 journal.csv 中被模拟交易污染的记录，确保实盘与模拟完全分离。

    判别逻辑（兼容历史遗留数据）：
      - 含 `source` 列：删除 source != 'real' 的行（模拟交易统一标记 source='sim'）。
      - 旧版无 `source` 列：实盘手动交易 reason='MANUAL'，模拟为 ENTRY/TP/SL/END，
        故删除 reason != 'MANUAL' 的行。
    仅作用于 data/plan_e/live/journal.csv，不动独立的 journal_sim.csv。
    """
    if not JOURNAL_FILE.exists():
        print("❌ 无 journal.csv")
        return
    df = pd.read_csv(JOURNAL_FILE)
    if 'source' in df.columns:
        before = len(df)
        df = df[df['source'] == SOURCE_REAL]
        removed = before - len(df)
    else:
        # 遗留数据兜底：实盘手动 reason='MANUAL'
        before = len(df)
        df = df[df['reason'] == 'MANUAL']
        removed = before - len(df)
    if removed:
        df.to_csv(JOURNAL_FILE, index=False, encoding='utf-8-sig')
        print(f"✓ 已清理模拟污染记录 {removed} 行，保留实盘 {len(df)} 行 → {JOURNAL_FILE}")
    else:
        print(f"✓ journal.csv 无模拟污染记录（保留 {len(df)} 行）")


# ============== Main ==============

def main():
    parser = argparse.ArgumentParser(description='方案 E 实盘跟踪系统')
    parser.add_argument('--mode', required=True,
                        choices=['daily', 'weekly', 'status', 'buy', 'sell', 'simulate', 'purge-sim'],
                        help='运行模式（purge-sim=清理 journal.csv 中的模拟污染记录）')
    parser.add_argument('--code', help='股票代码（buy/sell 用）')
    parser.add_argument('--price', type=float, help='价格（buy/sell 用）')
    parser.add_argument('--qty', type=int, help='数量（buy 用）')
    parser.add_argument('--reason', default='MANUAL', help='卖出原因（buy/sell 用）')
    parser.add_argument('--start', help='模拟开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end', help='模拟结束日期 (YYYY-MM-DD)')
    parser.add_argument('--capital', type=float, default=INITIAL_CAPITAL,
                        help=f'初始资金（默认 {INITIAL_CAPITAL}）')
    parser.add_argument('--no-freshness-check', action='store_true', help='跳过数据新鲜度校验')
    args = parser.parse_args()

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    if args.mode == 'daily':
        mode_daily()
    elif args.mode == 'weekly':
        mode_weekly()
    elif args.mode == 'status':
        mode_status()
    elif args.mode == 'buy':
        if not (args.code and args.price and args.qty):
            print("❌ buy 需要 --code --price --qty")
            sys.exit(1)
        mode_buy(args.code, args.price, args.qty)
    elif args.mode == 'sell':
        if not (args.code and args.price):
            print("❌ sell 需要 --code --price")
            sys.exit(1)
        mode_sell(args.code, args.price, args.reason)
    elif args.mode == 'simulate':
        if not (args.start and args.end):
            print("❌ simulate 需要 --start --end (YYYY-MM-DD)")
            sys.exit(1)
        simulate_one_month(args.start, args.end, args.capital)
    elif args.mode == 'purge-sim':
        mode_purge_sim()


if __name__ == '__main__':
    main()
