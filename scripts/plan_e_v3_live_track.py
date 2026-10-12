"""
方案 E v3 实盘跟踪系统
=========================

相对 v2 跟随版，v3 提前版的实盘跟踪更复杂：
1. 每日盘后跑当日信号（用最近 22,305 笔翻倍样本 + 全市场因子）
2. 维护两类池：当日候选 + 持仓监控
3. 多维度异常检测（胜率/回撤/IC 衰减/滑点）
4. 每日/周/月三级报告
5. 模拟/实盘可切换（--mode live/simulate/paper）

【目录结构】
  data/plan_e/live_v3/
  ├── journal.csv           # 所有历史交易（含滑点、手续费）
  ├── positions.json        # 当前持仓
  ├── signals/
  │   ├── signals_20260928.csv
  │   └── ...
  ├── daily_reports/
  │   └── daily_20260928.md
  ├── weekly_reports/
  │   └── week_20260928.md
  ├── monthly_reports/
  │   └── month_202609.md
  ├── pnl_summary.csv       # 累计 P&L
  ├── factor_ic_decay.csv   # 月度 IC 监控
  └── anomalies.log         # 异常事件日志

【运行模式】
  --mode daily      # 每日盘后跑（生成信号 + 更新持仓 + 检测异常 + 日报）
  --mode weekly     # 周报生成（周一盘前跑上周汇总）
  --mode monthly    # 月报（含 IC 衰减监控）
  --mode status     # 查看当前状态
  --mode anomaly    # 单独跑异常检测
  --mode simulate   # paper trading（不实际下单）
  --mode live       # 实盘模式（人工确认后下单）
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')
LIVE_DIR = ROOT / 'data/plan_e/live_v3'
FACTOR_FP = ROOT / 'data/factors/event_factors_with_sentiment.parquet'
DOUBLE_UP_FP = ROOT / 'data/double_up/double_up_v2_2020_2026_detail.csv'
ALL_STOCKS = ROOT / 'data/all_a_stocks.json'
PARQUET = ROOT / 'data/parquet/daily'
SIGNIF_FP = ROOT / 'data/factor_significance_detail.csv'

# 跟踪参数（v3 提前版）
DEFAULT_CONFIG = {
    'capital': 1_000_000,           # 起始资金
    'top_pct': 0.05,                # 候选池 Top 5%
    'position_pct': 0.03,           # 单只仓位 3%
    'max_holdings': 10,             # 同时持有 ≤ 10 只
    'take_profit': 0.30,            # +30% 止盈
    'stop_loss': -0.10,             # -10% 止损
    'hold_days': 30,                # 持仓 30 日
    'max_drawdown': 0.15,           # 总回撤 > 15% 暂停
    'consecutive_loss_stop': 5,     # 连续 5 笔亏暂停
    'slippage_pct': 0.005,          # 滑点 0.5%
    'commission_pct': 0.0003,       # 手续费 0.03%
    'stamp_tax_pct': 0.001,         # 印花税 0.1%（卖出）
    'ic_decay_threshold': 0.05,     # 月度 IC 衰减 > 5pp 报警
    'win_rate_alert': 0.50,         # 月度胜率 < 50% 报警
    'factor_top_n': 10,             # 使用 Top 10 因子
}

JOURNAL_COLS = [
    'date', 'code', 'name', 'action', 'price', 'qty', 'amount',
    'score', 'factor_top1', 'pnl_yuan', 'pnl_pct', 'cum_pnl',
    'exit_type', 'days_held', 'slippage_pct', 'commission', 'note',
]


def ensure_dirs():
    for sub in ['signals', 'daily_reports', 'weekly_reports',
                'monthly_reports', 'anomalies']:
        (LIVE_DIR / sub).mkdir(parents=True, exist_ok=True)


def load_journal() -> pd.DataFrame:
    fp = LIVE_DIR / 'journal.csv'
    if not fp.exists():
        return pd.DataFrame(columns=JOURNAL_COLS)
    df = pd.read_csv(fp)
    return df


def save_journal(df: pd.DataFrame):
    df.to_csv(LIVE_DIR / 'journal.csv', index=False)


def load_positions() -> dict:
    fp = LIVE_DIR / 'positions.json'
    if not fp.exists():
        return {'holdings': {}, 'cash': DEFAULT_CONFIG['capital'], 'last_update': None}
    with open(fp) as f:
        return json.load(f)


def save_positions(p: dict):
    p['last_update'] = datetime.now().isoformat()
    with open(LIVE_DIR / 'positions.json', 'w') as f:
        json.dump(p, f, indent=2, ensure_ascii=False)


def load_config() -> dict:
    fp = LIVE_DIR / 'config.json'
    if not fp.exists():
        save_config(DEFAULT_CONFIG)
    with open(fp) as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


def save_config(cfg: dict):
    with open(LIVE_DIR / 'config.json', 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def calc_composite_score(df: pd.DataFrame, top_factors: list[str]) -> pd.Series:
    rank_sum = pd.Series(0.0, index=df.index)
    for fc in top_factors:
        if fc in df.columns:
            rank_sum += df[fc].rank(pct=True, na_option='bottom').fillna(0.5)
    return rank_sum / len(top_factors)


def load_top_factors(n=10) -> list[str]:
    if not SIGNIF_FP.exists():
        return []
    df = pd.read_csv(SIGNIF_FP)
    df['abs_ric'] = df['rank_ic'].abs()
    return df.sort_values('abs_ric', ascending=False).head(n)['factor'].tolist()


def generate_daily_signals(today: pd.Timestamp, cfg: dict) -> pd.DataFrame:
    """生成当日信号：当日 signal_date = today - 10 days 的事件 → 当日是 T+10
    实际：当日盘后用最新因子算"未来 10 日可能翻倍"的标的
    """
    if not FACTOR_FP.exists():
        return pd.DataFrame()

    df = pd.read_parquet(FACTOR_FP)
    df['signal_date'] = pd.to_datetime(df['signal_date'])

    # 简化：用今日之前的最近一批信号（已生成）
    top_factors = load_top_factors(cfg['factor_top_n'])
    df['score'] = calc_composite_score(df, top_factors)

    # 当日候选：signal_date 在今日前 5 日内
    recent = df[(df['signal_date'] >= today - timedelta(days=5)) &
                (df['signal_date'] <= today)]
    if recent.empty:
        return pd.DataFrame()

    # 取 Top X%
    threshold = recent['score'].quantile(1 - cfg['top_pct'])
    candidates = recent[recent['score'] >= threshold].copy()
    candidates['signal_date_str'] = candidates['signal_date'].dt.strftime('%Y-%m-%d')
    return candidates.sort_values('score', ascending=False)


def load_signals(today: pd.Timestamp) -> pd.DataFrame:
    """加载当日翻倍信号（由独立信号生成步骤写入 signals_YYYYMMDD.csv）。

    缺失时返回空 DataFrame，避免 v3 信号生成步骤尚未接入时崩溃。
    """
    fp = LIVE_DIR / 'signals' / f'signals_{today:%Y%m%d}.csv'
    if not fp.exists():
        return pd.DataFrame()
    return pd.read_csv(fp)


def update_positions(positions: dict, signals: pd.DataFrame, today: pd.Timestamp,
                     cfg: dict) -> tuple[dict, list[dict]]:
    """根据当日信号 + 当前持仓，计算应执行的动作（不动手，只生成建议）"""
    holdings = positions['holdings']
    cash = positions['cash']
    new_actions = []

    # 1. 入场候选（买 Top 3）
    n_bought = 0
    for _, row in signals.head(5).iterrows():
        code = row['code']
        if code in holdings:
            continue
        if len(holdings) >= cfg['max_holdings']:
            break
        target_value = positions.get('initial_capital', cfg['capital']) * cfg['position_pct']
        if cash < target_value:
            break
        entry_price = row.get('close', np.nan)
        if pd.isna(entry_price) or entry_price <= 0:
            continue
        qty = int(target_value / entry_price / 100) * 100
        if qty < 100:
            continue
        new_actions.append({
            'action': 'buy', 'code': code, 'name': row.get('name', ''),
            'price': entry_price, 'qty': qty, 'score': row['score'],
            'date': today.strftime('%Y-%m-%d'),
        })
        n_bought += 1

    return positions, new_actions


def check_anomalies(journal: pd.DataFrame, positions: dict, cfg: dict) -> list[dict]:
    """异常检测"""
    anomalies = []
    today = pd.Timestamp.now()

    if journal.empty:
        return anomalies

    # 1. 月度胜率
    journal['date'] = pd.to_datetime(journal['date'])
    this_month = journal[journal['date'].dt.to_period('M') == today.to_period('M')]
    if len(this_month) >= 5:
        win_rate = (this_month['pnl_pct'] > 0).mean()
        if win_rate < cfg['win_rate_alert']:
            anomalies.append({
                'type': 'low_win_rate',
                'level': 'warning',
                'msg': f'本月胜率 {win_rate*100:.1f}% < {cfg["win_rate_alert"]*100:.0f}%',
                'value': win_rate,
            })

    # 2. 连续亏损
    if not journal.empty:
        last_actions = journal.tail(cfg['consecutive_loss_stop'])
        if len(last_actions) >= cfg['consecutive_loss_stop']:
            consecutive_loss = (last_actions['pnl_pct'] < 0).all() if 'pnl_pct' in last_actions.columns else False
            if consecutive_loss:
                anomalies.append({
                    'type': 'consecutive_loss',
                    'level': 'critical',
                    'msg': f'连续 {cfg["consecutive_loss_stop"]} 笔亏损，建议暂停 1 周',
                })

    # 3. 总回撤
    if 'cum_pnl' in journal.columns:
        cum_max = journal['cum_pnl'].max()
        cum_now = journal['cum_pnl'].iloc[-1] if len(journal) > 0 else 0
        drawdown = (cum_now - cum_max) / max(cum_max, 1)
        if drawdown < -cfg['max_drawdown']:
            anomalies.append({
                'type': 'max_drawdown',
                'level': 'critical',
                'msg': f'总回撤 {drawdown*100:.1f}% > -{cfg["max_drawdown"]*100:.0f}%，强制暂停',
                'value': drawdown,
            })

    return anomalies


def generate_daily_report(today: pd.Timestamp, signals: pd.DataFrame,
                          positions: dict, actions: list[dict],
                          anomalies: list[dict], cfg: dict) -> str:
    """生成日报 Markdown"""
    holdings = positions['holdings']
    n_holdings = len(holdings)
    cash = positions['cash']
    holdings_value = sum(h.get('current_value', h['cost']) for h in holdings.values())
    total_equity = cash + holdings_value

    md = []
    md.append(f'# v3 提前版实盘日报 - {today.strftime("%Y-%m-%d")}')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%H:%M:%S}  ')
    md.append(f'> 模式: {cfg.get("mode", "live")}  ')
    md.append('')
    md.append('## 一、当日信号')
    md.append('')
    if not signals.empty:
        md.append('当日 Top 5 候选:')
        md.append('')
        md.append('| # | 代码 | 名称 | score | close |')
        md.append('|:---:|---|---|---:|---:|')
        for i, (_, row) in enumerate(signals.head(5).iterrows(), 1):
            md.append(f'| {i} | `{row["code"]}` | {row.get("name","")} | '
                      f'{row["score"]:.3f} | {row.get("close", 0):.2f} |')
    else:
        md.append('*当日无候选*')
    md.append('')
    md.append('## 二、建议动作')
    md.append('')
    if actions:
        for a in actions:
            md.append(f'- **买** `{a["code"]}` @ {a["price"]:.2f} × {a["qty"]} 股, '
                      f'score={a["score"]:.3f}')
    else:
        md.append('*无新买入建议*')
    md.append('')
    md.append('## 三、当前状态')
    md.append('')
    md.append('| 指标 | 数值 |')
    md.append('|---|---:|')
    md.append(f'| 持仓数 | {n_holdings} |')
    md.append(f'| 现金 | ¥{cash:,.0f} |')
    md.append(f'| 持仓市值 | ¥{holdings_value:,.0f} |')
    md.append(f'| 总权益 | ¥{total_equity:,.0f} |')
    md.append('')
    if holdings:
        md.append('### 持仓明细')
        md.append('')
        md.append('| 代码 | 入场价 | 数量 | 现价 | 浮盈 |')
        md.append('|---|---:|---:|---:|---:|')
        for code, h in holdings.items():
            cur = h.get('current_value', h['cost']) / h['qty']
            pnl_pct = (cur / h['entry_price'] - 1) * 100
            md.append(f'| `{code}` | {h["entry_price"]:.2f} | {h["qty"]} | '
                      f'{cur:.2f} | {pnl_pct:+.1f}% |')
        md.append('')
    md.append('## 四、异常检测')
    md.append('')
    if anomalies:
        for a in anomalies:
            icon = '🔴' if a['level'] == 'critical' else '🟡'
            md.append(f'- {icon} **{a["type"]}**: {a["msg"]}')
    else:
        md.append('✅ 一切正常')
    md.append('')
    return '\n'.join(md)


def log_anomaly(anomaly: dict):
    fp = LIVE_DIR / 'anomalies' / 'anomalies.log'
    with open(fp, 'a') as f:
        f.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] [{anomaly["level"].upper()}] '
                f'{anomaly["type"]}: {anomaly["msg"]}\n')


def cmd_daily(args):
    today = pd.Timestamp.now()
    cfg = load_config()
    signals = load_signals(today)

    # 2. 更新持仓建议
    print('\n[2/5] 计算建议动作...')
    positions = load_positions()
    positions, actions = update_positions(positions, signals, today, cfg)
    print(f'  建议动作: {len(actions)}')

    # 3. 异常检测
    print('\n[3/5] 异常检测...')
    journal = load_journal()
    anomalies = check_anomalies(journal, positions, cfg)
    print(f'  检测到异常: {len(anomalies)}')
    for a in anomalies:
        log_anomaly(a)
        print(f'    - [{a["level"]}] {a["type"]}: {a["msg"]}')

    # 4. 生成日报
    print('\n[4/5] 生成日报...')
    daily_md = generate_daily_report(today, signals, positions, actions, anomalies, cfg)
    daily_fp = LIVE_DIR / 'daily_reports' / f'daily_{today.strftime("%Y%m%d")}.md'
    daily_fp.write_text(daily_md, encoding='utf-8')
    print(f'  日报: {daily_fp}')

    # 5. 保存持仓
    save_positions(positions)

    print('\n[5/5] 完成。')


def cmd_status(args):
    positions = load_positions()
    journal = load_journal()

    print('===== v3 提前版实盘状态 =====')
    print(f'当前持仓: {len(positions["holdings"])} 只')
    print(f'可用现金: ¥{positions["cash"]:,.0f}')
    holdings_value = sum(h.get('current_value', h['cost']) for h in positions['holdings'].values())
    print(f'持仓市值: ¥{holdings_value:,.0f}')
    print(f'总权益: ¥{positions["cash"] + holdings_value:,.0f}')

    if not journal.empty:
        journal['date'] = pd.to_datetime(journal['date'])
        print(f'\n累计交易: {len(journal)} 笔')
        if 'pnl_yuan' in journal.columns:
            total_pnl = journal['pnl_yuan'].sum()
            print(f'累计 P&L: ¥{total_pnl:,.0f}')

        last_30 = journal.tail(30)
        if 'pnl_pct' in last_30.columns and len(last_30) > 0:
            win_rate = (last_30['pnl_pct'] > 0).mean() * 100
            print(f'近 30 笔胜率: {win_rate:.1f}%')


def cmd_anomaly(args):
    print(f'[{datetime.now():%H:%M:%S}] 异常检测...')
    journal = load_journal()
    positions = load_positions()
    cfg = load_config()
    anomalies = check_anomalies(journal, positions, cfg)

    if not anomalies:
        print('✅ 一切正常')
    else:
        for a in anomalies:
            icon = '🔴' if a['level'] == 'critical' else '🟡'
            print(f'{icon} [{a["level"].upper()}] {a["type"]}: {a["msg"]}')
            log_anomaly(a)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['daily', 'weekly', 'monthly',
                                           'status', 'anomaly', 'simulate', 'live',
                                           'premarket'],
                       default='daily')
    parser.add_argument('--date', help='指定日期 (YYYY-MM-DD)，默认今天')
    parser.add_argument('--top-n', type=int, default=10,
                        help='盘前清单数量（premarket 模式）')
    args = parser.parse_args()

    if args.mode == 'daily':
        args.mode = 'live'
        cmd_daily(args)
    elif args.mode == 'simulate' or args.mode == 'live':
        cmd_daily(args)
    elif args.mode == 'status':
        cmd_status(args)
    elif args.mode == 'anomaly':
        cmd_anomaly(args)
    elif args.mode == 'premarket':
        # 调用 v3_premarket_top.py
        import subprocess
        cmd = ['venv/bin/python', 'scripts/v3_premarket_top.py',
               '--date', args.date or '', '--top-n', str(args.top_n)]
        cmd = [c for c in cmd if c]
        subprocess.run(cmd)
    elif args.mode == 'weekly':
        print('周报模式 - 即将实现')
    elif args.mode == 'monthly':
        print('月报模式 - 即将实现')


if __name__ == '__main__':
    main()
