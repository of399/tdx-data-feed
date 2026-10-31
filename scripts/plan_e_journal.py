#!/usr/bin/env python3
"""
方案 E 实盘跟踪系统 v1
=====================

【功能】
  1. 每日打印当日研究清单（读盘前 timer 落下的 plan_e_today.csv；该 timer 已于 2026-10-24 晚重开，见下）
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
  --mode daily           # 打印停用声明 + 当前持仓 + 今日研究清单（不产下单量、不改持仓）
  --mode weekly          # 周报生成
  --mode status          # 查看当前状态（持仓 + 累计 P&L）
  --mode buy --code 600519 --price 10.5  # 手动买入（实盘用）
  --mode sell --code 600519 --price 12.0 --reason TP  # 手动卖出
  --mode simulate         # 模拟模式（用历史数据回测 1 个月）
  --from-predict-list --mode simulate   # predict 榜单逐笔入模拟账（--mode real 已停用，见下）

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
  - 单股仓位：模拟口径默认 10% (10,000)，只作用于 --mode simulate 的虚拟账本
  - 最大持仓：10 只
  - 出场判定：同一根 K 线同时够到 TP/SL 时取保守侧（先判止损），跳空穿越价位按当日开盘成交
    ——与 scripts/plan_e_backtest.py 的 backtest_one 同一口径

【predict list 与实盘入口】
  daban(默认小仓位 Top5)按诚实成交口径回测为负期望(-0.78%/笔,累计 -94.5%,
  见 docs/DABAN_TOPK_PNL_20261022.md),所以 `--from-predict-list` 不再自动写入
  journal.csv;榜单本身照旧产出,模拟账 journal_sim.csv 仍可用于排序研究。

【方案 E 与实盘入口】
  2026-10-24 决定：方案 E 同样退出实盘入口，研究产出保留。诚实成交口径 + 修正统计推断后
  的 112 笔样本拒绝不了「无超额收益」(DSR/ Newey-West / bootstrap / 胜率对盈亏平衡四个检验
  全部 p≥0.05)，随机择时基线(标的池不动只重排入场日)也没测出择时信息——真实胜率 33.9% 反而
  低于随机入场日的 45.1%。加上样本产量约 16~17 笔/年，靠实盘积累永远补不够功效。`--mode daily`
  只出研究清单，不再逐笔算下单量；理由与数字见 PLAN_E_LIVE_RETIREMENT。
  调度：2026-10-24 晚 plan-e-today（工作日 08:45 盘前清单）与 plan-e-journal（18:10 归档）重新
  回到 `make timer-enable` 的启用列表——自动产出的仍然只是研究口径。plan-e-monitor /
  plan-e-weekly / plan-e-real-refresh 维持停用：它们的输入是那份空账本（无 positions.json，
  journal.csv 只有表头）。晚间那次依旧不自己跑 CLI 兜底（PLAN_E_PREMARKET_LIST_ACTIVE 的本名是
  「晚间兜底开关」，跟 timer 状态无关），读不到今日清单就如实说"今天没有盘前清单"，
  不报成"今日无触发信号"。要临时补一张清单，手动跑 plan_e_backtest.py --today。
"""

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent

# 仓库根目录加入 sys.path，必须在任何仓库内包的 import 之前：
# 直跑脚本时 sys.path[0] 是 scripts/，`quant` / `tdxfeed` 都不在其下；
# driver 落在仓库外时连 scripts/ 也要显式加，`_common` 才可导入。
# tdxfeed 未以可编辑方式安装；predict.py/plan_e_backtest.py 借 _common 间接引导，
# 本脚本的引导不依赖 _common（它在引导之后才导入），需显式。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _common import daily_path  # noqa: E402

from quant.execution.optimal_execution import (  # noqa: E402
    execution_table_markdown,
    suggest_execution,
)
from tdxfeed.execution.single import SingleTradePlan, check_exit  # noqa: E402

warnings.filterwarnings('ignore')

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

# 与回测同一口径：日内判定、双触发结算亏损侧、跳空按开盘
EXIT_PLAN = SingleTradePlan(judge="intrabar", tp=TP_PCT, sl=SL_PCT,
                            hold_days=HOLD_DAYS, sl_first=True)


def _bars_held(entry_day: pd.Timestamp, today: pd.Timestamp) -> int:
    """交易根数：用工作日近似交易日（journal 的日期循环本身就是 freq='B'）。"""
    return int(len(pd.bdate_range(entry_day, today)) - 1)


def _time_due(bars_held: int) -> bool:
    """内核口径：hold_days 含入场根，故入场后第 hold_days-1 根收盘即到期。"""
    return bars_held >= max(1, EXIT_PLAN.hold_days - 1)


def ensure_dirs():
    for d in [LIVE_DIR, SIGNALS_DIR, WEEKLY_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ============== 拆单建议（Almgren-Chriss，P2-5 接入） ==============

def _find_parquet(code: str):
    """定位日 K 文件（裸码按板块推断，同码双文件取个股而非指数）。"""
    fp = daily_path(code)
    return fp if fp.exists() else None


def get_execution_inputs(fp) -> tuple:
    """从日 K 取近 20 个交易日的 ADV（日均成交量）与日收益波动率。"""
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None, None
    if 'date' in df.columns:
        df = df.sort_values('date')
    tail = df.tail(20)
    if tail.empty or 'volume' not in tail.columns or 'close' not in tail.columns:
        return None, None
    adv = float(tail['volume'].mean())
    rets = tail['close'].pct_change().dropna()
    dvol = float(rets.std()) if len(rets) > 1 else None
    if not (adv > 0) or dvol is None or not np.isfinite(dvol):
        return None, None
    return adv, dvol


def get_last_close(fp) -> float:
    try:
        df = pd.read_parquet(fp)
        return float(df.sort_values('date')['close'].iloc[-1])
    except Exception:
        return 0.0


def format_execution_suggestion(code: str, side: str, qty: int, price: float,
                               adv: float, dvol: float, *, lam: float = 1.0,
                               n_buckets: int = 6) -> str:
    """生成一笔订单的 Almgren-Chriss 拆单建议文本（供 daily / weekly 嵌入）。"""
    side_cn = '买入' if side == 'buy' else '卖出'
    try:
        s = suggest_execution(qty, price, adv, dvol, T_days=1.0, lam=lam, n_buckets=n_buckets)
    except Exception as e:
        return f"  （{code} 拆单建议计算失败：{e}）"
    table = execution_table_markdown(s)
    return (
        f"\n  ### {side_cn} {code}  {qty:,.0f} 股 @ {price:.2f}（成交额 {s['notional']:,.0f}）\n"
        f"  ADV≈{adv:,.0f} 股，日波动 {s['sigma'] * 100:.1f}%，γ={s['gamma']:.2e}\n"
        f"  {table}\n"
        f"  > 最优每股成本 {s['per_share_cost']:.4f} vs 匀速 {s['uniform_per_share_cost']:.4f}"
        f"（风险调整后省 {s['cost_save_pct']:.1f}%）；执行风险降 {s['var_reduce_pct']:.1f}%\n"
        f"  > {s['recommendation']}"
    )


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
    """追加记录到 journal.csv（实盘日志），统一标记 source=real

    ⚠️ 注意:code 列在 CSV 中保留为字符串(如 '000001')。pd.read_csv 默认会推断为 int(丢前导 0),
    用户读取时需显式 dtype={'code': str}。这是 pandas 行为,不是本函数问题。
    """
    if not records:
        return
    df = pd.DataFrame(records)
    if 'source' not in df.columns:
        df['source'] = SOURCE_REAL
    if JOURNAL_FILE.exists():
        old = pd.read_csv(JOURNAL_FILE, dtype={'code': str})  # 保留前导 0
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(JOURNAL_FILE, index=False, encoding='utf-8-sig')


def append_sim_journal(records: list):
    """追加模拟交易记录到独立的 journal_sim.csv（与实盘 journal.csv 完全分离）

    ⚠️ 注意:code 列在 CSV 中保留为字符串(如 '000001')。pd.read_csv 默认会推断为 int(丢前导 0),
    用户读取时需显式 dtype={'code': str}。这是 pandas 行为,不是本函数问题。
    """
    if not records:
        return
    df = pd.DataFrame(records)
    df['source'] = SOURCE_SIM
    if SIM_JOURNAL_FILE.exists():
        old = pd.read_csv(SIM_JOURNAL_FILE, dtype={'code': str})  # 保留前导 0
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(SIM_JOURNAL_FILE, index=False, encoding='utf-8-sig')


# ============== 当日信号 ==============

# 晚间 CLI 兜底开关（与 plan-e-today.timer 是否启用无关，别顺手翻它）。
# 盘前那份清单在位时 journal 直接复用（走 _read_today_list，不看这个开关）；开关只管
# 「读不到清单时要不要自己跑 plan_e_backtest --today」。晚间跑的是当日收盘数据，算出来必然
# 是 entry_date=明天 的榜单，守卫一定丢弃——既白付一次全量回测，又把今日那份覆盖成明日榜。
PLAN_E_PREMARKET_LIST_ACTIVE = False


def _read_today_list(today: str) -> pd.DataFrame | None:
    """读盘前清单；不是今天写的、或 entry_date 不是今天的，一律返回 None。"""
    fp = LIVE_DIR.parent / 'plan_e_today.csv'
    if not fp.exists():
        return None
    if datetime.fromtimestamp(fp.stat().st_mtime).strftime('%Y-%m-%d') != today:
        return None
    sig = pd.read_csv(fp, dtype={'code': str})
    if sig.empty:
        return sig
    entry = (sig['entry_date'].astype(str).str.slice(0, 10)
             if 'entry_date' in sig.columns else pd.Series(dtype=str))
    if entry.empty or not (entry == today).all():
        print(f"⚠️  清单 entry_date 不是 {today}（实际 {sorted(entry.unique())}），丢弃")
        return None
    return sig


def get_daily_signals(today: str | None = None) -> pd.DataFrame:
    """取「今日开盘入场」清单：只认盘前 timer 写好的那份。

    盘前 plan-e-today.service 用截至昨日的行情算出 entry_date=今天 的榜单；晚间再跑一次
    CLI 用的是今日收盘后的数据，算出来的是明天的入场清单，会把今日那份覆盖掉。
    两道闸：清单必须是今天写的且 entry_date 就是今天；子进程非零退出时不读磁盘残留。
    晚间兜底关着（PLAN_E_PREMARKET_LIST_ACTIVE=False）时读不到就是没有，并且要如实说
    "今天没有盘前清单"，不能报成"今日无触发信号"。
    """
    import subprocess
    today = today or datetime.now().strftime('%Y-%m-%d')
    sig = _read_today_list(today)
    if sig is None:
        if not PLAN_E_PREMARKET_LIST_ACTIVE:
            print("\n  今天没有盘前清单（08:45 的 plan-e-today 没跑成，或被数据新鲜度守卫拦下）："
                  "晚间不跑 CLI 兜底——那用的是当日收盘数据，算出来是明天的入场榜。")
            return pd.DataFrame()
        result = subprocess.run(
            ['venv/bin/python', 'scripts/plan_e_backtest.py', '--today'],
            cwd=ROOT, capture_output=True, text=True, timeout=300
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"⚠️  plan_e_backtest --today 退出码 {result.returncode}，"
                  f"不读清单：{(result.stderr or '').strip()[:200]}")
            return pd.DataFrame()
        sig = _read_today_list(today)
        if sig is None or sig.empty:
            return pd.DataFrame()
    elif sig.empty:
        print("\n  今日无触发信号")
        return pd.DataFrame()
    archive = SIGNALS_DIR / f'signals_{today.replace("-", "")}.csv'
    sig.to_csv(archive, index=False, encoding='utf-8-sig')
    return sig


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

    dates = pd.date_range(start=start_date, end=end_date, freq='B')  # 工作日
    journal_records = []

    for trade_date in dates:
        date_str = trade_date.strftime('%Y-%m-%d')
        print(f"\n[{date_str}] ", end='')

        # Step 1: 检查现有持仓是否触发 TP/SL/TIME
        closed = []
        for code, p in list(pos['positions'].items()):
            fp = daily_path(code)
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
                df['date'] = pd.to_datetime(df['date'])
                today_data = df[df['date'] == trade_date]
                if today_data.empty:
                    continue
                t = today_data.iloc[0]
                entry = p['entry_price']
                bars_held = _bars_held(pd.to_datetime(p['entry_date']), trade_date)
                hit = check_exit(entry, open_=float(t['open']), high=float(t['high']),
                                 low=float(t['low']), close=float(t['close']), plan=EXIT_PLAN)
                reason, exit_price = None, None
                if hit is not None:
                    reason = hit.reason          # 内核返回 "TP"/"SL"，与 journal 既有取值一致
                    exit_price = hit.price
                elif _time_due(bars_held):
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
                        'hold_days': bars_held,
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
                    fp = daily_path(code)
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
            fp = daily_path(code)
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
                    'hold_days': _bars_held(pd.to_datetime(p['entry_date']), dates[-1]),
                    'entry_date': p['entry_date'],
                    'signal_date': p['signal_date'],
                })
            except Exception as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
                print(f"[WARN] swallowed: {e!r}")

    # 保存（模拟交易写入独立的 journal_sim.csv，与实盘 journal.csv 完全分离）
    ensure_dirs()
    append_sim_journal(journal_records)

    # 汇总
    df_j = pd.DataFrame(journal_records)
    print("\n=== 模拟交易汇总 ===")
    print(f"  初始资金: {capital:,.0f}")
    print(f"  期末资金: {pos['cash']:,.0f}")
    print(f"  总盈亏: {pos['cash'] - capital:+,.0f} ({(pos['cash']/capital-1)*100:+.2f}%)")
    print(f"  总交易笔数: {len(df_j[df_j['action']=='BUY'])}")
    sells = df_j[df_j['action']=='SELL']
    print(f"  胜率: {(sells['pnl_pct']>0).mean()*100:.1f}%")
    print(f"  平均收益: {sells['pnl_pct'].mean()*100:+.2f}%")
    print(f"  交易明细 → {SIM_JOURNAL_FILE}（与实盘 journal.csv 分离）")

    return df_j


# ============== 实盘每日模式 ==============

# 2026-10-24 决定：方案 E 退出实盘入口，研究产出保留（处理方式同 daban 的 DABAN_LIVE_BLOCK）。
PLAN_E_LIVE_RETIREMENT = (
    "⚠️ 方案 E 已退出实盘入口（2026-10-24 决定）。下面的清单是研究产出，不作建仓依据。\n"
    "\n"
    "   成交口径与统计推断都改保守之后（data/plan_e/plan_e_backtest_v2_latest.md，112 笔）：\n"
    "     · Deflated Sharpe p=1.000 / Newey-West 日收益 p=0.748 / bootstrap 均值 p=0.561，\n"
    "       三个检验都拒绝不了「无超额收益」；\n"
    "     · 胜率 33.9% 对赔率隐含盈亏平衡 31.2% 的单侧 p=0.296。原先那行「对随机 50% 基准\n"
    "       p=0.001」测的是 TP+30%/SL-10 的赔付阶梯，不是本事，已改掉；\n"
    "     · 均收益 +1.16%/笔的 95% CI 跨零（[-2.6%, +5.1%]），年化 0.50% 对 -23.63% 最大回撤，\n"
    "       Calmar 0.02；\n"
    "     · 随机择时基线（标的池不动，只把每笔入场日换成该股票随机的一天，出场阶梯不变）：\n"
    "       真实均收益 +1.16% 对随机 +0.39%（p=0.215），胜率 33.9% 反而低于随机的 45.1%\n"
    "       （p=0.994）——「买在哪天」没有信息。这条只判择时、不判选股，且因为随机化只动\n"
    "       入场日，它是条件推断，不能外推到没见过的票。\n"
    "\n"
    "   这条路也没法靠实盘积累来验证：样本产量约 16~17 笔/年，按当前效应量要 ~1,240 笔才摸到\n"
    "   p<0.05（80% 功效需 ~2,000 笔），即 70~120 年；约 94% 的清晨盘前清单本就是空表。\n"
    "\n"
    "   研究用途照旧：--mode simulate 写 journal_sim.csv，plan_e_backtest.py 随时可跑。\n"
    "   真要建仓：人工 --mode buy --code ... --price ... --qty ... —— 这个决定本脚本不代做。"
)


def mode_daily(args):
    """每日模式：输出今日研究清单 + 当前持仓（不再给下单建议）。"""
    ensure_dirs()
    pos = load_positions()

    today = datetime.now().strftime('%Y-%m-%d')
    print(f"=== 方案 E 每日模式 {today} ===")
    print(PLAN_E_LIVE_RETIREMENT)
    print(f"\n  当前持仓: {len(pos['positions'])} 只")
    print(f"  可用现金: {pos['cash']:,.0f}")

    # 取盘前那份清单；"没有清单"和"扫过无触发"两类否定话术由 get_daily_signals 自己分辨着说
    sig = get_daily_signals(today)
    if not sig.empty:
        print(f"\n  今日触发信号（研究口径，不含入场建议）: {len(sig)} 只")
        print(sig[['code', 'signal_date', 't1_pct', 't1_vol_ratio']].to_string(index=False))


def mode_status():
    """查看当前状态"""
    pos = load_positions()
    print("=== 方案 E 状态 ===")
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


# ============== 方案 B 2026-10-16：--from-predict-list ==============

# 2026-10-24 决定：predict list 退出实盘入口，研究产出保留。
DABAN_LIVE_BLOCK = (
    "❌ --from-predict-list --mode real 已停用。\n"
    "\n"
    "   predict.py 的默认出票模型是 daban(小仓位 Top5,次日收盘涨停分类),这张榜按诚实\n"
    "   成交口径的两个独立回测都没有正期望:\n"
    "     · docs/DABAN_TOPK_PNL_20261022.md —— K=5 成交 1509 笔,平均净收益 -0.78%/笔,\n"
    "       累计 -94.5%;未计成本毛收益 -0.58%,负期望来自 T+1 开盘买入的入场时点,\n"
    "       不是交易成本。\n"
    "     · data/daban/execution_backtest_20261022.json 的 gate —— baseline/top1/top2/\n"
    "       top3/top5/stage1_threshold 六条 primary 规则全部未达标,overall_go=false,\n"
    "       \"Stage 1 未通过,不进入前向纸盘/小资金\"。\n"
    "\n"
    "   研究用途照旧:--mode simulate 逐笔写入 journal_sim.csv,夜间 predict 产物不受影响。\n"
    "   真要建仓:人工 --mode buy --code ... --price ... --qty ... —— 这个决定本脚本不代做。"
)


def load_latest_predict_list(top_n: int = 50, threshold: float = 0.7,
                             score_col: str = 'prob', price_col: str = 'close'):
    """加载 data/predictions/predict_*.csv 中 mtime 最新的文件,按 score ≥ threshold 过滤,
    取 Top N。返回 pd.DataFrame[code, price, score]。

    ⚠️ 注意:阈值逻辑与 predict.py 一致——回归模型(prob 列实为预期 PnL)按 > 0;分类模型按 ≥ threshold。
    本函数对两种都按 score >= threshold(若 threshold<0,等价于回归逻辑)。
    """
    pred_dir = ROOT / 'data' / 'predictions'
    if not pred_dir.exists():
        sys.exit(f"❌ --from-predict-list: {pred_dir} 不存在")
    # mtime 倒序取第 1 个:glob 拿所有 predict_*.csv(含 _top*_thr* 子集)
    candidates = sorted(pred_dir.glob('predict_*.csv'),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        sys.exit(f"❌ --from-predict-list: {pred_dir} 无 predict_*.csv")
    latest = candidates[0]
    print(f"  消费最新 predict 文件: {latest.name} (mtime={latest.stat().st_mtime:.0f})")
    try:
        df = pd.read_csv(latest)
    except Exception as e:
        sys.exit(f"❌ --from-predict-list 读取失败: {latest}\n  错误: {e}")
    if score_col not in df.columns:
        sys.exit(f"❌ --from-predict-list: {latest.name} 缺列 '{score_col}'")
    if price_col not in df.columns:
        sys.exit(f"❌ --from-predict-list: {latest.name} 缺列 '{price_col}'")
    if 'code' not in df.columns:
        sys.exit(f"❌ --from-predict-list: {latest.name} 缺列 'code'")
    # 过滤 + top_n
    df = df[df[score_col] >= threshold].copy()
    df = df.sort_values(score_col, ascending=False).head(top_n)
    df = df[['code', price_col, score_col]].rename(columns={price_col: 'price', score_col: 'score'})
    # 关键:code 显式转 str(6位 zfill),避免 pandas iloc[0] 把 int64 转 float64 → '600519.0'
    df['code'] = df['code'].astype(str).str.zfill(6)
    print(f"  过滤: threshold={threshold} top_n={top_n} → {len(df)} 只入选")
    if df.empty:
        print("  (空集,正常退出,不写 journal)")
        return df
    return df


def mode_from_predict_list(mode: str, top_n: int = 50, threshold: float = 0.7):
    """消费最新 predict list → 模拟逐笔买入 → 写入 journal.csv (real) / journal_sim.csv (sim)。

    资金分配:等额(INITIAL_CAPITAL / top_n, 100 元一手,按 100 股整数取整)。
    ⚠️ 重要:--mode real 是 **paper-trading**(模拟真金白银流),不是真交易。
    """
    if mode == 'real':
        sys.exit(DABAN_LIVE_BLOCK)
    if mode == 'simulate':
        print("  ⚠ 该榜默认来自 daban(小仓位 Top5),诚实成交口径回测为负期望,只作排序研究。")
    ensure_dirs()
    picks = load_latest_predict_list(top_n=top_n, threshold=threshold)
    if picks.empty:
        return
    # 等额资金分配
    cash_per_pick = INITIAL_CAPITAL / max(len(picks), 1)
    n_buys = 0
    for _, row in picks.iterrows():
        code = str(row['code']).zfill(6)
        price = float(row['price'])
        score = float(row['score'])
        if price <= 0:
            print(f"  ⚠ {code} price={price} 无效,跳过")
            continue
        # 100 股一手
        qty = max(int(cash_per_pick / price / 100) * 100, 100)
        if mode == 'simulate':
            # 走模拟:直接写 journal_sim.csv(不经 positions)
            append_sim_journal([{
                'date': datetime.now().strftime('%Y-%m-%d'),
                'code': code,  # 已是 str(6位 zfill),pandas 写入保留字符串
                'action': 'BUY',
                'qty': qty,
                'price': price,
                'amount': qty * price,
                'pnl': 0,
                'pnl_pct': 0,
                'reason': 'PREDICT_LIST',
                'hold_days': 0,
                'entry_date': datetime.now().strftime('%Y-%m-%d'),
                'signal_date': datetime.now().strftime('%Y-%m-%d'),
                'source': SOURCE_SIM,
                'score': score,
            }])
            n_buys += 1
        else:
            # mode == 'real' 走实盘 paper-trading:用 mode_buy(管理 positions + 写 journal.csv)
            try:
                mode_buy(code, price, qty)
                n_buys += 1
            except SystemExit as e:
                print(f"  ⚠ {code} 买入失败: {e}")
                continue
    print(f"\n✓ 完成: {n_buys}/{len(picks)} 只成交,模式={mode}")


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


def mode_weekly(args):
    """生成周报（含成交拆单建议章节）"""
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
"""

    # 拆单建议章节：本周每笔买入订单的 Almgren-Chriss 日内拆单轨迹
    sched_parts = []
    for _, row in buys.iterrows():
        code = str(row['code']).zfill(6)
        fpath = _find_parquet(code)
        if fpath is None:
            continue
        adv, dvol = get_execution_inputs(fpath)
        if adv is None or dvol is None:
            continue
        price = float(row['price'])
        qty = int(row['qty'])
        if qty <= 0:
            continue
        sched_parts.append(format_execution_suggestion(code, 'buy', qty, price, adv, dvol,
                                                      lam=args.lam, n_buckets=args.exec_buckets))
    if sched_parts:
        md += "\n## 成交拆单建议（Almgren-Chriss 最优执行轨迹）\n\n"
        md += "本周买入订单的建议日内拆单轨迹（前 1/3 段占比高=前置成交，低=匀速 TWAP）：\n\n"
        md += "\n\n".join(sched_parts)
        md += "\n"

    md += f"""
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
    parser.add_argument('--lam', type=float, default=0.05,
                        help='Almgren-Chriss 风险厌恶 λ（拆单建议用，默认 0.05，越大越前置）')
    parser.add_argument('--exec-buckets', type=int, default=6,
                        help='拆单段数（默认 6 段，日内对应 ~40 分钟/段）')
    parser.add_argument('--no-freshness-check', action='store_true', help='跳过数据新鲜度校验')
    # 方案 B 2026-10-16：--from-predict-list 信号→journal 自动消费
    parser.add_argument('--from-predict-list', action='store_true',
                        help='从 data/predictions/predict_*.csv 取最新文件作为买入信号源'
                             '(默认 Top 50 + 阈值 0.7 过滤)。⚠ 仅 --mode simulate 可用:'
                             '默认榜源 daban 回测为负期望,--mode real 已停用')
    args = parser.parse_args()

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    # 方案 B 2026-10-16：--from-predict-list 信号→journal 路由(优先级最高,不走 daily/buy/etc)
    if args.from_predict_list:
        if args.mode not in ('real', 'simulate'):
            sys.exit(f"❌ --from-predict-list 需要 --mode real 或 simulate,当前: {args.mode}")
        print("=== 方案 B: predict list → journal 自动消费 ===")
        mode_from_predict_list(args.mode)
        return

    if args.mode == 'daily':
        mode_daily(args)
    elif args.mode == 'weekly':
        mode_weekly(args)
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
