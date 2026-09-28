"""
v3 提前版滚动模拟（按时间窗口滚动更新）
==========================================

相对 v3_simulate.py 单窗口的局限，本脚本：
- 每个月底取 1 个月窗口跑模拟
- 21 个月（验证期 2025-01 ~ 2026-09）共 21 次
- 输出：每次结果 + 累计收益曲线 + 综合统计

参数:
- --window-months: 每个窗口的月数（默认 1）
- --step-months: 滚动步长（默认 1）
- --top-pct: 候选池占比（默认 0.05 = Top 5%）
- --capital: 每窗口起始资金（默认 100 万）
- --start: 起始月份（默认 2025-01）
- --end: 结束月份（默认 2026-09）

输出:
- data/plan_e/v3_simulate_rolling_summary.md
- data/plan_e/v3_simulate_rolling_windows.csv
- data/plan_e/v3_simulate_rolling_equity.csv
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')
PARQUET = ROOT / 'data/parquet/daily'
OUT_DIR = ROOT / 'data/plan_e'
CANDIDATE_FP = OUT_DIR / 'v3_early_test_candidates_20260927_2114.csv'


def load_ohlcv(code: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return pd.DataFrame()
    df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    if df['date'].dtype in ('int64', 'int32'):
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    mask = (df['date'] >= start_date) & (df['date'] <= end_date)
    return df[mask].reset_index(drop=True)


def simulate_window(cand: pd.DataFrame, ohlcv_cache: dict,
                    capital: float, top_pct: float = 0.05,
                    take_profit: float = 0.30, stop_loss: float = -0.10,
                    hold_days: int = 30, max_holdings: int = 10) -> dict:
    """单窗口模拟"""
    cand = cand.copy()
    cand['signal_date'] = pd.to_datetime(cand['signal_date'])

    # 取 Top X% score 候选（不区分 label，模拟实盘未知）
    threshold = cand['score'].quantile(1 - top_pct)
    cand_top = cand[cand['score'] >= threshold].copy()

    cash = capital
    holdings = {}
    trades = []
    equity_curve = []

    start_date = cand['signal_date'].min()
    end_date = cand['signal_date'].max()

    # 交易日历
    all_trade_dates = pd.bdate_range(start_date - timedelta(days=2),
                                     end_date + timedelta(days=hold_days + 10))

    trade_dates = set(cand['signal_date'])

    for day in all_trade_dates:
        day_ts = pd.Timestamp(day)

        # 入场：当日 signal_date
        if day_ts in trade_dates:
            today_cands = cand_top[cand_top['signal_date'] == day_ts]
            for _, row in today_cands.iterrows():
                code = row['code']
                if code in holdings or len(holdings) >= max_holdings:
                    continue
                if cash < capital * 0.03:
                    break
                if code not in ohlcv_cache:
                    continue

                df = ohlcv_cache[code]
                next_day = day_ts + timedelta(days=1)
                next_df = df[df['date'] >= next_day]
                if next_df.empty:
                    continue
                entry_price = float(next_df.iloc[0]['open'])
                if entry_price <= 0 or pd.isna(entry_price):
                    continue

                target_value = capital * 0.03
                qty = int(target_value / entry_price / 100) * 100
                if qty < 100:
                    continue
                cost = qty * entry_price
                if cost > cash:
                    qty = int(cash / entry_price / 100) * 100
                    if qty < 100:
                        continue
                    cost = qty * entry_price

                cash -= cost
                holdings[code] = {
                    'qty': qty, 'cost': cost, 'entry_date': next_day,
                    'entry_price': entry_price, 'max_price': entry_price, 'days_held': 0,
                }

        # 出场检查
        to_close = []
        for code, h in holdings.items():
            df = ohlcv_cache[code]
            day_data = df[df['date'] == day_ts]
            if day_data.empty:
                continue
            close = float(day_data.iloc[0]['close'])
            high = float(day_data.iloc[0]['high'])
            h['max_price'] = max(h['max_price'], high)
            h['days_held'] += 1

            ret = close / h['entry_price'] - 1
            if ret >= take_profit:
                to_close.append((code, close, 'tp', ret))
            elif ret <= stop_loss:
                to_close.append((code, close, 'sl', ret))
            elif h['days_held'] >= hold_days:
                to_close.append((code, close, 'time', ret))

        for code, exit_price, exit_type, ret in to_close:
            h = holdings.pop(code)
            proceeds = h['qty'] * exit_price
            cash += proceeds
            trades.append({
                'code': code, 'entry_date': h['entry_date'],
                'entry_price': h['entry_price'], 'exit_date': day_ts,
                'exit_price': exit_price, 'qty': h['qty'],
                'days_held': h['days_held'], 'ret_pct': ret * 100,
                'exit_type': exit_type,
            })

        # 权益
        holdings_value = sum(
            ohlcv_cache[c][ohlcv_cache[c]['date'] == day_ts].iloc[0]['close'] * h['qty']
            for c, h in holdings.items()
            if not ohlcv_cache[c][ohlcv_cache[c]['date'] == day_ts].empty
        )
        equity_curve.append({
            'date': day_ts, 'cash': cash, 'holdings_value': holdings_value,
            'total_equity': cash + holdings_value, 'n_holdings': len(holdings),
        })

    # 强制平仓
    if holdings:
        final_day = all_trade_dates[-1]
        for code, h in list(holdings.items()):
            df = ohlcv_cache[code]
            last = df[df['date'] <= final_day]
            exit_price = float(last.iloc[-1]['close']) if not last.empty else h['entry_price']
            ret = exit_price / h['entry_price'] - 1
            cash += h['qty'] * exit_price
            trades.append({
                'code': code, 'entry_date': h['entry_date'],
                'entry_price': h['entry_price'], 'exit_date': final_day,
                'exit_price': exit_price, 'qty': h['qty'],
                'days_held': h['days_held'], 'ret_pct': ret * 100,
                'exit_type': 'force_close',
            })

    final_equity = cash
    return {
        'trades': trades,
        'equity_curve': equity_curve,
        'final_equity': final_equity,
        'start_capital': capital,
        'return_pct': (final_equity / capital - 1) * 100,
        'n_trades': len(trades),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital', type=float, default=1_000_000)
    parser.add_argument('--window-months', type=int, default=1)
    parser.add_argument('--step-months', type=int, default=1)
    parser.add_argument('--top-pct', type=float, default=0.05)
    parser.add_argument('--start', default='2025-01-01')
    parser.add_argument('--end', default='2026-09-30')
    args = parser.parse_args()

    print(f'[{datetime.now():%H:%M:%S}] 加载候选池...')
    cand = pd.read_csv(CANDIDATE_FP)
    cand['signal_date'] = pd.to_datetime(cand['signal_date'])
    print(f'  候选池: {len(cand):,} 条')
    print(f'  区间: {cand["signal_date"].min()} ~ {cand["signal_date"].max()}')

    # 滚动窗口
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    cur = start
    windows = []
    while cur < end:
        win_end = cur + pd.DateOffset(months=args.window_months)
        windows.append((cur, win_end))
        cur = cur + pd.DateOffset(months=args.step_months)
    print(f'  滚动窗口: {len(windows)} 个 ({args.window_months} 月/step {args.step_months})')

    # 预加载全期日 K
    all_codes = cand['code'].unique()
    print(f'\n[{datetime.now():%H:%M:%S}] 预加载 {len(all_codes)} 只股票日 K...')
    ohlcv_cache = {}
    for code in all_codes:
        ohlcv_cache[code] = load_ohlcv(code, start - timedelta(days=5), end + timedelta(days=45))

    # 滚动跑模拟
    print(f'\n[{datetime.now():%H:%M:%S}] 开始滚动模拟...')
    results = []
    for i, (win_start, win_end) in enumerate(windows):
        win_cand = cand[(cand['signal_date'] >= win_start) & (cand['signal_date'] < win_end)]
        if len(win_cand) < 5:
            continue
        print(f'\n  [{i+1}/{len(windows)}] {win_start.date()} ~ {win_end.date()}: {len(win_cand)} 候选')

        r = simulate_window(win_cand, ohlcv_cache, args.capital, args.top_pct)
        r['window_start'] = win_start
        r['window_end'] = win_end
        r['n_candidates'] = len(win_cand)
        results.append(r)
        print(f'    交易 {r["n_trades"]} 笔, 收益 {r["return_pct"]:+.2f}%')

    if not results:
        print('\n无结果')
        return

    # === 综合报告 ===
    summary_df = pd.DataFrame([
        {
            'window_start': r['window_start'].strftime('%Y-%m-%d'),
            'window_end': r['window_end'].strftime('%Y-%m-%d'),
            'n_candidates': r['n_candidates'],
            'n_trades': r['n_trades'],
            'start_capital': r['start_capital'],
            'final_equity': r['final_equity'],
            'return_pct': r['return_pct'],
            'win_rate': (sum(1 for t in r['trades'] if t['ret_pct'] > 0) / r['n_trades'] * 100)
                       if r['n_trades'] > 0 else 0,
        }
        for r in results
    ])

    # 累计收益（每窗口独立模拟，不连接）
    cumulative_return = ((1 + summary_df['return_pct'] / 100).prod() - 1) * 100
    avg_return = summary_df['return_pct'].mean()
    std_return = summary_df['return_pct'].std()
    win_rate_windows = (summary_df['return_pct'] > 0).mean() * 100

    # 拼接权益曲线
    all_equity = []
    for i, r in enumerate(results):
        for ec in r['equity_curve']:
            ec['window_idx'] = i
            all_equity.append(ec)
    equity_df = pd.DataFrame(all_equity)

    # === 输出 ===
    md = []
    md.append('# v3 提前版滚动模拟报告')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}  ')
    md.append(f'> 验证期: {start.date()} ~ {end.date()}  ')
    md.append(f'> 窗口: {args.window_months} 月 (step {args.step_months} 月)  ')
    md.append(f'> 共跑: {len(results)} 个窗口  ')
    md.append(f'> 候选池: Top {args.top_pct*100:.0f}%  ')
    md.append(f'> 起始资金/窗口: ¥{args.capital:,.0f}  ')
    md.append('')
    md.append('## 一、关键指标')
    md.append('')
    md.append('| 指标 | 数值 |')
    md.append('|---|---:|')
    md.append(f'| 窗口数 | {len(results)} |')
    md.append(f'| 单窗口平均收益 | {avg_return:+.2f}% |')
    md.append(f'| 单窗口收益波动（σ）| {std_return:.2f}% |')
    md.append(f'| 盈利窗口占比 | {win_rate_windows:.1f}% ({int((summary_df["return_pct"]>0).sum())}/{len(results)}) |')
    md.append(f'| 累计复利收益（独立窗口）| **{cumulative_return:+.2f}%** |')
    md.append(f'| 单窗口最大盈利 | {summary_df["return_pct"].max():+.2f}% |')
    md.append(f'| 单窗口最大亏损 | {summary_df["return_pct"].min():+.2f}% |')
    md.append('')
    md.append('## 二、各窗口明细')
    md.append('')
    md.append('| 窗口 | 候选 | 交易 | 收益 | 胜率 |')
    md.append('|:---:|---:|---:|---:|---:|')
    for _, r in summary_df.iterrows():
        md.append(f'| {r["window_start"]} ~ {r["window_end"]} | '
                  f'{int(r["n_candidates"])} | {int(r["n_trades"])} | '
                  f'{r["return_pct"]:+.2f}% | {r["win_rate"]:.1f}% |')
    md.append('')
    md.append('## 三、风险评估')
    md.append('')
    md.append(f'- **胜率**（窗口维度）: {win_rate_windows:.1f}%')
    md.append(f'- **收益分布**: 均值 {avg_return:+.2f}% / σ {std_return:.2f}%')
    md.append(f'- **最差窗口**: {summary_df["return_pct"].min():+.2f}%')
    md.append(f'- **最好窗口**: {summary_df["return_pct"].max():+.2f}%')
    if win_rate_windows >= 70 and cumulative_return > 0:
        md.append('- ✅ **整体稳健**')
        md.append('  → 多窗口一致性高，建议进入实盘跟踪')
    elif cumulative_return > 0:
        md.append('- 🟡 **部分稳健**')
        md.append('  → 累计正收益但个别窗口亏损，需进一步降低仓位')
    else:
        md.append('- 🔴 **策略需优化**')
    md.append('')
    md.append('## 四、局限')
    md.append('')
    md.append('1. 窗口独立模拟，未考虑跨窗口复利连接（可改为连续滚动）')
    md.append('2. 未计手续费、印花税、滑点（实盘预计损失 1-3%）')
    md.append('3. Top 5% 候选中包含 group=neg（实盘会买错）')
    md.append('4. 21 个月样本量较小，需更长周期验证')
    md.append('')
    md.append('---')
    md.append('')
    md.append(f'明细: `data/plan_e/v3_simulate_rolling_windows.csv`  ')
    md.append(f'权益: `data/plan_e/v3_simulate_rolling_equity.csv`  ')
    md.append(f'脚本: `scripts/v3_simulate_rolling.py`  ')

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_fp = OUT_DIR / 'v3_simulate_rolling_summary.md'
    report_fp.write_text('\n'.join(md), encoding='utf-8')

    summary_df.to_csv(OUT_DIR / 'v3_simulate_rolling_windows.csv', index=False)
    equity_df.to_csv(OUT_DIR / 'v3_simulate_rolling_equity.csv', index=False)

    print(f'\n报告: {report_fp}')
    print(f'明细: {OUT_DIR / "v3_simulate_rolling_windows.csv"}')
    print(f'权益: {OUT_DIR / "v3_simulate_rolling_equity.csv"}')
    print(f'\n=== 综合 ===')
    print(f'窗口数: {len(results)}')
    print(f'平均收益: {avg_return:+.2f}%')
    print(f'累计复利: {cumulative_return:+.2f}%')
    print(f'盈利窗口占比: {win_rate_windows:.1f}%')


if __name__ == '__main__':
    main()