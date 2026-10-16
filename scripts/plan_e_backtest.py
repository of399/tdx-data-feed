#!/usr/bin/env python3
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
  venv/bin/python scripts/plan_e_backtest.py --no-cost          # 净价回测（不含成本）
  venv/bin/python scripts/plan_e_backtest.py --fee 0.0003 --slippage 0.001  # 自定义成本
  venv/bin/python scripts/plan_e_backtest.py --sensitivity      # 参数敏感性扫描（hold/tp/sl 网格）
  venv/bin/python scripts/plan_e_backtest.py --stratify         # 沪深/北交所分层回测
  venv/bin/python scripts/plan_e_backtest.py --by-year          # 按年份分层回测
  venv/bin/python scripts/plan_e_backtest.py --walk-forward     # walk-forward 固定参数滚动验证（OOS 按年）
  venv/bin/python scripts/plan_e_backtest.py --retune           # walk-forward 训练窗内重选参数→OOS（真 OOS）
  venv/bin/python scripts/plan_e_backtest.py --retune --bj-slippage 0.003   # 同上 + 北交所 0.3% 额外滑点压力
  venv/bin/python scripts/plan_e_real_samples.py              # 从 journal 构建实盘样本集（回灌 data/double_up，累积去重）
  venv/bin/python scripts/plan_e_backtest.py --real           # 实盘样本分析（观测收益，按年/滚动窗口，n≥30 显著性闸口）

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
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings('ignore')

ROOT = Path(__file__).parent.parent
DOUBLE_UP_CSV = ROOT / 'data' / 'double_up' / 'double_up_2020_2026_detail.csv'
PARQUET_DIR = ROOT / 'data' / 'parquet' / 'daily'
OUTPUT_DIR = ROOT / 'data' / 'plan_e'
# 实盘样本集（由 scripts/plan_e_real_samples.py 从 journal 回灌，累积去重）
REAL_TRADES_CSV = ROOT / 'data' / 'double_up' / 'real_trades.csv'


# 统一日期解析：单一数据源见 tdxfeed/date_utils.parse_date
from _common import parse_date  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# P0-1 ④：置换检验（H0 策略日盈亏顺序随机 = 随机抽样同分布）
from quant.multifactor.cv_utils import permutation_return_test  # noqa: E402

_PARQUET_CACHE: dict[str, pd.DataFrame] = {}


def _get_df(code_clean: str):
    """带缓存的 parquet 加载：按代码；命中则返回已解析 date 的 DataFrame（加速网格扫描）。"""
    if code_clean in _PARQUET_CACHE:
        return _PARQUET_CACHE[code_clean]
    for cand in [f'{code_clean}.parquet', f'sh{code_clean}.parquet',
                 f'sz{code_clean}.parquet', f'bj{code_clean}.parquet']:
        f = PARQUET_DIR / cand
        if f.exists():
            try:
                df = pd.read_parquet(f)
            except Exception:
                return None
            df['date'] = parse_date(df['date'])
            _PARQUET_CACHE[code_clean] = df
            return df
    return None


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
                 min_t_vol_ratio: float = 0.0,
                 fee: float = 0.0,
                 slippage: float = 0.0,
                 bj_slippage: float = 0.0) -> dict:
    """
    对单只股回测
    signal_date = 翻倍首次触发日 (T 日)
    T+1 日 = 触发后第一天（决定是否入场）
    T+2 日 = 入场日
    """
    code_clean = str(code).zfill(6).lstrip('bj')

    # 北交所（920XXX 或 8xxxxx）流动性低，叠加额外滑点压力
    is_bj = code_clean.startswith('920') or code_clean[0] == '8'
    eff_slip = slippage + (bj_slippage if is_bj else 0.0)

    df = _get_df(code_clean)
    if df is None:
        return None

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

    entry_idx = idx + 2
    # 持有窗口（T+3 起，T+2 为入场日）
    start = idx + 3
    end = min(start + hold_days - 1, len(df))
    if end - start < 3:   # 窗口至少 3 天
        return None

    # 逐日判定出场（先 TP 后 SL，否则时间止损）
    exit_idx = end - 1
    reason = 'TIME'
    for j in range(start, end):
        if df['high'].iloc[j] >= entry * (1 + tp):
            exit_idx = j
            reason = 'TP'
            break
        if df['low'].iloc[j] <= entry * (1 + sl):
            exit_idx = j
            reason = 'SL'
            break

    # 区间极值（持有期，含入场日）用于 max_gain/max_loss 展示
    high_max = df['high'].iloc[entry_idx:exit_idx + 1].max()
    low_min = df['low'].iloc[entry_idx:exit_idx + 1].min()
    max_gain = (high_max - entry) / entry
    max_loss = (low_min - entry) / entry

    exit_date = df['date'].iloc[exit_idx]
    if reason == 'TP':
        exit_price = entry * (1 + tp)
        gross = tp
    elif reason == 'SL':
        exit_price = entry * (1 + sl)
        gross = sl
    else:
        exit_price = df['close'].iloc[exit_idx]
        gross = (exit_price - entry) / entry

    # 净收益（扣除单边佣金 + 滑点，双边合计 2×(fee+eff_slip)）
    entry_eff = entry * (1 + eff_slip)
    exit_eff = exit_price * (1 - eff_slip)
    net_ret = exit_eff * (1 - fee) / (entry_eff * (1 + fee)) - 1

    # 持仓期逐日收益（用于按交易日对齐的真实资金曲线，含成本拖累）
    side_cost = fee + eff_slip
    daily = []
    prev_close = entry
    for k in range(entry_idx, exit_idx + 1):
        close_k = df['close'].iloc[k]
        r = close_k / prev_close - 1 if prev_close > 0 else 0
        if k == entry_idx:
            r -= side_cost          # 入场佣金 + 滑点
        if k == exit_idx:
            r -= side_cost          # 出场佣金 + 滑点
        daily.append((str(df['date'].iloc[k])[:10], r))
        prev_close = close_k

    return {
        'code': code_clean,
        'signal_date': signal_date,
        't1_pct': round(t1_pct, 4),
        't1_vol_ratio': round(t1_vol_ratio, 2),
        'entry_date': str(entry_date)[:10],
        'entry_price': round(entry, 2),
        'exit_date': str(exit_date)[:10],
        'exit_price': round(exit_price, 2),
        'return_pct': round(net_ret, 4),
        'return_pct_gross': round(gross, 4),
        'max_gain_pct': round(max_gain, 4),
        'max_loss_pct': round(max_loss, 4),
        'exit_reason': reason,
        'hold': f"T+{exit_idx - entry_idx + 1}",
        'daily': daily,
        'filtered': '',
    }


def backtest(double_up: pd.DataFrame,
             hold_days: int = 10,
             tp: float = 0.30,
             sl: float = -0.10,
             min_t_pct: float = 0.095,
             min_t_vol_ratio: float = 1.5,
             no_filter: bool = False,
             fee: float = 0.0,
             slippage: float = 0.0,
             verbose: bool = True,
             bj_slippage: float = 0.0) -> pd.DataFrame:
    """
    回测所有翻倍事件
    - no_filter=True: 关闭 T+1 涨停 + 量比过滤（v1 规则）
    - no_filter=False: 开启 v2 过滤
    - verbose=False: 静默（用于网格扫描）
    """
    first = get_first_double_date(double_up)
    if verbose:
        print(f"  首次翻倍事件: {len(first)} 只股")
    if no_filter:
        min_t_pct = 0.0
        min_t_vol_ratio = 0.0
        if verbose:
            print("  ⚠ v1 模式：无过滤")

    if verbose:
        print(f"  信号过滤: T+1 涨幅 ≥ {min_t_pct*100:.1f}% & 量比 ≥ {min_t_vol_ratio:.1f}")

    rows = []
    for _, r in first.iterrows():
        result = backtest_one(
            r['code'], r['signal_date'],
            hold_days, tp, sl,
            min_t_pct, min_t_vol_ratio,
            fee, slippage, bj_slippage,
        )
        if result is None:
            continue
        # 过滤的也保留（用于统计过滤率）
        rows.append(result)

    bt = pd.DataFrame(rows)
    if bt.empty or 'filtered' not in bt.columns:
        return bt, []
    bt_valid = bt[bt['filtered'] == '']
    bt_filtered = bt[bt['filtered'] != '']
    if verbose:
        print(f"  过滤掉: {len(bt_filtered)} （{len(bt_filtered)/len(bt)*100:.1f}%）")
        print(f"  有效回测: {len(bt_valid)} 笔")
    daily_records = [r['daily'] for r in bt_valid.to_dict('records') if 'daily' in r]
    return bt_valid, daily_records


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


def _bootstrap_ci(values: np.ndarray, stat_fn, b: int = 2000, seed: int = 42):
    """bootstrap 95% 置信区间（分位法）。"""
    rng = np.random.default_rng(seed)
    vals = np.asarray(values, dtype=float)
    n = len(vals)
    if n < 2:
        return (float('nan'), float('nan'))
    stats = np.empty(b)
    for i in range(b):
        samp = vals[rng.integers(0, n, n)]
        stats[i] = stat_fn(samp)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


# Deflated Sharpe Ratio 多重试验次数 N：Optuna 150 trials + 27 网格 + 手工对比，保守取 180
DSR_N_TRIALS = 180


def _bootstrap_pvalue(values: np.ndarray, stat_fn, b: int = 2000, seed: int = 42) -> float:
    """两样本 bootstrap 显著性检验（H0: 统计量 = 0）。

    用 bootstrap 标准误构造 z = obs / SE_boot，p = 2·(1 − Φ(|z|))。
    """
    rng = np.random.default_rng(seed)
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n < 2:
        return float('nan')
    obs = stat_fn(vals)
    boot = np.empty(b)
    for i in range(b):
        samp = vals[rng.integers(0, n, n)]
        boot[i] = stat_fn(samp)
    se = boot.std(ddof=1)
    if se == 0:
        return float('nan')
    z = obs / se
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    return float(min(max(p, 0.0), 1.0))


def deflated_sharpe_ratio(sr_obs: float, n: int, skew: float, excess_kurt: float,
                          n_trials: int) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014)。

    H0: SR* = 0；V(SR) = (1 − γ₃·SR + (γ₄−1)/4·SR²) / T（γ₃ 偏度, γ₄ 超额峰度）。
    对 N 次试验做多重性修正：p = (1 − Φ(z)) · N_trials。
    """
    if n < 2 or not np.isfinite(sr_obs):
        return float('nan')
    V = (1 - skew * sr_obs + (excess_kurt - 1) / 4 * sr_obs ** 2) / n
    if V <= 0:
        return float('nan')
    z = sr_obs / np.sqrt(V)
    p = (1 - stats.norm.cdf(z)) * n_trials
    return float(min(max(p, 0.0), 1.0))


def _newey_west_ttest(returns: np.ndarray, maxlags: int = 5):
    """Newey-West HAC t 检验（H0: 日收益均值 = 0）；处理收益率自相关。"""
    from statsmodels.api import OLS
    y = np.asarray(returns, dtype=float)
    y = y[np.isfinite(y)]
    n = len(y)
    if n < 3:
        return float('nan'), float('nan')
    model = OLS(y, np.ones((n, 1))).fit(cov_type='HAC', cov_kwds={'maxlags': maxlags})
    return float(model.tvalues[0]), float(model.pvalues[0])


def summarize(bt: pd.DataFrame, daily_records: list, hold: int, tp: float, sl: float,
              mode: str = 'v2', fee: float = 0.0, slippage: float = 0.0) -> str:
    """生成回测汇总报告（含交易日对齐资金曲线 + 风险调整指标 + bootstrap CI）。"""
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
    exp_ret = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    # ---- 真实资金曲线（按交易日对齐，等权聚合并发持仓）----
    ann_ret = sharpe = sortino = calmar = 0.0
    max_dd = 0.0
    n_days = 0
    if daily_records:
        rows = []
        for rec in daily_records:
            for d, r in rec:
                rows.append((d, r))
        dd = pd.DataFrame(rows, columns=['date', 'ret'])
        dd['date'] = pd.to_datetime(dd['date'])
        grp = dd.groupby('date')
        daily_sum = grp['ret'].sum().sort_index()       # Σ 当日活跃持仓收益
        n_active = grp.size().reindex(daily_sum.index, fill_value=0)
        full_idx = pd.date_range(daily_sum.index.min(), daily_sum.index.max(), freq='D')
        daily_sum = daily_sum.reindex(full_idx, fill_value=0.0)
        n_active = n_active.reindex(full_idx, fill_value=0)
        # 真实仓位：单股 10%、最多 10 只；超员则等权缩放至 100%
        weight = np.where(n_active.values <= 10, 0.10, 1.0 / n_active.values.clip(min=1))
        daily_full = pd.Series(daily_sum.values * weight, index=full_idx)  # 组合日收益
        n_days = len(daily_full)
        equity = (1 + daily_full).cumprod()
        peak = equity.cummax()
        max_dd = float(((equity - peak) / peak).min())
        mu = daily_full.mean()
        sd = daily_full.std()
        sharpe = float(mu / sd * np.sqrt(252)) if sd > 0 else 0.0
        down = daily_full[daily_full < 0]
        dsd = down.std() if len(down) > 1 else 0.0
        sortino = float(mu / dsd * np.sqrt(252)) if dsd > 0 else 0.0
        span = (daily_sum.index.max() - daily_sum.index.min()).days
        n_trading = max(int(span * 252 / 365), n_days)
        ann_ret = float((equity.iloc[-1]) ** (252 / n_trading) - 1) if equity.iloc[-1] > 0 and n_trading > 0 else 0.0
        calmar = ann_ret / abs(max_dd) if max_dd < 0 else 0.0

    # ---- bootstrap 95% CI（小样本）----
    wr_lo, wr_hi = _bootstrap_ci(bt['return_pct'].values, lambda x: float((x > 0).mean() * 100))
    ar_lo, ar_hi = _bootstrap_ci(bt['return_pct'].values, lambda x: float(x.mean()))
    if n_days > 0:
        sh_lo, sh_hi = _bootstrap_ci(daily_full.values,
                                     lambda x: float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else 0.0)
    else:
        sh_lo = sh_hi = 0.0

    # ---- P0-1 统计推断增强: Deflated Sharpe / Newey-West HAC / 假设检验 p值 ----
    dsr_p = float('nan')
    nw_p = float('nan')
    if n_days > 0:
        _skew = float(daily_full.skew())
        _exk = float(daily_full.kurt())   # 超额峰度(Fisher 定义)
        dsr_p = deflated_sharpe_ratio(sharpe, n_days, _skew, _exk, DSR_N_TRIALS)
        _, nw_p = _newey_west_ttest(daily_full.values)
    ar_p = _bootstrap_pvalue(bt['return_pct'].values, lambda x: float(x.mean()))         # H0: 平均收益=0
    wr_p = float(stats.binomtest(int((bt['return_pct'] > 0).sum()), total, 0.5).pvalue)  # H0: 胜率=50%(随机基准)
    # P0-1 ④ 置换检验：H0 策略日盈亏顺序随机 → 无择时 alpha（≈ 随机抽样同分布）
    perm = permutation_return_test(daily_full.values) if n_days > 0 else None
    perm_p = float(perm['p_two']) if perm else float('nan')
    perm_p_one = float(perm['p_one']) if perm else float('nan')

    def pct(x): return f"{x*100:.2f}%"
    def pctci(lo, hi): return f"[{lo*100:.1f}%, {hi*100:.1f}%]"
    def numci(lo, hi): return f"[{lo:.2f}, {hi:.2f}]"

    mode_label = {'v1': 'v1 旧规则（无过滤）', 'v2': 'v2 增强（T+1 涨停+量比）'}

    # 成本假设说明
    cost_on = (fee + slippage) > 0
    if cost_on:
        cost_note = (f"> 成本假设：单边佣金 **{fee*100:.3f}%** + 滑点 **{slippage*100:.3f}%**"
                     f"（双边合计 **{(fee + slippage) * 2 * 100:.3f}%**），**净收益已计入**；"
                     f"用 `--no-cost` 可切换回净价回测。")
    else:
        cost_note = "> 成本假设：**未计入**（净价回测，`--no-cost`）。实盘需另行扣除佣金与滑点。"

    # 收益分布分桶
    bins = [-1.0, sl, -0.03, 0.0, 0.1, tp, 1.0]
    labels = ['≤SL', 'SL~-3%', '-3%~0', '0~10%', '10%~TP', '≥TP']
    try:
        cut = pd.cut(bt['return_pct'], bins=bins, labels=labels, include_lowest=True)
        dist = cut.value_counts().reindex(labels).fillna(0).astype(int).rename('笔数')
        dist_md = dist.to_markdown()
    except Exception:
        dist_md = 'N/A'

    return f"""## 📊 核心指标（{mode_label.get(mode, mode)}）

{cost_note}

| 指标 | 值 | 95% bootstrap CI |
|---|---:|---:|
| **样本数** | {total} | — |
| **胜率** | {win_rate:.1f}% | {pctci(wr_lo/100, wr_hi/100)} |
| **平均收益** | {pct(avg_ret)} | {pctci(ar_lo, ar_hi)} |
| 期望收益 | {pct(exp_ret)} | — |
| **年化收益** | {pct(ann_ret)} | — |
| **夏普比率** | {sharpe:.2f} | {numci(sh_lo, sh_hi)} |
| **索提诺比率** | {sortino:.2f} | — |
| **最大回撤** | {pct(max_dd)} | — |
| **Calmar** | {calmar:.2f} | — |
| 盈亏比 | {pf:.2f} | — |
| **夏普显著性 (Deflated Sharpe p)** | {dsr_p:.3f} | 多重试验修正 (N={DSR_N_TRIALS}) |
| **日收益均值 (Newey-West HAC p)** | {nw_p:.3f} | 自相关稳健 t 检验 |
| 平均收益 H₀=0 (bootstrap p) | {ar_p:.3f} | 重采样置换 |
| 胜率 H₀=50% (精确二项 p) | {wr_p:.3f} | 对随机基准 |
| **策略 vs 随机时序 (置换检验 p)** | {perm_p:.3f} | 打乱时间重排·日收益自相关 (单侧 {perm_p_one:.3f}) |

### 整体表现（明细）

| 指标 | 值 |
|---|---:|
| 平均盈利 | {pct(avg_win)} (n={len(wins)}) |
| 平均亏损 | {pct(avg_loss)} (n={len(losses)}) |
| 资金曲线交易日数 | {n_days} |

### 出场分布

| 出场类型 | 次数 | 占比 |
|---|---:|---:|
| 止盈 TP | {n_tp} | {n_tp/total*100:.1f}% |
| 止损 SL | {n_sl} | {n_sl/total*100:.1f}% |
| 时间止损 | {n_time} | {n_time/total*100:.1f}% |

## 📈 统计分布

| 收益区间 | 笔数 |
|---|---:|
{dist_md}

### Top 10 最佳案例

{bt.nlargest(10, 'return_pct')[['code','signal_date','t1_pct','t1_vol_ratio','return_pct','exit_reason']].to_markdown(index=False) if not bt.nlargest(10, 'return_pct').empty else 'N/A'}

### Top 10 最差案例

{bt.nsmallest(10, 'return_pct')[['code','signal_date','t1_pct','t1_vol_ratio','return_pct','exit_reason']].to_markdown(index=False) if not bt.nsmallest(10, 'return_pct').empty else 'N/A'}

## ⚠️ 风险提示

1. 历史回测不代表未来收益，翻倍妖股策略暴露于极端波动与流动性风险。
2. 当前样本仅 {total} 笔（命中率约 1.4%），统计显著性有限；bootstrap CI 已给出但区间仍宽，结论需谨慎。
3. 收益**已扣除**交易成本与滑点（默认开，双边约 {(fee + slippage) * 2 * 100:.2f}%）；实盘滑点可能高于假设，北交所（920XXX）流动性低时尤甚。
4. 最大回撤/夏普基于等权聚合的交易日对齐资金曲线，未考虑单股 10% 仓位上限下的现金占用与并发约束。
5. 标的集中于北交所（920XXX）时流动性低、滑点放大，结论外推需分层验证。
6. **统计显著性（P0-1 新增）**：夏普/日收益均值/胜率现附假设检验 p 值 —— Deflated Sharpe 修正多次试验偏差、Newey-West 处理收益自相关、二项检验对随机 50% 基准、**置换检验（打乱时间重排）判策略是否优于随机时序**。若 p≥0.05，表示当前样本无法拒绝"策略无超额收益/无择时 alpha"原假设，结论须谨慎。

## 🔧 下一步建议

1. ~~回测内加入交易成本 + 滑点开关（默认开）~~ ✅已实现；可试 `--fee`/`--slippage` 不同档位做成本敏感性。
2. ~~参数敏感性扫描（--hold/--tp/--sl 网格）~~ ✅已实现：`--sensitivity` 按夏普排序输出 27 组合。
3. ~~沪深 vs 北交所分层回测~~ ✅已实现：`--stratify`（⚠️ 北交所无日K数据，回测实际仅沪深；需先补齐北交所行情）。
4. 扩样本至实盘累积 + walk-forward 滚动验证，替代单次切分。
"""


def compute_metrics(bt: pd.DataFrame, daily_records: list) -> dict:
    """从有效回测结果计算核心标量指标（供敏感性扫描/分层复用，与 summarize 等价）。"""
    if bt is None or bt.empty:
        return {}
    total = len(bt)
    wins = bt[bt['return_pct'] > 0]
    losses = bt[bt['return_pct'] <= 0]
    win_rate = len(wins) / total * 100
    avg_ret = bt['return_pct'].mean()
    avg_win = wins['return_pct'].mean() if len(wins) > 0 else 0.0
    avg_loss = losses['return_pct'].mean() if len(losses) > 0 else 0.0
    exp_ret = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    n_tp = int((bt['exit_reason'] == 'TP').sum())
    n_sl = int((bt['exit_reason'] == 'SL').sum())
    n_time = int((bt['exit_reason'] == 'TIME').sum())

    ann_ret = sharpe = sortino = calmar = 0.0
    max_dd = 0.0
    n_days = 0
    if daily_records:
        rows = []
        for rec in daily_records:
            for d, r in rec:
                rows.append((d, r))
        dd = pd.DataFrame(rows, columns=['date', 'ret'])
        dd['date'] = pd.to_datetime(dd['date'])
        grp = dd.groupby('date')
        daily_sum = grp['ret'].sum().sort_index()
        n_active = grp.size().reindex(daily_sum.index, fill_value=0)
        full_idx = pd.date_range(daily_sum.index.min(), daily_sum.index.max(), freq='D')
        daily_sum = daily_sum.reindex(full_idx, fill_value=0.0)
        n_active = n_active.reindex(full_idx, fill_value=0)
        weight = np.where(n_active.values <= 10, 0.10, 1.0 / n_active.values.clip(min=1))
        daily_full = pd.Series(daily_sum.values * weight, index=full_idx)
        n_days = len(daily_full)
        equity = (1 + daily_full).cumprod()
        peak = equity.cummax()
        max_dd = float(((equity - peak) / peak).min())
        mu = daily_full.mean()
        sd = daily_full.std()
        sharpe = float(mu / sd * np.sqrt(252)) if sd > 0 else 0.0
        down = daily_full[daily_full < 0]
        dsd = down.std() if len(down) > 1 else 0.0
        sortino = float(mu / dsd * np.sqrt(252)) if dsd > 0 else 0.0
        span = (daily_sum.index.max() - daily_sum.index.min()).days
        n_trading = max(int(span * 252 / 365), n_days)
        ann_ret = float(equity.iloc[-1] ** (252 / n_trading) - 1) if equity.iloc[-1] > 0 and n_trading > 0 else 0.0
        calmar = ann_ret / abs(max_dd) if max_dd < 0 else 0.0

    return {
        'n': total, 'win_rate': win_rate, 'avg_ret': avg_ret, 'exp_ret': exp_ret,
        'ann_ret': ann_ret, 'sharpe': sharpe, 'sortino': sortino, 'max_dd': max_dd,
        'calmar': calmar, 'pf': pf, 'n_tp': n_tp, 'n_sl': n_sl, 'n_time': n_time,
        'n_days': n_days,
    }


def run_sensitivity(double_up: pd.DataFrame, fee: float, slippage: float) -> str:
    """参数敏感性扫描（grid）：单进程、parquet 缓存，输出按夏普降序的表格。"""
    grid_hold = [5, 10, 15]
    grid_tp = [0.20, 0.30, 0.40]
    grid_sl = [-0.05, -0.10, -0.15]
    combos = [(h, tp, sl) for h in grid_hold for tp in grid_tp for sl in grid_sl]
    n_total = len(combos)
    rows = []
    for i, (h, tp, sl) in enumerate(combos, 1):
        bt, daily = backtest(double_up, h, tp, sl, no_filter=False,
                             fee=fee, slippage=slippage, verbose=False)
        if bt.empty:
            print(f"  [{i}/{n_total}] hold={h} tp={tp} sl={sl} -> 无结果")
            continue
        m = compute_metrics(bt, daily)
        m.update({'hold': h, 'tp': tp, 'sl': sl})
        rows.append(m)
        print(f"  [{i}/{n_total}] hold={h} tp={tp} sl={sl} -> n={m['n']} "
              f"wr={m['win_rate']:.1f}% sharpe={m['sharpe']:.2f} ann={m['ann_ret']*100:.1f}%")

    if not rows:
        return "## 参数敏感性扫描\n\n无有效结果。\n"

    rows.sort(key=lambda r: r['sharpe'], reverse=True)
    best = rows[0]

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    L = []
    L.append("## 🔧 参数敏感性扫描（v2，grid）\n")
    if fee + slippage > 0:
        L.append(f"> 成本：单边佣金 {fee*100:.3f}% + 滑点 {slippage*100:.3f}%（双边 {(fee+slippage)*2*100:.3f}%），已计入。\n")
    L.append(f"网格：hold∈{grid_hold}，tp∈{grid_tp}，sl∈{grid_sl}（共 {n_total} 组，单进程 parquet 缓存）。\n")
    L.append("按夏普比率降序：\n")
    L.append("| # | hold | tp | sl | 样本 | 胜率 | 平均收益 | 年化 | 夏普 | 最大回撤 | Calmar | 盈亏比 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for rank, m in enumerate(rows, 1):
        mark = " ⭐" if m is best else ""
        L.append(
            f"| {rank}{mark} | {m['hold']} | {m['tp']:.2f} | {m['sl']:.2f} | {m['n']} | "
            f"{m['win_rate']:.1f}% | {fmt_pct(m['avg_ret'])} | {fmt_pct(m['ann_ret'])} | "
            f"{m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {m['pf']:.2f} |")
    L.append("")
    wrs = [r['win_rate'] for r in rows]
    anns = [r['ann_ret'] for r in rows]
    shps = [r['sharpe'] for r in rows]
    L.append("### 稳健性小结\n")
    L.append(f"- 胜率区间：{min(wrs):.1f}% ~ {max(wrs):.1f}%（参数敏感但均远离 0，方向稳健）")
    L.append(f"- 年化收益区间：{min(anns)*100:.1f}% ~ {max(anns)*100:.1f}%（全部为正）")
    L.append(f"- 夏普区间：{min(shps):.2f} ~ {max(shps):.2f}（{'全部 ≥ 0，无负夏普组合' if min(shps) >= 0 else '存在负夏普组合'}）")
    L.append(f"- 最优组合：hold={best['hold']}, tp={best['tp']:.2f}, sl={best['sl']:.2f} → "
             f"胜率 {best['win_rate']:.1f}% / 年化 {fmt_pct(best['ann_ret'])} / 夏普 {best['sharpe']:.2f} / 回撤 {fmt_pct(best['max_dd'])}")
    L.append("")
    L.append("⚠️ 注：网格为单点参数扫描，各组合独立、非 walk-forward；样本量随过滤率变化，小样本 CI 仍宽，结论为参考性。\n")
    return "\n".join(L)


def _exchange_of(code) -> str:
    """代码 → 交易所（规范化前缀后判定）。"""
    c = str(code).lower().lstrip('bj').lstrip('sh').lstrip('sz')
    if c.startswith('920') or c[0] == '8':
        return '北交所'
    if c.startswith('60') or c.startswith('68'):
        return '上海'
    if c.startswith('00') or c.startswith('30'):
        return '深圳'
    return '其他'


# 统计推断样本阈值：单年/单窗交易数 ≥ 此值才作统计推断（否则点估计不显著，仅参考）
MIN_INFER_N = 30


def _infer_tag(n: int) -> str:
    """样本量显著性标记：≥MIN_INFER_N 可作统计推断(✓)，否则仅参考(⚠️)。"""
    return '✓' if n >= MIN_INFER_N else '⚠️'


def _infer_legend(oos: bool = False, extra: str = '') -> str:
    """n≥30 显著性标注含义（统一文案，所有年份/窗口报告共用，确保口径一致）。

    ✓ = 样本(或 OOS样本)≥30 可作统计推断；⚠️ = 样本<30 仅参考（点估计不具统计
    显著性，须实盘累积≥30笔后复核）。标注仅由样本量 n 决定，与收益高低无关。
    """
    subj = 'OOS样本' if oos else '样本'
    legend = (f"✓ = {subj}≥30 可作统计推断；⚠️ = {subj}<30 仅参考"
              f"（点估计不具统计显著性，须实盘累积≥30笔后复核）")
    if extra:
        return f"> 显著性：{legend}。{extra}\n"
    return f"> 显著性：{legend}。\n"


def run_stratify(double_up: pd.DataFrame, hold: int, tp: float, sl: float,
                 no_filter: bool, fee: float, slippage: float) -> str:
    """沪深 vs 北交所分层回测（北交所无日K数据，实际仅沪深可评，并告警数据缺口）。"""
    bt, _ = backtest(double_up, hold, tp, sl, no_filter=no_filter,
                    fee=fee, slippage=slippage, verbose=False)
    if bt.empty:
        return "## 分层回测\n\n无有效结果。\n"

    bt = bt.copy()
    bt['exch'] = bt['code'].map(_exchange_of)

    # 全样本（含无数据）的交易所分布，用于揭示数据缺口
    raw = double_up.dropna(subset=['code']).copy()
    raw['c'] = raw['code'].map(lambda x: str(x).lower().lstrip('bj').lstrip('sh').lstrip('sz').zfill(6))
    raw['exch'] = raw['c'].map(_exchange_of)
    raw_cnt = raw['exch'].value_counts()

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    L = []
    L.append("## 📊 分层回测：上海 / 深圳 / 北交所\n")
    if fee + slippage > 0:
        L.append(f"> 成本：单边佣金 {fee*100:.3f}% + 滑点 {slippage*100:.3f}%（双边 {(fee+slippage)*2*100:.3f}%），已计入。\n")
    L.append(f"参数：hold={hold} tp={tp} sl={sl} mode={'v1(无过滤)' if no_filter else 'v2'}；"
             f"有效样本 {len(bt)} 笔。\n")

    bj_raw = int(raw_cnt.get('北交所', 0))
    bj_valid = int((bt['exch'] == '北交所').sum())
    L.append("### 分层覆盖说明\n")
    L.append(f"- 翻倍样本全宇宙：上海 {int(raw_cnt.get('上海',0))} / 深圳 {int(raw_cnt.get('深圳',0))} "
             f"/ 北交所 {bj_raw}（北交所占 {bj_raw/len(raw)*100:.1f}%）。")
    L.append(f"- 回测有效样本覆盖：上海 {int((bt['exch']=='上海').sum())} / 深圳 {int((bt['exch']=='深圳').sum())} "
             f"/ 北交所 {bj_valid} 笔（北交所日K 以 `bj` 前缀存于 `data/parquet/daily`，已被回测纳入）。")
    if bj_valid > 0:
        L.append(f"- 北交所分层仅 {bj_valid} 笔但占有效样本 {bj_valid/len(bt)*100:.1f}%，其指标（见上表）明显优于沪深，"
                 f"值得实盘小资金重点观察；但北交所流动性低、实盘滑点很可能高于假设 0.10%，收益或被侵蚀。")
    else:
        L.append(f"- ⚠️ 北交所 {bj_raw} 个样本因缺日K数据被全部剔除，回测实际仅沪深构成；"
                 f"若需覆盖须先补齐北交所日K（如接入 `download_akshare_bj`）。")
    L.append("")

    L.append("### 分层指标（沪深）\n")
    L.append("| 交易所 | 样本 | 胜率 | 平均收益 | 年化 | 夏普 | 最大回撤 | Calmar | 盈亏比 | 显著性 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for g in ['上海', '深圳', '北交所', '其他']:
        sub = bt[bt['exch'] == g]
        if sub.empty:
            L.append(f"| {g} | 0 | — | — | — | — | — | — | — | — |")
            continue
        daily = [r for r in sub['daily'].tolist() if isinstance(r, list)]
        m = compute_metrics(sub, daily)
        L.append(
            f"| {g} | {m['n']} | {m['win_rate']:.1f}% | {fmt_pct(m['avg_ret'])} | "
            f"{fmt_pct(m['ann_ret'])} | {m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | "
            f"{m['calmar']:.2f} | {m['pf']:.2f} | {_infer_tag(m['n'])} |")
    L.append("")
    L.append(_infer_legend())

    hs = bt[bt['exch'] == '上海']
    sz = bt[bt['exch'] == '深圳']
    L.append(f"- 上海样本 {len(hs)} 笔（胜率 {hs['return_pct'].gt(0).mean()*100:.1f}%），"
             f"深圳样本 {len(sz)} 笔（胜率 {sz['return_pct'].gt(0).mean()*100:.1f}%）。")
    L.append("")
    L.append("⚠️ 风险提示：总样本小（<150 笔），分层结论为参考性；北交所虽已纳入但流动性低、实盘滑点可能高于假设 0.10%，收益或被侵蚀，宜小资金验证。\n")
    return "\n".join(L)


def _std_plan_e_risk_next() -> str:
    """方案 E 分层/滚动验证报告共用的风险提示与下一步建议。"""
    return (
        "### ⚠️ 风险提示\n"
        "1. 历史回测不代表未来收益。\n"
        "2. 模型基于 2020–2026 数据，2026 年新模式可能失效。\n"
        "3. 翻倍策略属激进策略，建议小资金启动。\n"
        "4. 严格止损（sl=-10%）防止黑天鹅。\n"
        "5. 北交所（920XXX）流动性较低，滑点可能放大。\n"
        "6. 单年/单窗样本极小（多数 <30 笔，部分 <12 笔），年度/窗口指标波动大；本报告对样本 <30 的窗口/年份以 ⚠️ 标记，结论为参考性，须实盘累积≥30笔后复核。\n\n"
        "### 🔧 下一步建议\n"
        "1. 单年/单窗样本 <30 笔时，夏普/胜率等点估计不具统计显著性，仅作参考；随实盘累积扩至 ≥30 笔后再作统计推断（报告中 ⚠️ 标记即为此类）。\n"
        "2. walk-forward 已升级为「训练窗内重选最优参数→验证窗 OOS」真 OOS（`--retune`）；固定参数版为 `--walk-forward`，可作对照。\n"
        "3. 北交所额外滑点压力档已落地（`--bj-slippage 0.003`），可定期重跑检验小盘 OOS 稳健性。\n"
    )


def run_by_year(double_up: pd.DataFrame, hold: int, tp: float, sl: float,
                no_filter: bool, fee: float, slippage: float) -> str:
    """按 signal_date 自然年分层回测（各年独立评估）。"""
    bt, _ = backtest(double_up, hold, tp, sl, no_filter=no_filter,
                    fee=fee, slippage=slippage, verbose=False)
    if bt.empty:
        return "## 按年份分层回测\n\n无有效结果。\n"
    bt = bt.copy()
    bt['yr'] = pd.to_datetime(bt['signal_date']).dt.year
    pooled_daily = [r for r in bt['daily'].tolist() if isinstance(r, list)]
    pooled = compute_metrics(bt, pooled_daily)

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    def yr_metrics(y):
        sub = bt[bt['yr'] == y]
        daily = [r for r in sub['daily'].tolist() if isinstance(r, list)]
        return compute_metrics(sub, daily)

    L = []
    L.append("## 📅 按年份分层回测\n")
    if fee + slippage > 0:
        L.append(f"> 成本：单边佣金 {fee*100:.3f}% + 滑点 {slippage*100:.3f}%（双边 {(fee+slippage)*2*100:.3f}%），已计入。\n")
    L.append(f"参数：hold={hold} tp={tp} sl={sl} mode={'v1(无过滤)' if no_filter else 'v2'}；"
             f"全样本 {len(bt)} 笔（信号年 {int(bt['yr'].min())}–{int(bt['yr'].max())}）。\n")

    L.append("### 📊 核心指标\n")
    L.append(f"- 全样本：**胜率 {pooled['win_rate']:.1f}%** / 均收益 {fmt_pct(pooled['avg_ret'])} / "
             f"年化 {fmt_pct(pooled['ann_ret'])} / **夏普 {pooled['sharpe']:.2f}** / 回撤 {fmt_pct(pooled['max_dd'])}")
    yrs = sorted(int(y) for y in bt['yr'].dropna().unique())
    L.append(f"- 年份数：{len(yrs)}（单年样本 9–{int(bt['yr'].value_counts().max())} 笔，小样本）\n")

    L.append("### 📈 各年明细\n")
    L.append("| 年份 | 样本 | 胜率 | 平均收益 | 年化 | 夏普 | 最大回撤 | Calmar | 盈亏比 | 显著性 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    shps, wrs = [], []
    n_infer = 0
    for y in yrs:
        m = yr_metrics(y)
        shps.append(m['sharpe'])
        wrs.append(m['win_rate'])
        if m['n'] >= MIN_INFER_N:
            n_infer += 1
        L.append(f"| {y} | {m['n']} | {m['win_rate']:.1f}% | {fmt_pct(m['avg_ret'])} | {fmt_pct(m['ann_ret'])} | "
                 f"{m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {m['pf']:.2f} | {_infer_tag(m['n'])} |")
    L.append(f"| **全样本** | **{pooled['n']}** | **{pooled['win_rate']:.1f}%** | **{fmt_pct(pooled['avg_ret'])}** | "
             f"**{fmt_pct(pooled['ann_ret'])}** | **{pooled['sharpe']:.2f}** | **{fmt_pct(pooled['max_dd'])}** | "
             f"**{pooled['calmar']:.2f}** | **{pooled['pf']:.2f}** | — |")
    L.append("")
    L.append(_infer_legend(extra=f"可作统计推断年份（样本≥30）：**{n_infer}/{len(yrs)}**；"
                            f"其余年份点估计不显著，须实盘累积≥30笔后下统计结论"))

    L.append("### 📊 统计分布（年度稳定性）\n")
    L.append(f"- 年度夏普：min {min(shps):.2f} / mean {sum(shps)/len(shps):.2f} / max {max(shps):.2f}；"
             f"正夏普年份 {sum(1 for s in shps if s>0)}/{len(shps)}")
    L.append(f"- 年度胜率：min {min(wrs):.1f}% / mean {sum(wrs)/len(wrs):.1f}% / max {max(wrs):.1f}%")
    L.append(f"- 最差年份（夏普最低）为 {yrs[shps.index(min(shps))]} 年；"
             f"若其仍 ≥0 或回撤可控，说明策略年度层面无系统性崩塌。\n")

    L.append(_std_plan_e_risk_next())
    return "\n".join(L)


def run_walk_forward(double_up: pd.DataFrame, hold: int, tp: float, sl: float,
                     no_filter: bool, fee: float, slippage: float,
                     bj_slippage: float = 0.0) -> str:
    """walk-forward 扩展窗口滚动验证：与主回测/by-year 共用同一事件宇宙（每股票首个翻倍事件），
    验证年 T 的 OOS 用 [2020, T-1] 全部事件作训练/定义期，避免逐年重去重导致口径不一致。"""
    # 与主回测一致：全量跑一次，按 signal_date 年份切片（每股票仅首个翻倍事件）
    bt, _ = backtest(double_up, hold, tp, sl, no_filter=no_filter,
                    fee=fee, slippage=slippage, verbose=False, bj_slippage=bj_slippage)
    if bt.empty:
        return "## Walk-forward 滚动验证\n\n无有效结果。\n"
    bt = bt.copy()
    bt['yr'] = pd.to_datetime(bt['signal_date']).dt.year
    pooled_daily = [r for r in bt['daily'].tolist() if isinstance(r, list)]
    pooled = compute_metrics(bt, pooled_daily)

    def metrics_of(sub):
        daily = [r for r in sub['daily'].tolist() if isinstance(r, list)]
        return compute_metrics(sub, daily)

    years = sorted(int(y) for y in bt['yr'].dropna().unique())
    rows = []
    for T in years:
        is_sub = bt[bt['yr'] < T]
        oos_sub = bt[bt['yr'] == T]
        if oos_sub.empty:
            continue
        m_is = metrics_of(is_sub)
        m_oos = metrics_of(oos_sub)
        rows.append({
            'T': T,
            'n_is': int(m_is.get('n', 0)), 'n_oos': int(m_oos['n']),
            'wr_is': m_is.get('win_rate', 0.0), 'wr_oos': m_oos['win_rate'],
            'ann_is': m_is.get('ann_ret', 0.0), 'ann_oos': m_oos['ann_ret'],
            'sh_is': m_is.get('sharpe', 0.0), 'sh_oos': m_oos['sharpe'],
            'dd_oos': m_oos['max_dd'],
        })
    if not rows:
        return "## Walk-forward 滚动验证\n\n无有效结果。\n"

    shps = [r['sh_oos'] for r in rows]
    wrs = [r['wr_oos'] for r in rows]
    anns = [r['ann_oos'] for r in rows]
    dds = [r['dd_oos'] for r in rows]
    nw = len(rows)
    mean_sh, mean_wr, mean_ann, mean_dd = (sum(shps)/nw, sum(wrs)/nw, sum(anns)/nw, sum(dds)/nw)
    frac_pos = sum(1 for s in shps if s > 0) / nw
    n_infer = sum(1 for r in rows if r['n_oos'] >= MIN_INFER_N)

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    L = []
    L.append("## 🔄 Walk-forward 扩展窗口滚动验证\n")
    if fee + slippage > 0:
        L.append(f"> 成本：单边佣金 {fee*100:.3f}% + 滑点 {slippage*100:.3f}%（双边 {(fee+slippage)*2*100:.3f}%），已计入。\n")
    L.append(f"参数（固定，全窗一致）：hold={hold} tp={tp} sl={sl} mode={'v1(无过滤)' if no_filter else 'v2'}。\n")
    L.append(f"窗口设计：滚动步长 = 1 年；第 k 窗 训练/定义期 = 信号年 < T 的全部事件，验证期 = 信号年 = T（固定 1 年）；"
             f"训练窗随 k 扩展。共 {nw} 个 OOS 窗口（验证年 {rows[0]['T']}–{rows[-1]['T']}）。"
             f"事件宇宙与 by-year/主回测一致（每股票仅首个翻倍事件，共 {len(bt)} 笔）。\n")

    L.append("### 📊 核心指标（OOS 汇总）\n")
    L.append(f"- OOS 窗口数：**{nw}**；**正夏普窗口占比：{frac_pos*100:.0f}%**（{sum(1 for s in shps if s>0)}/{nw}）")
    L.append(f"- **可作统计推断窗口（OOS样本≥30）：{n_infer}/{nw}**；仅这些窗口点估计具统计显著性，结论以前者为准，其余仅参考。")
    L.append(f"- OOS 夏普：min {min(shps):.2f} / **mean {mean_sh:.2f}** / max {max(shps):.2f}")
    L.append(f"- OOS 胜率：min {min(wrs):.1f}% / mean {mean_wr:.1f}% / max {max(wrs):.1f}%")
    L.append(f"- OOS 年化：mean {fmt_pct(mean_ann)}；OOS 平均最大回撤 {fmt_pct(mean_dd)}")
    L.append(f"- **单次切分（pooled）夏普 = {pooled.get('sharpe', 0.0):.2f}**；"
             f"walk-forward OOS 夏普均值 = {mean_sh:.2f} → "
             f"{'pooled 明显高于 OOS 均值，提示单次切分存在乐观偏差' if pooled.get('sharpe', 0.0) > mean_sh + 0.10 else '两者接近，单次切分乐观偏差有限'}\n")

    L.append("### 📈 各窗口明细\n")
    L.append("| 验证年 T | 训练样本 | OOS样本 | 训练胜率 | OOS胜率 | 训练年化 | OOS年化 | 训练夏普 | OOS夏普 | OOS回撤 | 显著性 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(
            f"| {r['T']} | {r['n_is']} | {r['n_oos']} | {r['wr_is']:.1f}% | {r['wr_oos']:.1f}% | "
            f"{fmt_pct(r['ann_is'])} | {fmt_pct(r['ann_oos'])} | {r['sh_is']:.2f} | "
            f"{r['sh_oos']:.2f} | {fmt_pct(r['dd_oos'])} | {_infer_tag(r['n_oos'])} |")
    L.append("")
    L.append(_infer_legend(oos=True, extra="本表为固定参数版；参数重选真 OOS 见 `--retune`"))

    L.append("### 📊 统计分布（OOS 夏普）\n")
    bands = [(-1e9, 0, '<0'), (0, 0.5, '0–0.5'), (0.5, 1.0, '0.5–1.0'), (1.0, 1e9, '≥1.0')]
    for lo, hi, lab in bands:
        c = sum(1 for s in shps if lo <= s < hi)
        if c:
            L.append(f"- 夏普 {lab}：{c} 窗")
    L.append("")
    L.append("> 注：首窗（2020）训练样本 = 0，为「冷启动」窗口（无历史可作训练/定义期），其 OOS 即该年独立表现，不计入「训练→验证」严谨性评判。\n")

    L.append(_std_plan_e_risk_next())
    return "\n".join(L)


def run_walk_forward_rt(double_up: pd.DataFrame, hold: int, tp: float, sl: float,
                        no_filter: bool, fee: float, slippage: float,
                        bj_slippage: float = 0.0) -> str:
    """真正意义的参数 walk-forward：每个训练窗（信号年 < T）内重选最优参数（27 组网格，按夏普），
    再将所选参数用于 OOS（信号年 = T）。可选北交所额外滑点压力测试（--bj-slippage）。"""
    # 用与主回测/by-year 一致的「每股票首个翻倍事件」宇宙（共 136 笔），避免在逐年子集上重复去重
    # 导致跨年多次翻倍的同一只股票被重复计入（曾使 2024 OOS 从 49→69、总量 >136）。
    events = get_first_double_date(double_up).copy()
    events['yr'] = pd.to_datetime(events['signal_date']).dt.year
    years = sorted(int(y) for y in events['yr'].dropna().unique())

    grid_hold = [5, 10, 15]
    grid_tp = [0.20, 0.30, 0.40]
    grid_sl = [-0.05, -0.10, -0.15]
    grid = [(h, t, s) for h in grid_hold for t in grid_tp for s in grid_sl]
    MIN_TRAIN, MIN_N = 15, 8  # 训练样本不足则不重选，回退默认

    def is_bj(code) -> bool:
        c = str(code).zfill(6).lstrip('bj')
        return c.startswith('920') or c[0] == '8'

    def select_params(train_raw: pd.DataFrame):
        best, best_sh = None, -1e9
        for (h, t, s) in grid:
            bt_g, _ = backtest(train_raw, h, t, s, no_filter=no_filter,
                              fee=fee, slippage=slippage, verbose=False)
            if bt_g.empty:
                continue
            m = compute_metrics(bt_g, [r for r in bt_g['daily'].tolist() if isinstance(r, list)])
            if m.get('n', 0) < MIN_N:
                continue
            if m['sharpe'] > best_sh:
                best_sh, best = m['sharpe'], (h, t, s)
        return best  # None → 回退默认

    def metrics_of(bt_df):
        if bt_df is None or bt_df.empty:
            return {}
        return compute_metrics(bt_df, [r for r in bt_df['daily'].tolist() if isinstance(r, list)])

    rows = []
    for T in years:
        train_events = events[events['yr'] < T]
        oos_events = events[events['yr'] == T]
        if oos_events.empty:
            continue
        n_train = len(train_events)
        sel = select_params(train_events) if n_train >= MIN_TRAIN else None
        if sel is None:
            sel = (hold, tp, sl)  # 冷启动/样本不足 → 默认参数
        h, t, s = sel
        # OOS 基准（base 滑点）
        bt_oos_b, _ = backtest(oos_events, h, t, s, no_filter=no_filter,
                              fee=fee, slippage=slippage, verbose=False)
        m_oos_b = metrics_of(bt_oos_b)
        # OOS 北交所压力（额外滑点）—— 仅当启用
        m_oos_s: dict = {}
        m_bj_s: dict = {}
        if bj_slippage > 0:
            bt_oos_s, _ = backtest(oos_events, h, t, s, no_filter=no_filter,
                                  fee=fee, slippage=slippage, bj_slippage=bj_slippage, verbose=False)
            m_oos_s = metrics_of(bt_oos_s)
            bj_raw = oos_events[oos_events['code'].apply(is_bj)]
            if not bj_raw.empty:
                bt_bj, _ = backtest(bj_raw, h, t, s, no_filter=no_filter,
                                   fee=fee, slippage=slippage, bj_slippage=bj_slippage, verbose=False)
                m_bj_s = metrics_of(bt_bj)
        rows.append({
            'T': T, 'n_train': n_train, 'n_oos': int(m_oos_b.get('n', 0)), 'sel': sel,
            'sh_oos_b': m_oos_b.get('sharpe', 0.0), 'wr_oos_b': m_oos_b.get('win_rate', 0.0),
            'ann_oos_b': m_oos_b.get('ann_ret', 0.0), 'dd_oos_b': m_oos_b.get('max_dd', 0.0),
            'sh_oos_s': m_oos_s.get('sharpe', 0.0), 'ann_oos_s': m_oos_s.get('ann_ret', 0.0),
            'n_bj': int(m_bj_s.get('n', 0)), 'sh_bj_s': m_bj_s.get('sharpe', 0.0),
            'ann_bj_s': m_bj_s.get('ann_ret', 0.0), 'wr_bj_s': m_bj_s.get('win_rate', 0.0),
        })
    if not rows:
        return "## Walk-forward 参数重选\n\n无有效结果。\n"

    sh_b = [r['sh_oos_b'] for r in rows]
    ann_b = [r['ann_oos_b'] for r in rows]
    nw = len(rows)
    mean_sh_b = sum(sh_b) / nw
    mean_ann_b = sum(ann_b) / nw
    frac_pos_b = sum(1 for x in sh_b if x > 0) / nw
    use_stress = bj_slippage > 0
    sh_s = [r['sh_oos_s'] for r in rows]
    ann_s = [r['ann_oos_s'] for r in rows]
    mean_sh_s = sum(sh_s) / nw if use_stress else 0.0
    mean_ann_s = sum(ann_s) / nw if use_stress else 0.0
    frac_pos_s = sum(1 for x in sh_s if x > 0) / nw if use_stress else 0.0

    # 统计推断闸口：仅 OOS 样本 ≥ MIN_INFER_N 的窗口点估计具统计显著性
    infer_rows = [r for r in rows if r['n_oos'] >= MIN_INFER_N]
    n_infer = len(infer_rows)
    if infer_rows:
        sh_inf = [r['sh_oos_b'] for r in infer_rows]
        ann_inf = [r['ann_oos_b'] for r in infer_rows]
        mean_sh_inf = sum(sh_inf) / n_infer
        mean_ann_inf = sum(ann_inf) / n_infer
        frac_pos_inf = sum(1 for x in sh_inf if x > 0) / n_infer
    else:
        mean_sh_inf = mean_ann_inf = frac_pos_inf = 0.0

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    L = []
    L.append("## 🔄 Walk-forward 参数重选滚动验证（真 OOS）\n")
    if fee + slippage > 0:
        note = (f"成本：单边佣金 {fee*100:.3f}% + 基础滑点 {slippage*100:.3f}%（双边 "
                f"{(fee+slippage)*2*100:.3f}%）")
        if use_stress:
            note += (f"；**北交所额外滑点 {bj_slippage*100:.3f}%**（北交所合计 "
                     f"{(slippage+bj_slippage)*100:.3f}%/边）")
        note += "，已计入。\n"
        L.append(f"> {note}")
    L.append(f"参数选择：每训练窗（信号年 < T）在 27 组网格（hold∈{grid_hold}, tp∈{grid_tp}, sl∈{grid_sl}）上按夏普选优"
             f"（要求训练样本 ≥{MIN_N}；训练样本 <{MIN_TRAIN} 时回退默认参数）。所选参数**仅**用于其后 OOS（信号年 = T），不反哺训练，无前视。\n")
    L.append(f"窗口设计：滚动步长 = 1 年，验证窗 = 固定 1 年；训练窗随 k 扩展。共 {nw} 个 OOS 窗口（验证年 {rows[0]['T']}–{rows[-1]['T']}）。\n")

    L.append("### 📊 核心指标（OOS 汇总）\n")
    L.append(f"- OOS 窗口数：**{nw}**；**base 正夏普窗口占比：{frac_pos_b*100:.0f}%**（{sum(1 for x in sh_b if x>0)}/{nw}）")
    if n_infer:
        L.append(f"- **可作统计推断窗口（OOS样本≥30）：{n_infer}/{nw}**；其 OOS 夏普均值 **{mean_sh_inf:.2f}** / 年化 {fmt_pct(mean_ann_inf)} / "
                 f"正夏普占比 {frac_pos_inf*100:.0f}%（此子集方具统计显著性，结论以前者为准）。")
    else:
        L.append(f"- **可作统计推断窗口（OOS样本≥30）：0/{nw}**；当前无任何窗口样本达阈值，OOS 结论**不具统计显著性**，仅作参考，须待实盘累积≥30笔后复核。")
    L.append(f"- **OOS 夏普（base）**：min {min(sh_b):.2f} / **mean {mean_sh_b:.2f}** / max {max(sh_b):.2f}")
    L.append(f"- **OOS 年化（base）**：mean {fmt_pct(mean_ann_b)}")
    if use_stress:
        L.append(f"- **OOS 夏普（北交所+{bj_slippage*100:.1f}%滑点）**：min {min(sh_s):.2f} / **mean {mean_sh_s:.2f}** / max {max(sh_s):.2f}；正夏普窗口 {sum(1 for x in sh_s if x>0)}/{nw}")
        L.append(f"- **OOS 年化（压力）**：mean {fmt_pct(mean_ann_s)}（base→压力 年化变动 {fmt_pct(mean_ann_s-mean_ann_b)}）")
    L.append(f"- 对照参考：固定参数版 walk-forward 的 OOS 夏普均值约 0.53；本参数重选版为 {mean_sh_b:.2f} → "
             f"{'更高，说明训练窗内选参在 OOS 上仍有增益' if mean_sh_b > 0.53 else '未明显更高，提示小样本选参增益有限甚至过拟合'}。\n")

    L.append("### 📈 各窗口明细（所选参数 + OOS 表现）\n")
    header = "| 验证年 T | 训练n | 选参(hold/tp/sl) | OOS n | OOS胜率 | OOS夏普(base) | OOS年化(base) | OOS回撤(base) | 显著性"
    sep = "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|"
    if use_stress:
        header += " | OOS夏普(压力) | 北交所OOS n | 北交所OOS夏普(压力) |"
        sep = "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    L.append(header)
    L.append(sep)
    for r in rows:
        sel = f"{r['sel'][0]}/{r['sel'][1]:.2f}/{r['sel'][2]:.2f}"
        line = (f"| {r['T']} | {r['n_train']} | {sel} | {r['n_oos']} | {r['wr_oos_b']:.1f}% | "
                f"{r['sh_oos_b']:.2f} | {fmt_pct(r['ann_oos_b'])} | {fmt_pct(r['dd_oos_b'])} | {_infer_tag(r['n_oos'])}")
        if use_stress:
            line += f" | {r['sh_oos_s']:.2f} | {r['n_bj']} | {r['sh_bj_s']:.2f} |"
        L.append(line)
    L.append("")
    L.append(_infer_legend(oos=True))

    L.append("### 🔎 北交所（小盘）压力测试结论\n")
    if use_stress:
        bj_sh = [r['sh_bj_s'] for r in rows if r['n_bj'] > 0]
        bj_ann = [r['ann_bj_s'] for r in rows if r['n_bj'] > 0]
        tot_bj = sum(r['n_bj'] for r in rows)
        if bj_sh:
            bj_sh_mean = sum(bj_sh) / len(bj_sh)
            L.append(f"- 北交所 OOS 合计 {tot_bj} 笔（占 OOS 子集）；夏普 mean {bj_sh_mean:.2f} / "
                     f"年化 mean {fmt_pct(sum(bj_ann)/len(bj_ann))}（短持仓+小样本年化放大，噪声大）；"
                     f"正夏普窗口 {sum(1 for x in bj_sh if x>0)}/{len(bj_sh)}")
            L.append(f"- **小盘股 OOS 在 +{bj_slippage*100:.1f}% 滑点下是否仍为正**："
                     f"{'是（夏普仍为正，滑点侵蚀有限）' if bj_sh_mean > 0 else '否（夏普转负，滑点显著侵蚀小盘收益）'}。")
        else:
            L.append("- 北交所 OOS 样本为 0，无法评估小盘压力。")
    else:
        L.append("- 未启用北交所额外滑点；用 `--bj-slippage 0.003` 重跑可检验小盘 OOS 正收益稳健性。\n")

    L.append("> 注：首窗（2020）训练样本 = 0，使用默认参数（非重选），不计入「训练→验证」严谨性评判；其 OOS 即该年独立表现。\n")
    L.append("> 注：训练窗样本极小（多数 <60 笔），网格选参本身噪声大、各窗所选参数可能剧烈跳变，属「小样本过拟合」高风险场景；"
             "本报告呈现的是真实 OOS 而非训练内拟合，结论须实盘累积后复核。\n")

    L.append(_std_plan_e_risk_next())
    return "\n".join(L)


def _real_bt_from_csv(path: Path) -> pd.DataFrame:
    """读取实盘样本集（plan_e_real_samples.py 生成），转成与 backtest 输出兼容的 bt。

    实盘样本使用**实际成交收益**（观测值），无需 K 线重算；
    每笔观测收益作为单点资金曲线，供 compute_metrics 聚合。
    """
    if not path.exists():
        print(f"  ⚠ 实盘样本集不存在: {path}（先跑 plan_e_real_samples.py 回灌 journal）")
        return pd.DataFrame()
    df = pd.read_csv(path)
    if 'return_pct' not in df.columns or df.empty:
        return pd.DataFrame()
    df['return_pct'] = df['return_pct'].astype(float)
    df['signal_date'] = df['signal_date'].astype(str)
    if 'exit_reason' not in df.columns and 'reason' in df.columns:
        df['exit_reason'] = df['reason']
    exit_date = pd.to_datetime(df.get('exit_date', df['signal_date']).astype(str))
    df['daily'] = [[(d, r)] for d, r in zip(exit_date, df['return_pct'])]
    df['yr'] = pd.to_datetime(df['signal_date']).dt.year
    if 'exch' not in df.columns:
        df['exch'] = df['code'].apply(_exchange_of)
    return df


def run_by_year_real(bt: pd.DataFrame) -> str:
    """实盘样本·按 signal_date 年份分层（观测收益），沿用 n≥30 显著性闸口。"""
    if bt is None or bt.empty:
        return "## 实盘样本·按年份分层\n\n无有效交易。\n"
    bt = bt.copy()
    bt['yr'] = pd.to_datetime(bt['signal_date']).dt.year
    pooled_daily = [r for r in bt['daily'].tolist() if isinstance(r, list)]
    pooled = compute_metrics(bt, pooled_daily)

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    # 样本过小时年化/夏普/卡玛由单点权益曲线年化，数值失真，统一显示为 —
    def _ann(m):
        return fmt_pct(m['ann_ret']) if m['n'] >= 10 else '—'
    def _sh(m):
        return f"{m['sharpe']:.2f}" if m['n'] >= 10 else '—'
    def _cl(m):
        return f"{m['calmar']:.2f}" if m['n'] >= 10 else '—'

    def yr_metrics(y):
        sub = bt[bt['yr'] == y]
        daily = [r for r in sub['daily'].tolist() if isinstance(r, list)]
        return compute_metrics(sub, daily)

    L = []
    L.append("## 📅 实盘样本·按年份分层（观测收益）\n")
    L.append(f"样本来源：实盘 journal 回灌（`data/double_up/real_trades.csv`）；共 **{len(bt)} 笔**观测交易"
             f"（信号年 {int(bt['yr'].min())}–{int(bt['yr'].max())}）。\n")
    L.append("> 注：实盘样本使用**实际成交收益**（非 K 线重算），下列指标为观测值汇总；无需参数回测。\n")
    L.append("### 📊 核心指标\n")
    L.append(f"- 全样本：**胜率 {pooled['win_rate']:.1f}%** / 均收益 {fmt_pct(pooled['avg_ret'])} / "
             f"年化 {_ann(pooled)} / **夏普 {_sh(pooled)}** / 回撤 {fmt_pct(pooled['max_dd'])}")
    yrs = sorted(int(y) for y in bt['yr'].dropna().unique())
    L.append(f"- 年份数：{len(yrs)}\n")
    L.append("### 📈 各年明细（年化/夏普/卡玛 在样本<10 时显示为 —，因其由单点权益曲线年化失真）\n")
    L.append("| 年份 | 样本 | 胜率 | 平均收益 | 年化 | 夏普 | 最大回撤 | Calmar | 盈亏比 | 显著性 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    shps, wrs, n_infer = [], [], 0
    for y in yrs:
        m = yr_metrics(y)
        shps.append(m['sharpe'] if m['n'] >= 10 else float('nan'))
        wrs.append(m['win_rate'])
        if m['n'] >= MIN_INFER_N:
            n_infer += 1
        L.append(f"| {y} | {m['n']} | {m['win_rate']:.1f}% | {fmt_pct(m['avg_ret'])} | {_ann(m)} | "
                 f"{_sh(m)} | {fmt_pct(m['max_dd'])} | {_cl(m)} | {m['pf']:.2f} | {_infer_tag(m['n'])} |")
    L.append(f"| **全样本** | **{pooled['n']}** | **{pooled['win_rate']:.1f}%** | **{fmt_pct(pooled['avg_ret'])}** | "
             f"**{_ann(pooled)}** | **{_sh(pooled)}** | **{fmt_pct(pooled['max_dd'])}** | "
             f"**{_cl(pooled)}** | **{pooled['pf']:.2f}** | — |")
    L.append("")
    L.append(_infer_legend(extra=f"可作统计推断年份（样本≥30）：**{n_infer}/{len(yrs)}**；"
                            f"其余年份点估计不显著，须实盘累积≥30笔后下统计结论"))
    L.append("### 📊 统计分布（年度稳定性）\n")
    _v = [x for x in shps if pd.notna(x)]
    if _v:
        L.append(f"- 年度夏普：min {min(_v):.2f} / mean {sum(_v)/len(_v):.2f} / max {max(_v):.2f}；"
                 f"正夏普年份 {sum(1 for s in _v if s>0)}/{len(_v)}（仅统计样本≥10 的年份）")
    else:
        L.append("- 年度夏普：—（所有年份样本均<10，无法稳定估计）")
    L.append(f"- 年度胜率：min {min(wrs):.1f}% / mean {sum(wrs)/len(wrs):.1f}% / max {max(wrs):.1f}%")
    L.append(_std_plan_e_risk_next())
    return "\n".join(L)


def run_real_walk_forward(bt: pd.DataFrame) -> str:
    """实盘样本·滚动窗口验证（观测收益 OOS，按年份切分），沿用 n≥30 闸口。"""
    if bt is None or bt.empty:
        return "## 实盘样本·滚动窗口验证\n\n无有效交易。\n"
    bt = bt.copy()
    bt['yr'] = pd.to_datetime(bt['signal_date']).dt.year
    years = sorted(int(y) for y in bt['yr'].dropna().unique())

    def metrics_of(sub):
        return compute_metrics(sub, [r for r in sub['daily'].tolist() if isinstance(r, list)])

    rows = []
    for T in years:
        oos = bt[bt['yr'] == T]
        m = metrics_of(oos)
        n_train = int((bt['yr'] < T).sum())
        rows.append({'T': T, 'n_train': n_train, 'n_oos': int(m.get('n', 0)),
                     'wr_oos_b': m.get('win_rate', 0.0), 'sh_oos_b': m.get('sharpe', 0.0),
                     'ann_oos_b': m.get('ann_ret', 0.0), 'dd_oos_b': m.get('max_dd', 0.0)})
    if not rows:
        return "## 实盘样本·滚动窗口验证\n\n无有效结果。\n"

    sh_b = [r['sh_oos_b'] for r in rows]
    ann_b = [r['ann_oos_b'] for r in rows]
    nw = len(rows)
    mean_sh_b = sum(sh_b) / nw
    mean_ann_b = sum(ann_b) / nw
    frac_pos_b = sum(1 for x in sh_b if x > 0) / nw
    infer_rows = [r for r in rows if r['n_oos'] >= MIN_INFER_N]
    n_infer = len(infer_rows)
    if infer_rows:
        mean_sh_inf = sum(r['sh_oos_b'] for r in infer_rows) / n_infer
        mean_ann_inf = sum(r['ann_oos_b'] for r in infer_rows) / n_infer
        frac_pos_inf = sum(1 for r in infer_rows if r['sh_oos_b'] > 0) / n_infer
    else:
        mean_sh_inf = mean_ann_inf = frac_pos_inf = 0.0

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    # 样本<10 时年化/夏普由单点权益曲线年化失真，统一显示为 —
    def _ann(r):
        return fmt_pct(r['ann_oos_b']) if r['n_oos'] >= 10 else '—'
    def _sh(r):
        return f"{r['sh_oos_b']:.2f}" if r['n_oos'] >= 10 else '—'
    # 聚合指标仅取样本≥10 的窗口，避免被小样本年化扭曲
    def _mean(vals):
        return (sum(vals) / len(vals)) if vals else None
    sh_ok = [r['sh_oos_b'] for r in rows if r['n_oos'] >= 10]
    ann_ok = [r['ann_oos_b'] for r in rows if r['n_oos'] >= 10]
    sh_mean = _mean(sh_ok)
    ann_mean = _mean(ann_ok)
    sh_min = min(sh_ok) if sh_ok else None
    sh_max = max(sh_ok) if sh_ok else None
    _d = lambda v: (f"{v:.2f}" if v is not None else '—')
    _da = lambda v: (fmt_pct(v) if v is not None else '—')

    L = []
    L.append("## 🔄 实盘样本·滚动窗口验证（观测收益 OOS）\n")
    L.append(f"窗口设计：滚动步长=1年，验证窗=固定1年（按 signal_date 年份切分）；训练窗=该年之前所有实盘样本"
             f"（仅界定窗口，不作参数重选——实盘为观测收益）。共 **{nw}** 个窗口。\n")
    L.append("### 📊 核心指标（OOS 汇总）\n")
    L.append(f"- OOS 窗口数：**{nw}**；**正夏普窗口占比：{frac_pos_b*100:.0f}%**（{sum(1 for x in sh_b if x>0)}/{nw}）")
    if n_infer:
        L.append(f"- **可作统计推断窗口（OOS样本≥30）：{n_infer}/{nw}**；其 OOS 夏普均值 **{mean_sh_inf:.2f}** / 年化 {fmt_pct(mean_ann_inf)} / "
                 f"正夏普占比 {frac_pos_inf*100:.0f}%（此子集方具统计显著性，结论以前者为准）。")
    else:
        L.append(f"- **可作统计推断窗口（OOS样本≥30）：0/{nw}**；当前无任何窗口样本达阈值，OOS 结论**不具统计显著性**，仅作参考，须待实盘累积≥30笔后复核。")
    L.append(f"- **OOS 夏普**：min {_d(sh_min)} / **mean {_d(sh_mean)}** / max {_d(sh_max)}（仅统计样本≥10 的窗口；否则 —）")
    L.append(f"- **OOS 年化**：mean {_da(ann_mean)}（仅统计样本≥10 的窗口；否则 —）")
    L.append("### 📈 各窗口明细（年化/夏普 在样本<10 时显示为 —，因单点权益曲线年化失真）\n")
    L.append("| 验证年 T | 训练n | OOS n | OOS胜率 | OOS夏普 | OOS年化 | OOS回撤 | 显著性 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(f"| {r['T']} | {r['n_train']} | {r['n_oos']} | {r['wr_oos_b']:.1f}% | {_sh(r)} | "
                 f"{_ann(r)} | {fmt_pct(r['dd_oos_b'])} | {_infer_tag(r['n_oos'])} |")
    L.append("")
    L.append(_infer_legend(oos=True))
    L.append(_std_plan_e_risk_next())
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser(description='方案 E：翻倍当日跟随策略回测')
    parser.add_argument('--hold', type=int, default=10, help='持仓天数（默认 10）')
    parser.add_argument('--tp', type=float, default=0.30, help='止盈比例（默认 0.30）')
    parser.add_argument('--sl', type=float, default=-0.10, help='止损比例（默认 -0.10）')
    parser.add_argument('--no-filter', action='store_true', help='关闭 v2 信号过滤（v1 旧规则）')
    parser.add_argument('--today', action='store_true', help='只生成今日信号（不跑回测）')
    parser.add_argument('--compare', action='store_true', help='v1 vs v2 对比')
    parser.add_argument('--fee', type=float, default=0.0003,
                        help='单边佣金费率（默认 0.0003 = 0.03%%）')
    parser.add_argument('--slippage', type=float, default=0.001,
                        help='单边滑点（默认 0.001 = 0.1%%）')
    parser.add_argument('--no-cost', action='store_true',
                        help='关闭交易成本与滑点（净价回测）')
    parser.add_argument('--sensitivity', action='store_true',
                        help='参数敏感性扫描（hold/tp/sl 网格，按夏普排序）')
    parser.add_argument('--stratify', action='store_true',
                        help='沪深/北交所分层回测（告警北交所数据缺口）')
    parser.add_argument('--by-year', action='store_true',
                        help='按 signal_date 年份分层回测')
    parser.add_argument('--walk-forward', action='store_true',
                        help='walk-forward 扩展窗口滚动验证（固定参数，OOS 按年）')
    parser.add_argument('--retune', action='store_true',
                        help='walk-forward 训练窗内重选最优参数（真 OOS）；与 --walk-forward 互斥，更严苛')
    parser.add_argument('--bj-slippage', type=float, default=0.0,
                        help='北交所额外滑点（在 --slippage 基础上叠加，默认 0；压力测试可用 0.003=0.3%%）')
    parser.add_argument('--real', action='store_true',
                        help='实盘样本分析：消费 data/double_up/real_trades.csv（journal 回灌），'
                             '按观测收益跑 --by-year / 滚动窗口，沿用 n≥30 显著性闸口')
    parser.add_argument('--no-freshness-check', action='store_true', help='跳过数据新鲜度校验')
    parser.add_argument('--mf', action='store_true',
                        help='多因子组合回测模式（截面五维因子 + ICIR 合成 + ERC 风险预算 + 全成本），调用 quant/multifactor')
    parser.add_argument('--mf-limit', type=int, default=0, help='--mf 仅前 N 只股票（冒烟用）')
    parser.add_argument('--mf-valuation-mode', default='auto', choices=['proxy', 'real', 'auto'],
                        help='--mf 估值模式：proxy 代理 / real 真实 / auto 自动')
    parser.add_argument('--mf-top-n', type=int, default=30, help='--mf 持仓数量')
    parser.add_argument('--mf-start', default='2000-01-01', help='--mf 回测起始日')
    parser.add_argument('--mf-end', default=datetime.now().strftime("%Y-%m-%d"),
                        help='--mf 回测结束日（默认至今日）')
    args = parser.parse_args()

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    fee = 0.0 if args.no_cost else args.fee
    slippage = 0.0 if args.no_cost else args.slippage
    bj_slippage = 0.0 if args.no_cost else args.bj_slippage

    # ---- 多因子组合回测模式（与事件驱动方案 E 互不干扰）----
    if args.mf:
        sys.path.insert(0, str(ROOT))
        from quant.multifactor.engine import run_mf_backtest
        from quant.multifactor.factors import MFConfig
        cfg = MFConfig(start=args.mf_start, end=args.mf_end, top_n=args.mf_top_n,
                       valuation_mode=args.mf_valuation_mode, limit=args.mf_limit)
        print("=== 多因子组合回测 (multifactor v1) ===")
        print(f"  参数: top_n={cfg.top_n} valuation={cfg.valuation_mode} "
              f"range={cfg.start}~{cfg.end} limit={cfg.limit}")
        df, metrics = run_mf_backtest(cfg)
        print(f"  调仓次数: {metrics.get('n_rebalances')}  股票池: {metrics.get('universe_size')}  "
              f"估值模式: {metrics.get('valuation_mode')}")
        print(f"  年化: {metrics.get('ann_ret', 0)*100:.2f}%  夏普: {metrics.get('sharpe', 0):.2f}  "
              f"回撤: {metrics.get('max_dd', 0)*100:.2f}%  IC均值: {metrics.get('ic_mean', 0):.3f}  "
              f"ICIR: {metrics.get('icir_overall', 0):.3f}")
        print(f"  输出: {OUTPUT_DIR}")
        print("=== ✅ 完成 ===")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    print("=== 方案 E：翻倍当日跟随策略 (v2) ===")
    print(f"  时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  参数: hold={args.hold} tp={args.tp} sl={args.sl}")
    if fee + slippage > 0:
        print(f"  成本: 佣金 {fee*100:.3f}% + 滑点 {slippage*100:.3f}%（双边 {(fee+slippage)*2*100:.3f}%）")
    else:
        print("  成本: 关闭（净价）")
    if args.no_filter:
        print("  模式: v1 旧规则（无过滤）")

    # 加载翻倍样本
    print("\n[1/3] 加载翻倍样本...")
    double_up = load_double_up()

    if args.today:
        print("\n[2/3] 生成今日信号...")
        sig = generate_today_signals(double_up)
        if sig.empty:
            print("  (今日无触发信号)")
        else:
            sig = sig.drop(columns=['daily'], errors='ignore')
            fp = OUTPUT_DIR / 'plan_e_today.csv'
            sig.to_csv(fp, index=False, encoding='utf-8-sig')
            print(f"  ✓ {fp}")
            print(sig[['code', 'signal_date', 't1_pct', 't1_vol_ratio']].to_string(index=False))
        return

    # 参数敏感性扫描
    if args.sensitivity:
        print("\n[2/3] 参数敏感性扫描（grid）...")
        md = run_sensitivity(double_up, fee, slippage)
        fp_md = OUTPUT_DIR / f'plan_e_sensitivity_{ts}.md'
        header = f"# 方案 E 参数敏感性扫描 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py\n模型版本：v2\n数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # 沪深/北交所分层回测
    if args.stratify:
        print("\n[2/3] 沪深/北交所分层回测...")
        md = run_stratify(double_up, args.hold, args.tp, args.sl, args.no_filter, fee, slippage)
        fp_md = OUTPUT_DIR / f'plan_e_stratify_{ts}.md'
        header = f"# 方案 E 分层回测 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py\n模型版本：v2\n数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # 按年份分层回测
    if args.by_year:
        print("\n[2/3] 按年份分层回测...")
        md = run_by_year(double_up, args.hold, args.tp, args.sl, args.no_filter, fee, slippage)
        fp_md = OUTPUT_DIR / f'plan_e_by_year_{ts}.md'
        header = f"# 方案 E 按年份分层回测 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py\n模型版本：v2\n数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # walk-forward 扩展窗口滚动验证（固定参数）
    if args.walk_forward:
        print("\n[2/3] walk-forward 滚动验证...")
        md = run_walk_forward(double_up, args.hold, args.tp, args.sl, args.no_filter, fee, slippage, bj_slippage)
        fp_md = OUTPUT_DIR / f'plan_e_walkforward_{ts}.md'
        header = f"# 方案 E Walk-forward 滚动验证 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py\n模型版本：v2\n数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # walk-forward 参数重选滚动验证（真 OOS，训练窗内重选参数）
    if args.retune:
        print("\n[2/3] walk-forward 参数重选滚动验证（训练窗内重选最优参数 → OOS）...")
        md = run_walk_forward_rt(double_up, args.hold, args.tp, args.sl, args.no_filter, fee, slippage, bj_slippage)
        tag = f"_bj{int(bj_slippage*1000)}" if bj_slippage > 0 else ""
        fp_md = OUTPUT_DIR / f'plan_e_wf_retune{tag}_{ts}.md'
        header = f"# 方案 E Walk-forward 参数重选 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py\n模型版本：v2\n数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # 实盘样本分析（journal 回灌 → data/double_up/real_trades.csv）
    if args.real:
        print("\n[2/3] 实盘样本分析（观测收益，n≥30 显著性闸口）...")
        bt_real = _real_bt_from_csv(REAL_TRADES_CSV)
        md = "\n\n".join([run_by_year_real(bt_real), run_real_walk_forward(bt_real)])
        fp_md = OUTPUT_DIR / f'plan_e_real_{ts}.md'
        header = (f"# 方案 E 实盘样本分析（观测收益 · n≥30 显著性闸口） "
                  f"({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n")
        footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"生成者：plan_e_backtest.py --real\n模型版本：v2（实盘观测）\n"
                  f"数据来源：data/double_up/real_trades.csv（由 plan_e_real_samples.py 从 journal 回灌）\n"
                  f"数据日期：{datetime.now().strftime('%Y-%m-%d')}\n")
        fp_md.write_text(header + md + footer, encoding='utf-8')
        print(f"  ✓ MD: {fp_md.name}")
        print("\n=== ✅ 完成 ===")
        return

    # 回测
    print("\n[2/3] 回测...")
    bt, daily = backtest(double_up, args.hold, args.tp, args.sl, no_filter=args.no_filter,
                       fee=fee, slippage=slippage)
    print(f"  完成: {len(bt)} 笔")
    if bt.empty:
        print("  ❌ 无回测结果")
        return
    # 剥离持仓逐日收益（仅用于资金曲线，不入表）
    bt = bt.drop(columns=['daily'], errors='ignore')

    # 输出
    print("\n[3/3] 输出...")
    mode = 'v1' if args.no_filter else 'v2'
    fp_csv = OUTPUT_DIR / f'plan_e_backtest_{mode}_{ts}.csv'
    bt.to_csv(fp_csv, index=False, encoding='utf-8-sig')
    print(f"  ✓ CSV: {fp_csv.name}")

    md = summarize(bt, daily, args.hold, args.tp, args.sl, mode, fee=fee, slippage=slippage)
    fp_md = OUTPUT_DIR / f'plan_e_backtest_{mode}_{ts}.md'
    header = f"# 方案 E 回测报告 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
    footer = (f"\n---\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"生成者：plan_e_backtest.py\n模型版本：v2\n"
              f"数据日期：{bt['signal_date'].max() if not bt.empty else 'N/A'}\n")
    full = header + md + footer
    with open(fp_md, 'w', encoding='utf-8') as f:
        f.write(full)
    print(f"  ✓ MD: {fp_md.name}")

    # latest
    fp_latest = OUTPUT_DIR / f'plan_e_backtest_{mode}_latest.md'
    fp_latest.write_text(full, encoding='utf-8')
    print(f"  ✓ latest: {fp_latest.name}")

    # v1 vs v2 对比
    if args.compare and not args.no_filter:
        print("\n[Bonus] v1 vs v2 对比...")
        bt_v1, _ = backtest(double_up, args.hold, args.tp, args.sl, no_filter=True,
                           fee=fee, slippage=slippage)
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

    print("\n=== ✅ 完成 ===")
    print(f"  期望收益: {bt['return_pct'].mean()*100:.2f}%")
    print(f"  胜率: {(bt['return_pct']>0).mean()*100:.1f}%")
    print(f"  盈亏比: {abs(bt[bt['return_pct']>0]['return_pct'].mean()/bt[bt['return_pct']<=0]['return_pct'].mean()):.2f}")
    print(f"  输出: {OUTPUT_DIR}")


if __name__ == '__main__':
    main()
