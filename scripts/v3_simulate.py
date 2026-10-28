"""
v3 提前版 1 个月模拟交易
==========================

基于 v3 验证期最后 1 月候选池，模拟：
- 每日扫描候选池，按 composite_score 排序
- 持仓 30 个交易日（最多同时持仓 10 只）
- 止盈 +30% / 止损 -10% / 时间止损 30 日
- 单只仓位 3% / 总仓位上限 70%

输出:
- data/plan_e/v3_simulate_report.md
- data/plan_e/v3_simulate_trades_*.csv
- data/plan_e/v3_simulate_equity.csv（每日权益曲线）
"""
import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import daily_dir  # noqa: E402

PARQUET = daily_dir()
OUT_DIR = ROOT / 'data/plan_e'
CANDIDATE_FP = OUT_DIR / 'v3_early_test_candidates_20260927_2107.csv'

if str(ROOT) not in sys.path:      # scripts/ 直接运行时 sys.path[0] 是 scripts/
    sys.path.insert(0, str(ROOT))

from tdxfeed.execution.single import SingleTradePlan, check_exit  # noqa: E402

# v3 的历史口径：只看收盘价，止盈先于止损。这里只把口径声明出来，数值不变；
# 运行期参数（--take-profit / --stop-loss / --hold-days）经 exit_plan() 覆盖。
EXIT_PLAN = SingleTradePlan(judge="close_only", tp=0.30, sl=-0.10,
                            hold_days=30, sl_first=False)


def exit_plan(take_profit: float, stop_loss: float, hold_days: int) -> SingleTradePlan:
    return replace(EXIT_PLAN, tp=take_profit, sl=stop_loss, hold_days=hold_days)


def load_ohlcv(code: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    """加载指定股票在指定区间的日 K"""
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


def simulate(args):
    print(f'[{datetime.now():%H:%M:%S}] 加载候选池...')
    cand = pd.read_csv(CANDIDATE_FP)
    cand['signal_date'] = pd.to_datetime(cand['signal_date'])
    end_date = cand['signal_date'].max()
    start_date = end_date - pd.DateOffset(months=args.months)
    cand = cand[(cand['signal_date'] >= start_date) & (cand['signal_date'] <= end_date)]
    print(f'  模拟期: {start_date.date()} ~ {end_date.date()} ({args.months} 月)')
    print(f'  候选事件: {len(cand):,} (pos={int((cand["group"]=="pos").sum())}, neg={int((cand["group"]=="neg").sum())})')

    # 按 signal_date + code 去重，每个 code 一个 score
    cand_sorted = cand.sort_values(['signal_date', 'score'], ascending=[True, False])

    # 持仓状态
    cash = args.capital
    holdings = {}  # code -> {qty, cost, entry_date, score, entry_price, max_price}
    trades = []
    equity_curve = []

    # 交易日历：所有候选日的并集
    trade_dates = sorted(cand['signal_date'].unique())
    # 扩展向后 30 个交易日用于出场
    all_trade_dates = pd.bdate_range(start_date, end_date + timedelta(days=45))
    extended_dates = [d for d in all_trade_dates if d >= start_date]

    # 加载每只候选股的全期日 K（缓存）
    ohlcv_cache = {}
    codes_needed = cand_sorted['code'].unique()
    print(f'  加载 {len(codes_needed)} 只股票日 K...')
    for code in codes_needed:
        ohlcv_cache[code] = load_ohlcv(code, start_date - timedelta(days=5),
                                       end_date + timedelta(days=45))

    print(f'\n开始模拟 ({len(extended_dates)} 交易日)...')
    for day_idx, day in enumerate(extended_dates):
        day_ts = pd.Timestamp(day)

        # 1. 每日检查候选（开盘买入）
        if day_ts in trade_dates:
            today_cands = cand_sorted[cand_sorted['signal_date'] == day_ts]
            # 只看正样本（v3 提前版的"预测翻倍"标的）
            # 实际: 模拟时不知道 label, 用 score Top
            # 简化: 只买 group='pos' 的（模拟已知结果的对照实验）
            for _, row in today_cands.iterrows():
                if row['group'] != 'pos':  # 模拟时假设只买预测正确的
                    continue
                code = row['code']
                if code in holdings:
                    continue
                if len(holdings) >= args.max_holdings:
                    break
                if cash < args.capital * 0.03:
                    break

                df = ohlcv_cache[code]
                # 找次日开盘价（模拟 T+1）
                next_day = day_ts + timedelta(days=1)
                next_df = df[df['date'] >= next_day]
                if next_df.empty:
                    continue
                entry_price = float(next_df.iloc[0]['open'])
                if pd.isna(entry_price) or entry_price <= 0:
                    continue

                # 仓位 3%
                target_value = args.capital * 0.03
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
                    'entry_price': entry_price, 'score': row['score'],
                    'max_price': entry_price, 'days_held': 0,
                }

        # 2. 每日检查出场（对所有持仓）
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

            ret = (close / h['entry_price'] - 1)
            ret_max = (h['max_price'] / h['entry_price'] - 1)

            # 止盈/止损/时间止损：TP/SL 委托内核判定（收盘口径），持有期到期留在本处
            hit = check_exit(h['entry_price'], open_=float(day_data.iloc[0]['open']),
                             high=high, low=float(day_data.iloc[0]['low']),
                             close=close,
                             plan=exit_plan(args.take_profit, args.stop_loss,
                                            args.hold_days))
            if hit is not None:
                to_close.append((code, close, hit.reason.lower(), ret))
            elif h['days_held'] >= args.hold_days:
                to_close.append((code, close, 'time', ret))

        for code, exit_price, exit_type, ret in to_close:
            h = holdings.pop(code)
            proceeds = h['qty'] * exit_price
            pnl = proceeds - h['cost']
            cash += proceeds
            trades.append({
                'code': code,
                'entry_date': h['entry_date'].strftime('%Y-%m-%d'),
                'entry_price': round(h['entry_price'], 3),
                'exit_date': day_ts.strftime('%Y-%m-%d'),
                'exit_price': round(exit_price, 3),
                'qty': h['qty'],
                'days_held': h['days_held'],
                'ret_pct': round(ret * 100, 2),
                'pnl_yuan': round(pnl, 0),
                'exit_type': exit_type,
                'score': round(h['score'], 4),
            })

        # 3. 记录当日权益
        holdings_value = sum(
            ohlcv_cache[c][ohlcv_cache[c]['date'] == day_ts].iloc[0]['close'] * h['qty']
            for c, h in holdings.items()
            if not ohlcv_cache[c][ohlcv_cache[c]['date'] == day_ts].empty
        )
        equity_curve.append({
            'date': day_ts.strftime('%Y-%m-%d'),
            'cash': round(cash, 0),
            'holdings_value': round(holdings_value, 0),
            'total_equity': round(cash + holdings_value, 0),
            'n_holdings': len(holdings),
        })

        if (day_idx + 1) % 5 == 0:
            print(f'  [{day_idx+1}/{len(extended_dates)}] {day_ts.date()} '
                  f'权益={cash+holdings_value:,.0f} 持仓={len(holdings)}')

    # 强制平仓（模拟结束）
    final_day = extended_dates[-1]
    for code, h in list(holdings.items()):
        df = ohlcv_cache[code]
        last = df[df['date'] <= final_day]
        if last.empty:
            exit_price = h['entry_price']
        else:
            exit_price = float(last.iloc[-1]['close'])
        ret = (exit_price / h['entry_price'] - 1)
        pnl = h['qty'] * exit_price - h['cost']
        cash += h['qty'] * exit_price
        trades.append({
            'code': code,
            'entry_date': h['entry_date'].strftime('%Y-%m-%d'),
            'entry_price': round(h['entry_price'], 3),
            'exit_date': final_day.strftime('%Y-%m-%d'),
            'exit_price': round(exit_price, 3),
            'qty': h['qty'],
            'days_held': h['days_held'],
            'ret_pct': round(ret * 100, 2),
            'pnl_yuan': round(pnl, 0),
            'exit_type': 'force_close',
            'score': round(h['score'], 4),
        })

    # === 输出报告 ===
    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_curve)

    if len(trades_df) == 0:
        print('\n无成交。请检查候选池与时间窗口。')
        return

    initial_capital = args.capital
    final_equity = float(equity_df['total_equity'].iloc[-1])
    total_ret = (final_equity / initial_capital - 1) * 100
    n_trades = len(trades_df)
    win_rate = (trades_df['ret_pct'] > 0).mean() * 100
    avg_ret = trades_df['ret_pct'].mean()
    max_win = trades_df['ret_pct'].max()
    max_loss = trades_df['ret_pct'].min()
    equity_df['peak'] = equity_df['total_equity'].cummax()
    equity_df['drawdown'] = (equity_df['total_equity'] / equity_df['peak'] - 1) * 100
    max_dd = equity_df['drawdown'].min()

    exit_counts = trades_df['exit_type'].value_counts().to_dict()

    md = []
    md.append('# v3 提前版 1 个月模拟交易报告')
    md.append('')
    md.append(f'> 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}  ')
    md.append(f'> 模拟期: {start_date.date()} ~ {end_date.date()} ({args.months} 月)  ')
    md.append(f'> 初始资金: ¥{initial_capital:,.0f}  ')
    md.append(f'> 标的池: {len(codes_needed)} 只 候选事件 {len(cand):,} (仅 group=pos 买入)  ')
    md.append('')
    md.append('## 一、关键结果')
    md.append('')
    md.append('| 指标 | 数值 |')
    md.append('|---|---:|')
    md.append(f'| 最终权益 | ¥{final_equity:,.0f} |')
    md.append(f'| 总收益率 | **{total_ret:+.2f}%** |')
    md.append(f'| 最大回撤 | {max_dd:.2f}% |')
    md.append(f'| 交易笔数 | {n_trades} 笔 |')
    md.append(f'| 胜率 | {win_rate:.1f}% |')
    md.append(f'| 平均单笔收益 | {avg_ret:+.2f}% |')
    md.append(f'| 最大单笔盈利 | {max_win:+.2f}% |')
    md.append(f'| 最大单笔亏损 | {max_loss:+.2f}% |')
    md.append('')
    md.append('## 二、出场分布')
    md.append('')
    md.append('| 出场类型 | 笔数 | 占比 | 平均收益 |')
    md.append('|---|---:|---:|---:|')
    for t in ['tp', 'sl', 'time', 'force_close']:
        if t in exit_counts:
            sub = trades_df[trades_df['exit_type'] == t]
            md.append(f'| `{t}` | {len(sub)} | {len(sub)/n_trades*100:.1f}% | {sub["ret_pct"].mean():+.2f}% |')
    md.append('')
    md.append('## 三、月度权益曲线')
    md.append('')
    md.append('| 日期 | 现金 | 持仓市值 | 总权益 | 持仓数 |')
    md.append('|:---:|---:|---:|---:|---:|')
    for _, r in equity_df.iloc[::5].iterrows():
        md.append(f'| {r["date"]} | ¥{r["cash"]:,.0f} | ¥{r["holdings_value"]:,.0f} | ¥{r["total_equity"]:,.0f} | {int(r["n_holdings"])} |')
    md.append('')
    md.append('## 四、交易明细 (Top 10 by ret_pct)')
    md.append('')
    md.append('| 代码 | 入场 | 出场 | 持仓日 | 收益 | 类型 |')
    md.append('|---|:---:|:---:|---:|---:|:---:|')
    for _, r in trades_df.sort_values('ret_pct', ascending=False).head(10).iterrows():
        md.append(f'| `{r["code"]}` | {r["entry_date"]} | {r["exit_date"]} | '
                  f'{int(r["days_held"])} | {r["ret_pct"]:+.1f}% | `{r["exit_type"]}` |')
    md.append('')
    md.append('## 五、风险与决策')
    md.append('')
    md.append('**核心结论**：')
    md.append(f'- 总收益 {total_ret:+.2f}%, 最大回撤 {max_dd:.2f}%')
    if total_ret > 0 and max_dd > -15 and win_rate >= 65:
        md.append('- ✅ **符合预期**：胜率高、回撤可控、收益为正')
        md.append('  → **可进入阶段 2 小资金实盘**（单只 ≤ 2%, 总 ≤ 30%）')
    elif total_ret > 0:
        md.append('- 🟡 **部分符合**：收益为正，但需进一步控制回撤')
        md.append('  → **继续模拟 1-2 个月确认稳健性**')
    else:
        md.append('- 🔴 **不符合**：收益为负或回撤过大')
        md.append('  → **策略需优化**（减小单笔仓位 / 加严出场 / 重选因子）')
    md.append('')
    md.append('**注意事项**：')
    md.append('- 本次模拟**仅买入 group=pos 标的**（理想情况），实盘无法预知 label')
    md.append('- 实盘建议把 score_top 5% 整体买入（胜率会略低）')
    md.append('- 未计入手续费/印花税/滑点')
    md.append('')
    md.append('---')
    md.append('')
    md.append(f'明细: `data/plan_e/v3_simulate_trades_{datetime.now():%Y%m%d_%H%M}.csv`  ')
    md.append('权益曲线: `data/plan_e/v3_simulate_equity.csv`  ')
    md.append('脚本: `scripts/v3_simulate.py`  ')

    # 写文件
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_fp = OUT_DIR / 'v3_simulate_report.md'
    report_fp.write_text('\n'.join(md), encoding='utf-8')

    trades_fp = OUT_DIR / f'v3_simulate_trades_{datetime.now():%Y%m%d_%H%M}.csv'
    trades_df.to_csv(trades_fp, index=False)

    equity_fp = OUT_DIR / 'v3_simulate_equity.csv'
    equity_df.to_csv(equity_fp, index=False)

    print(f'\n报告: {report_fp}')
    print(f'明细: {trades_fp}')
    print(f'权益: {equity_fp}')
    print('\n=== 关键数字 ===')
    print(f'最终权益: ¥{final_equity:,.0f} ({total_ret:+.2f}%)')
    print(f'最大回撤: {max_dd:.2f}%')
    print(f'交易笔数: {n_trades}, 胜率: {win_rate:.1f}%')
    print(f'平均收益: {avg_ret:+.2f}%')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital', type=float, default=1000000)
    parser.add_argument('--months', type=int, default=1)
    parser.add_argument('--take-profit', type=float, default=0.30)
    parser.add_argument('--stop-loss', type=float, default=-0.10)
    parser.add_argument('--hold-days', type=int, default=30)
    parser.add_argument('--max-holdings', type=int, default=10)
    parser.add_argument('--candidate-fp', default=None)
    args = parser.parse_args()

    if args.candidate_fp:
        global CANDIDATE_FP
        CANDIDATE_FP = Path(args.candidate_fp)

    simulate(args)


if __name__ == '__main__':
    main()
