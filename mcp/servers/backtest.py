#!/usr/bin/env python3
"""
mcp/servers/backtest.py — MCP Server: 单标的技术策略回测
=========================================================

工具：
    1. list_strategies       列出内置策略与默认参数
    2. run_backtest          按内置策略回测（前复权、T+1 开盘成交）
    3. run_signal_backtest   按显式信号日期回测（可配止盈/止损/持有天数）
    4. daban_top_picks       全市场打板选股：daban 模型打分，输出 T+1 涨停概率 Top-N

数据源：data/parquet/daily/{symbol}.parquet + data/adj_factor/{six}.parquet
        模型 data/double_up/d_signal_5d_model_daban.txt（29 维，次日收盘涨停二分类）
协议：MCP 2024-11-05（JSON-RPC over stdio）

口径：
    - 信号在收盘后确认，下一交易日开盘成交（无未来函数）
    - 买入 T+1 才可卖出（止盈止损从入场次日开始检查）
    - 默认成本：双边佣金 0.025%、卖出印花税 0.05%、双边滑点 0.05%
    - 单标的满仓复利；不含涨跌停无法成交、停牌、分红税等微观约束
"""

import json
import logging
import math
import os
import re
import sys
import time
from bisect import bisect_left
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = ROOT / "logs" / "mcp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:backtest] %(message)s",
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import (  # noqa: E402
    adj_factor_dir,
    daily_dir,
    model_path,
    universe_path,
)

DABAN_MODEL = model_path("d_signal_5d_model_daban.txt")
UNIVERSE_FILE = universe_path()
DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()

from tdxfeed.daban.limit_price import (  # noqa: E402
    is_st as _is_st_name,
    limit_ratio,
    limit_up_price,
)
from tdxfeed.execution import single as _single  # noqa: E402
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features_extended,
    get_limit,
    is_open_limit_up,
)

DEFAULT_FEE_BPS = 2.5
DEFAULT_STAMP_BPS = 5.0
DEFAULT_SLIP_BPS = 5.0
TRADING_DAYS = 243

STRATEGIES = {
    "buy_hold": {
        "description": "买入并持有（基准）",
        "defaults": {},
    },
    "ma_cross": {
        "description": "双均线：快线上穿慢线买入，下穿卖出",
        "defaults": {"fast": 5, "slow": 20, "cooldown": 0},
    },
    "macd_cross": {
        "description": "MACD：DIF 上穿 DEA 买入，下穿卖出",
        "defaults": {"fast": 12, "slow": 26, "signal": 9, "cooldown": 0},
    },
    "breakout": {
        "description": "唐奇安突破：收盘突破前 N 日高点买入，跌破前 M 日低点卖出",
        "defaults": {"lookback": 20, "exit_lookback": 10, "cooldown": 5},
    },
    "rsi_reversion": {
        "description": "RSI 均值回归：超卖买入，超买卖出，最长持有 max_hold 日",
        "defaults": {"n": 14, "oversold": 30, "overbought": 70,
                     "max_hold": 20, "cooldown": 0},
    },
}


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# === 参数与数据 ============================================================
def _norm_code(code: str) -> tuple[str, str]:
    s = str(code or "").strip().lower().replace(".", "")
    if re.fullmatch(r"(sh|sz)\d{6}", s):
        sym, six = s, s[2:]
    elif re.fullmatch(r"\d{6}", s):
        six = s
        if six.startswith("6"):
            sym = "sh" + six
        elif six.startswith(("0", "3")):
            sym = "sz" + six
        else:
            raise McpError(-32602, f"仅支持沪深 A 股代码，收到：{code}")
    else:
        raise McpError(-32602, f"非法代码：{code}（示例 sh600000 / 600000）")
    if not (DAILY_DIR / f"{sym}.parquet").exists():
        raise McpError(404, f"{sym} 无日 K 数据")
    return sym, six


def _norm_date(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        return s
    try:
        ts = pd.to_datetime(s, errors="coerce")
        return None if pd.isna(ts) else ts.strftime("%Y%m%d")
    except Exception:
        return None


def _date_series(s: pd.Series) -> pd.Series:
    """兼容 date 列的 int64(YYYYMMDD) / string / datetime 三种 schema。"""
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype(str).str.zfill(8), format="%Y%m%d",
                              errors="coerce").dt.strftime("%Y%m%d")
    return pd.to_datetime(s, errors="coerce").dt.strftime("%Y%m%d")


def _load_prices(sym: str, six: str) -> tuple[pd.DataFrame, bool, list[str]]:
    warnings_out: list[str] = []
    df = pd.read_parquet(DAILY_DIR / f"{sym}.parquet")
    if df is None or df.empty:
        raise McpError(404, f"{sym} 日 K 为空")
    need = {"date", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise McpError(-32603, f"{sym} 日 K 缺列：{need - set(df.columns)}")
    df = df.copy()
    df["date"] = _date_series(df["date"])
    df = df.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["close"] > 0) & (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0)]
    df = df.reset_index(drop=True)

    adjusted = False
    factor = pd.Series(1.0, index=df.index)
    adj_path = ADJ_DIR / f"{six}.parquet"
    if adj_path.exists():
        try:
            adj = pd.read_parquet(adj_path)
            if "date" in adj.columns:
                adj["date"] = _date_series(adj["date"])
                if "qfq_close" in adj.columns and "raw_close" in adj.columns:
                    adj["factor"] = (pd.to_numeric(adj["qfq_close"], errors="coerce") /
                                     pd.to_numeric(adj["raw_close"], errors="coerce"))
                elif "adj_factor" in adj.columns:
                    adj["factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
                else:
                    adj["factor"] = np.nan
                adj.loc[~np.isfinite(adj["factor"]) | (adj["factor"] <= 0), "factor"] = np.nan
                merged = df[["date"]].merge(adj[["date", "factor"]], on="date", how="left")
                factor = merged["factor"].ffill().bfill()
                if factor.notna().any():
                    adjusted = True
                    warnings_out.append("使用 data/adj_factor 前复权因子调整 OHLC")
        except Exception as e:
            warnings_out.append(f"复权因子读取失败，使用未复权价：{e}")
    factor = factor.fillna(1.0).to_numpy(dtype=float)
    for c in ("open", "high", "low", "close"):
        df[f"{c}_adj"] = df[c].to_numpy(dtype=float) * factor
    if not adjusted:
        warnings_out.append("未找到有效复权因子，回测使用未复权价（分红送转会污染收益）")
    return df, adjusted, warnings_out


def _pint(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(params.get(key, default))
    except (TypeError, ValueError):
        raise McpError(-32602, f"参数 {key} 必须是整数") from None
    if not lo <= v <= hi:
        raise McpError(-32602, f"参数 {key} 超出范围 [{lo}, {hi}]：{v}")
    return v


def _pfloat(params: dict, key: str, default: float | None,
            lo: float, hi: float) -> float | None:
    if params.get(key) is None:
        return default
    try:
        v = float(params[key])
    except (TypeError, ValueError):
        raise McpError(-32602, f"参数 {key} 必须是数字") from None
    if not lo <= v <= hi:
        raise McpError(-32602, f"参数 {key} 超出范围 [{lo}, {hi}]：{v}")
    return v


def _effective_params(strategy: str, raw: dict | None) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise McpError(-32602, "params 必须是对象")
    p = dict(STRATEGIES[strategy]["defaults"])
    p.update({k: v for k, v in raw.items() if v is not None})
    p["cooldown"] = _pint(p, "cooldown", 0, 0, 60)
    p["max_hold"] = _pint(p, "max_hold", p.get("max_hold", 0) or 0, 0, 250) or None
    p["take_profit"] = _pfloat(p, "take_profit", None, 0.001, 5.0)
    p["stop_loss"] = _pfloat(p, "stop_loss", None, 0.001, 0.95)
    if strategy == "ma_cross":
        p["fast"] = _pint(p, "fast", 5, 2, 120)
        p["slow"] = _pint(p, "slow", 20, 3, 250)
        if p["fast"] >= p["slow"]:
            raise McpError(-32602, "ma_cross 要求 fast < slow")
    elif strategy == "macd_cross":
        p["fast"] = _pint(p, "fast", 12, 2, 120)
        p["slow"] = _pint(p, "slow", 26, 3, 250)
        p["signal"] = _pint(p, "signal", 9, 2, 60)
        if p["fast"] >= p["slow"]:
            raise McpError(-32602, "macd_cross 要求 fast < slow")
    elif strategy == "breakout":
        p["lookback"] = _pint(p, "lookback", 20, 2, 250)
        p["exit_lookback"] = _pint(p, "exit_lookback", 10, 2, 250)
    elif strategy == "rsi_reversion":
        p["n"] = _pint(p, "n", 14, 2, 120)
        p["oversold"] = _pfloat(p, "oversold", 30, 1, 49)
        p["overbought"] = _pfloat(p, "overbought", 70, 51, 99)
        if p["oversold"] >= p["overbought"]:
            raise McpError(-32602, "rsi_reversion 要求 oversold < overbought")
    return p


# === 指标与信号 ============================================================
def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(s: pd.Series, n: int) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & (gain > 0)] = 100.0
    out[(loss == 0) & (gain == 0)] = 50.0
    return out


def _signals(df: pd.DataFrame, strategy: str, p: dict,
             start_idx: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(df)
    entry = np.zeros(n, dtype=bool)
    exit_ = np.zeros(n, dtype=bool)
    close = df["close_adj"]

    if strategy == "buy_hold":
        entry[start_idx] = True
        return entry, exit_

    if strategy == "ma_cross":
        fast, slow = _sma(close, p["fast"]), _sma(close, p["slow"])
        state = (fast > slow) & fast.notna() & slow.notna()
        state = state.fillna(False).to_numpy(dtype=bool)
        entry = state & ~np.r_[False, state[:-1]]
        exit_ = ~state & np.r_[False, state[:-1]]
        return entry, exit_

    if strategy == "macd_cross":
        dif = _ema(close, p["fast"]) - _ema(close, p["slow"])
        dea = _ema(dif, p["signal"])
        state = (dif > dea) & dif.notna() & dea.notna()
        state = state.fillna(False).to_numpy(dtype=bool)
        entry = state & ~np.r_[False, state[:-1]]
        exit_ = ~state & np.r_[False, state[:-1]]
        return entry, exit_

    if strategy == "breakout":
        prior_high = df["high_adj"].shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
        prior_low = df["low_adj"].shift(1).rolling(p["exit_lookback"],
                                                    min_periods=p["exit_lookback"]).min()
        entry = (close > prior_high).fillna(False).to_numpy(dtype=bool)
        exit_ = (close < prior_low).fillna(False).to_numpy(dtype=bool)
        return entry, exit_

    if strategy == "rsi_reversion":
        rsi = _rsi(close, p["n"])
        entry = (rsi < p["oversold"]).fillna(False).to_numpy(dtype=bool)
        exit_ = (rsi > p["overbought"]).fillna(False).to_numpy(dtype=bool)
        return entry, exit_

    raise McpError(-32602, f"未知策略：{strategy}")


# === 回测核心 ==============================================================
def _simulate(df: pd.DataFrame, entry_signal: np.ndarray, exit_signal: np.ndarray,
              start_idx: int, end_idx: int, *, fee_bps: float, stamp_bps: float,
              slip_bps: float, take_profit: float | None, stop_loss: float | None,
              max_hold: int | None, cooldown: int) -> dict:
    fee, stamp, slip = fee_bps / 10000, stamp_bps / 10000, slip_bps / 10000
    cash, shares = 1.0, 0.0
    in_pos, pending_entry, pending_exit = False, False, False
    entry_idx, entry_gross, trade_start_equity = 0, 0.0, 1.0
    cooldown_until = 0
    trades: list[dict] = []
    equity_curve: list[dict] = []
    fee_total = 0.0
    position_days = 0
    ignored_entries = 0

    def sell(i: int, raw_price: float, reason: str) -> None:
        nonlocal cash, shares, in_pos, pending_exit, fee_total, cooldown_until
        if not in_pos or shares <= 0:
            return
        exec_price = raw_price * (1 - slip)
        proceeds = shares * exec_price
        cost = proceeds * fee + proceeds * stamp
        cash = proceeds - cost
        fee_total += cost
        shares = 0.0
        in_pos = False
        pending_exit = False
        cooldown_until = i + cooldown
        net = cash / trade_start_equity - 1
        gross = raw_price / entry_gross - 1 if entry_gross > 0 else 0.0
        trades.append({
            "entry_date": df.at[entry_idx, "date"],
            "exit_date": df.at[i, "date"],
            "entry_price": round(entry_gross, 4),
            "exit_price": round(raw_price, 4),
            "hold_days": int(i - entry_idx),
            "return_gross": round(gross, 6),
            "return_net": round(net, 6),
            "exit_reason": reason,
        })

    for i in range(start_idx, end_idx + 1):
        open_p = float(df.at[i, "open_adj"])
        close_p = float(df.at[i, "close_adj"])

        # 1) 开盘执行上一交易日收盘产生的委托
        if pending_entry:
            pending_entry = False
            if not in_pos and open_p > 0:
                exec_price = open_p * (1 + slip)
                buy_fee = cash * fee
                fee_total += buy_fee
                trade_start_equity = cash
                shares = (cash - buy_fee) / exec_price
                cash = 0.0
                in_pos = True
                entry_idx = i
                entry_gross = open_p
        if pending_exit and in_pos:
            pending_exit = False
            if open_p > 0:
                sell(i, open_p, "signal")

        # 2) 日内止盈止损（T+1：入场当日不检查）——阶梯委托内核，max_hold/cooldown 留在本函数
        if in_pos and i > entry_idx:
            # 符号约定不同：本模块 stop_loss 为正数（0.10），内核 sl 为带符号收益（-0.10）
            # check_exit 不读 hold_days（时间窗归调用方），故不传；传了会让人误以为 max_hold 也搬进内核
            plan = _single.SingleTradePlan(
                judge="intrabar", tp=take_profit,
                sl=-stop_loss if stop_loss is not None else None, sl_first=True)
            hit = _single.check_exit(entry_gross, open_=open_p,
                                     high=float(df.at[i, "high_adj"]),
                                     low=float(df.at[i, "low_adj"]),
                                     close=close_p, plan=plan)
            if hit is not None:
                if hit.reason == "SL":
                    reason = "stop_gap" if open_p <= entry_gross * (1 - stop_loss) else "stop_loss"
                else:
                    reason = "take_profit_gap" if open_p >= entry_gross * (1 + take_profit) else "take_profit"
                sell(i, hit.price, reason)

        # 3) 最长持有到期：收盘卖出
        if in_pos and max_hold and i - entry_idx >= max_hold:
            sell(i, close_p, "max_hold")

        # 4) 收盘估值
        equity = cash if not in_pos else shares * close_p
        equity_curve.append({
            "date": df.at[i, "date"],
            "equity": round(float(equity), 6),
            "position": 1 if in_pos else 0,
        })
        if in_pos:
            position_days += 1

        # 5) 收盘信号 → 下一交易日开盘执行
        if not in_pos and i < end_idx and entry_signal[i]:
            if i >= cooldown_until:
                pending_entry = True
            else:
                ignored_entries += 1
        if in_pos and i < end_idx and exit_signal[i]:
            pending_exit = True

    if in_pos:
        sell(end_idx, float(df.at[end_idx, "close_adj"]), "end")
        equity_curve[-1]["equity"] = round(float(cash), 6)
        equity_curve[-1]["position"] = 0
        position_days = max(0, position_days - 1)

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "fees_paid": round(float(fee_total), 6),
        "position_days": position_days,
        "ignored_entries": ignored_entries,
    }


def _metrics(df: pd.DataFrame, start_idx: int, end_idx: int, sim: dict) -> dict:
    curve = np.array([p["equity"] for p in sim["equity_curve"]], dtype=float)
    n_days = len(curve)
    total = float(curve[-1] / curve[0] - 1) if n_days else 0.0
    years = max(n_days / TRADING_DAYS, 1e-9)
    annual = (1 + total) ** (1 / years) - 1 if total > -1 else None
    peak = np.maximum.accumulate(curve) if n_days else np.array([1.0])
    dd = curve / peak - 1 if n_days else np.array([0.0])
    max_dd = float(dd.min()) if n_days else 0.0
    rets = np.diff(curve) / curve[:-1] if n_days > 1 else np.array([])
    sharpe = (float(rets.mean() / rets.std(ddof=1) * math.sqrt(TRADING_DAYS))
              if len(rets) > 1 and rets.std(ddof=1) > 0 else None)
    bench = float(df.at[end_idx, "close_adj"] / df.at[start_idx, "close_adj"] - 1)
    trades = sim["trades"]
    wins = [t["return_net"] for t in trades if t["return_net"] > 0]
    losses = [t["return_net"] for t in trades if t["return_net"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "total_return": round(total, 6),
        "annualized_return": round(float(annual), 6) if annual is not None else None,
        "benchmark_return": round(bench, 6),
        "excess_return": round(total - bench, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_trade_return": round(float(np.mean([t["return_net"] for t in trades])), 6)
        if trades else None,
        "avg_win": round(float(np.mean(wins)), 6) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 6) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "exposure": round(sim["position_days"] / n_days, 4) if n_days else 0.0,
        "trading_days": n_days,
        "start_date": df.at[start_idx, "date"],
        "end_date": df.at[end_idx, "date"],
    }


def _date_range(df: pd.DataFrame, start, end) -> tuple[int, int, str | None, str | None]:
    dates = df["date"].tolist()
    s = _norm_date(start) or "00000000"
    e = _norm_date(end) or "99999999"
    si = bisect_left(dates, s)
    ei = bisect_left(dates, e + "~") - 1  # 字符串日期上界
    if si >= len(dates):
        raise McpError(-32602, f"起始日期 {start} 晚于最后交易日 {dates[-1]}")
    ei = min(ei, len(dates) - 1)
    if ei <= si:
        raise McpError(-32602, f"区间内交易日不足：{dates[si]} ~ {dates[ei]}")
    return si, ei, dates[si], dates[ei]


def _run(code: str, strategy: str, start, end, params: dict | None,
         entry_signal: np.ndarray | None, exit_signal: np.ndarray | None,
         fee_bps: float, stamp_bps: float, slip_bps: float,
         include_equity_curve: bool) -> dict:
    sym, six = _norm_code(code)
    df, adjusted, warns = _load_prices(sym, six)
    if len(df) < 10:
        raise McpError(404, f"{sym} 有效日 K 不足：{len(df)} 条")
    start_idx, end_idx, first, last = _date_range(df, start, end)
    p = _effective_params(strategy, params) if strategy in STRATEGIES else {}
    if entry_signal is None or exit_signal is None:
        entry_signal, exit_signal = _signals(df, strategy, p, start_idx)
    else:
        # 显式信号：数组已按全量日期对齐
        entry_signal = np.asarray(entry_signal, dtype=bool)
        exit_signal = np.asarray(exit_signal, dtype=bool)

    sim = _simulate(
        df, entry_signal, exit_signal, start_idx, end_idx,
        fee_bps=fee_bps, stamp_bps=stamp_bps, slip_bps=slip_bps,
        take_profit=p.get("take_profit"), stop_loss=p.get("stop_loss"),
        max_hold=p.get("max_hold"), cooldown=int(p.get("cooldown", 0)),
    )
    metrics = _metrics(df, start_idx, end_idx, sim)
    result = {
        "ok": True,
        "code": sym,
        "strategy": strategy,
        "params": p,
        "adjusted": adjusted,
        "first_trade_date": first,
        "last_trade_date": last,
        "metrics": metrics,
        "trades": sim["trades"],
        "fees_paid": sim["fees_paid"],
        "ignored_entry_signals": sim["ignored_entries"],
        "cost_model": {
            "commission_bps_each_side": fee_bps,
            "stamp_duty_bps_sell": stamp_bps,
            "slippage_bps_each_side": slip_bps,
            "execution": "收盘信号 → 次日开盘成交；T+1 才能卖出",
        },
        "warnings": warns,
        "note": ("单标的满仓复利，未建模涨跌停无法成交/停牌/分红税；"
                 "收益为前复权口径，成本按成交额计提"),
    }
    if include_equity_curve:
        result["equity_curve"] = sim["equity_curve"]
    return result


# === 对外工具 ==============================================================
def list_strategies() -> dict:
    return {
        "ok": True,
        "strategies": [
            {"name": k, "description": v["description"], "defaults": v["defaults"]}
            for k, v in STRATEGIES.items()
        ],
        "common_params": {
            "take_profit": "止盈比例（如 0.2，可空）",
            "stop_loss": "止损比例（如 0.1，可空）",
            "max_hold": "最长持有交易日（可空）",
            "cooldown": "平仓后冷却交易日（默认按策略）",
        },
        "note": "信号收盘确认，次日开盘成交；成本默认双边佣金 0.025% + 卖出印花税 0.05% + 双边滑点 0.05%",
    }


def run_backtest(code: str, strategy: str = "ma_cross", start: str | None = None,
                 end: str | None = None, params: dict | None = None,
                 fee_bps: float = DEFAULT_FEE_BPS, stamp_bps: float = DEFAULT_STAMP_BPS,
                 slippage_bps: float = DEFAULT_SLIP_BPS,
                 include_equity_curve: bool = True) -> dict:
    if strategy not in STRATEGIES:
        raise McpError(-32602, f"未知策略：{strategy}；可选：{', '.join(STRATEGIES)}")
    for name, v, lo, hi in (("fee_bps", fee_bps, 0, 200),
                            ("stamp_bps", stamp_bps, 0, 200),
                            ("slippage_bps", slippage_bps, 0, 200)):
        if not lo <= float(v) <= hi:
            raise McpError(-32602, f"{name} 超出范围 [{lo}, {hi}]：{v}")
    result = _run(code, strategy, start, end, params, None, None,
                  float(fee_bps), float(stamp_bps), float(slippage_bps),
                  bool(include_equity_curve))
    logging.info("run_backtest %s %s trades=%s total=%.4f",
                 code, strategy, result["metrics"]["n_trades"],
                 result["metrics"]["total_return"])
    return result


def run_signal_backtest(code: str, signals: list, start: str | None = None,
                        end: str | None = None, hold_days: int = 5,
                        take_profit: float | None = None,
                        stop_loss: float | None = None,
                        cooldown: int = 0,
                        fee_bps: float = DEFAULT_FEE_BPS,
                        stamp_bps: float = DEFAULT_STAMP_BPS,
                        slippage_bps: float = DEFAULT_SLIP_BPS,
                        include_equity_curve: bool = True) -> dict:
    if not isinstance(signals, list) or not signals:
        raise McpError(-32602, "signals 必须是非空日期数组")
    hold_days = max(1, min(int(hold_days), 250))
    cooldown = max(0, min(int(cooldown), 60))
    if take_profit is not None and not 0.001 <= float(take_profit) <= 5.0:
        raise McpError(-32602, "take_profit 超出范围 (0.001, 5.0)")
    if stop_loss is not None and not 0.001 <= float(stop_loss) <= 0.95:
        raise McpError(-32602, "stop_loss 超出范围 (0.001, 0.95)")

    sym, six = _norm_code(code)
    df, adjusted, warns = _load_prices(sym, six)
    start_idx, end_idx, first, last = _date_range(df, start, end)
    dates = df["date"].tolist()

    entry_signal = np.zeros(len(df), dtype=bool)
    used, skipped = [], []
    for raw in signals:
        d = _norm_date(raw)
        if d is None:
            skipped.append({"signal": str(raw), "reason": "日期无法解析"})
            continue
        idx = bisect_left(dates, d)
        if idx >= len(dates) or dates[idx] > last or idx > end_idx:
            skipped.append({"signal": d, "reason": "超出回测区间/无后续交易日"})
            continue
        if dates[idx] < first:
            skipped.append({"signal": d, "reason": "早于回测起点"})
            continue
        entry_signal[idx] = True
        used.append(dates[idx])

    p = {
        "take_profit": float(take_profit) if take_profit is not None else None,
        "stop_loss": float(stop_loss) if stop_loss is not None else None,
        "max_hold": hold_days,
        "cooldown": cooldown,
    }
    sim = _simulate(
        df, entry_signal, np.zeros(len(df), dtype=bool), start_idx, end_idx,
        fee_bps=float(fee_bps), stamp_bps=float(stamp_bps), slip_bps=float(slippage_bps),
        take_profit=p["take_profit"], stop_loss=p["stop_loss"],
        max_hold=p["max_hold"], cooldown=cooldown,
    )
    metrics = _metrics(df, start_idx, end_idx, sim)
    result = {
        "ok": True,
        "code": sym,
        "strategy": "signal_backtest",
        "params": p,
        "adjusted": adjusted,
        "first_trade_date": first,
        "last_trade_date": last,
        "signals_used": sorted(set(used)),
        "signals_skipped": skipped,
        "metrics": metrics,
        "trades": sim["trades"],
        "fees_paid": sim["fees_paid"],
        "ignored_entry_signals": sim["ignored_entries"],
        "cost_model": {
            "commission_bps_each_side": float(fee_bps),
            "stamp_duty_bps_sell": float(stamp_bps),
            "slippage_bps_each_side": float(slippage_bps),
            "execution": "信号日收盘确认 → 次日开盘成交；T+1 才能卖出",
        },
        "warnings": warns,
        "note": ("每个信号最多开一仓；持仓期间的新信号忽略；"
                 "止盈止损从入场次日开始检查，未触发则持有 hold_days 日后收盘卖出"),
    }
    if include_equity_curve:
        result["equity_curve"] = sim["equity_curve"]
    logging.info("run_signal_backtest %s signals=%s trades=%s",
                 code, len(used), metrics["n_trades"])
    return result


# === 打板选股（daban 模型全市场扫描）========================================
DABAN_WORKERS = min(8, os.cpu_count() or 4)
_DABAN_MODEL_CACHE = None


def _load_daban_model():
    global _DABAN_MODEL_CACHE
    if _DABAN_MODEL_CACHE is None:
        if not DABAN_MODEL.exists():
            raise McpError(404, f"daban 模型不存在：{DABAN_MODEL}")
        import lightgbm as lgb
        _DABAN_MODEL_CACHE = lgb.Booster(model_file=str(DABAN_MODEL))
    return _DABAN_MODEL_CACHE


def _load_universe() -> dict[str, str]:
    """沪深个股 {6位代码: 名称}，仅 60/68/00/30 前缀（不含指数/ETF/北交所）。"""
    names: dict[str, str] = {}
    if UNIVERSE_FILE.exists():
        try:
            raw = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                names = {str(k).zfill(6): str(v) for k, v in raw.items() if str(k).isdigit()}
        except Exception as e:
            logging.warning("股票池读取失败，回退扫描 parquet：%s", e)
    if not names:
        for fp in DAILY_DIR.glob("*.parquet"):
            if ".bak" in fp.stem:
                continue
            stem = fp.stem
            six = stem[2:] if stem[:2] in ("sh", "sz", "bj") else stem
            names[six] = ""
    return {c: n for c, n in names.items() if c[:2] in ("60", "68", "00", "30")}


def _to_ymd(series: pd.Series) -> pd.Series:
    if pd.api.types.is_integer_dtype(series):
        return pd.to_datetime(series.astype(str).str.zfill(8), format="%Y%m%d",
                              errors="coerce")
    return pd.to_datetime(series, errors="coerce")


def _latest_market_date() -> str:
    for ref in ("sh000001", "sh600000"):
        fp = DAILY_DIR / f"{ref}.parquet"
        if not fp.exists():
            continue
        s = _to_ymd(pd.read_parquet(fp, columns=["date"])["date"])
        if s.notna().any():
            return str(s.max().strftime("%Y-%m-%d"))
    raise McpError(404, "无法从指数/样本股 K 线确定最新交易日")


def _norm_ymd(value) -> str | None:
    d = _norm_date(value)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else None


def _daban_scan_one(task: tuple[str, str]) -> dict:
    six, target = task
    sym = ("sh" if six.startswith("6") else "sz") + six
    fp = DAILY_DIR / f"{sym}.parquet"
    if not fp.exists():
        return {"code": six, "sym": sym, "status": "missing"}
    try:
        df = pd.read_parquet(fp)
    except Exception as e:
        return {"code": six, "sym": sym, "status": "read_error", "detail": str(e)[:120]}
    if df.empty or not {"date", "open", "high", "low", "close"}.issubset(df.columns):
        return {"code": six, "sym": sym, "status": "missing"}

    dates = _to_ymd(df["date"]).dt.strftime("%Y-%m-%d")
    hits = np.flatnonzero((dates == target).to_numpy())
    if hits.size == 0:
        return {"code": six, "sym": sym, "status": "no_bar"}
    idx = int(hits[-1])
    if idx < 20:
        return {"code": six, "sym": sym, "status": "short_history"}

    close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    openp = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
    if not (np.isfinite(close[idx]) and np.isfinite(close[idx - 1])) or close[idx] <= 0:
        return {"code": six, "sym": sym, "status": "bad_price"}

    limit_val = get_limit(sym)
    if is_open_limit_up(float(openp[idx]), float(close[idx - 1]), limit_val):
        return {"code": six, "sym": sym, "status": "open_limit"}

    feat = compute_window_features_extended(df, sym, sig_idx=idx)
    if feat is None:
        return {"code": six, "sym": sym, "status": "short_history"}

    volume = 0.0
    if "volume" in df.columns:
        v = pd.to_numeric(df["volume"], errors="coerce").iloc[idx]
        volume = float(v) if pd.notna(v) else 0.0
    amount = 0.0
    if "amount" in df.columns:
        a = pd.to_numeric(df["amount"], errors="coerce").iloc[idx]
        amount = float(a) if pd.notna(a) else 0.0
    amount_est = amount if amount > 0 else close[idx] * volume * 100.0

    return {
        "code": six,
        "sym": sym,
        "status": "ok",
        "close": float(close[idx]),
        "pct_today": float(close[idx] / close[idx - 1] - 1.0),
        "amount_est": amount_est,
        "amount_is_estimated": amount <= 0,
        "feats": {k: (float(v) if isinstance(v, (int, float, np.floating, np.integer))
                      else 0.0) for k, v in feat.items()},
    }


def _daban_rows_to_frame(ok: list[dict]) -> pd.DataFrame:
    """展开 worker 结果。注意 `prev_close` 是模型特征名（历史命名，值为当日收盘），
    不能被同义的行情字段覆盖，否则当日有涨跌的股票打分整体偏移。
    """
    rows = []
    for r in ok:
        rows.append({
            **r["feats"],
            "code": r["code"], "sym": r["sym"], "close": r["close"],
            "pct_today": r["pct_today"],
            "amount_est": r["amount_est"],
            "amount_is_estimated": r["amount_is_estimated"],
        })
    return pd.DataFrame(rows)


def daban_top_picks(date: str | None = None, top: int = 5,
                    exclude_limit_today: bool = False,
                    min_amount: float = 0.0,
                    include_st: bool = False) -> dict:
    """全市场 daban 打分：T+1 收盘涨停概率 Top-N（收盘后使用）。"""
    try:
        top = int(top)
        min_amount = float(min_amount)
    except (TypeError, ValueError):
        raise McpError(-32602, "top 必须是整数，min_amount 必须是数字") from None
    if not 1 <= top <= 50:
        raise McpError(-32602, f"top 超出范围 [1, 50]：{top}")
    if min_amount < 0:
        raise McpError(-32602, "min_amount 不能为负")
    if date is None:
        target, date_source = _latest_market_date(), "latest"
    else:
        target = _norm_ymd(date)
        if target is None:
            raise McpError(-32602, f"日期无法解析：{date}")
        date_source = "requested"

    t0 = time.time()
    universe = _load_universe()
    codes = [c for c, n in universe.items()
             if include_st or not _is_st_name(n)]
    with ProcessPoolExecutor(max_workers=DABAN_WORKERS) as ex:
        results = list(ex.map(_daban_scan_one, [(c, target) for c in codes],
                              chunksize=64))

    status = Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] == "ok"]
    warnings_out: list[str] = [
        "⚠️ 本结果只作排序研究，不是买入指令：daban 在诚实成交口径下为负期望"
        "（K=5 平均净收益 -0.78%/笔，累计 -94.5%，执行闸门 overall_go=false）；"
        "详见 docs/DABAN_TOPK_PNL_20261022.md"
    ]
    if not ok:
        return {
            "ok": True, "date": target, "date_source": date_source,
            "picks": [], "universe": {"listed": len(universe), "scanned": len(codes)},
            "warnings": [f"{target} 无任何个股 K 线，可能不是交易日或数据未更新"],
            "elapsed_sec": round(time.time() - t0, 1),
        }

    df = _daban_rows_to_frame(ok)

    n_valid = len(df)
    df = df[(df["is_new"] == 0) & (df["close"] >= 1.0)]
    if exclude_limit_today:
        df = df[df["is_limit_up_today"] == 0]
    if min_amount > 0:
        df = df[df["amount_est"] >= min_amount]
    if df.empty:
        warnings_out.append("过滤后无候选（min_amount 过高或新股/低价股被剔除）")

    picks: list[dict] = []
    if not df.empty:
        model = _load_daban_model()
        feature_cols = model.feature_name()
        missing = [c for c in feature_cols if c not in df.columns]
        if missing:
            raise McpError(-32603, f"模型特征缺失：{missing}")
        df = df.copy()
        df["score"] = model.predict(df[feature_cols].fillna(0))
        df = df.sort_values(["score", "amount_est"], ascending=[False, False])
        for rank, (_, r) in enumerate(df.head(top).iterrows(), 1):
            six = str(r["code"])
            name = universe.get(six, "")
            picks.append({
                "rank": rank,
                "code": r["sym"],
                "name": name,
                "score": round(float(r["score"]), 6),
                "close": round(float(r["close"]), 2),
                "pct_today": round(float(r["pct_today"]), 4),
                "limit_up_price_t1": limit_up_price(float(r["close"]),
                                                    limit_ratio(six, name)),
                "consecutive_boards": int(r["consecutive_boards"]),
                "limit_up_today": bool(int(r["is_limit_up_today"])),
                "ret_5d": round(float(r["ret_5d"]), 4),
                "amount_est_yuan": round(float(r["amount_est"]), 0),
            })

    est_share = float(np.mean([r["amount_is_estimated"] for r in ok]))
    if est_share > 0.5:
        warnings_out.append(
            f"最新交易日成交额字段缺失率 {est_share:.0%}（amount=0），"
            "流动性按 close×volume×100 估算；模型内成交额类特征在当前数据上退化为常数")
    if n_valid < 1000:
        warnings_out.append(f"有效样本仅 {n_valid} 只，数据可能未完整更新")

    return {
        "ok": True,
        "date": target,
        "date_source": date_source,
        "model": {
            "name": "daban",
            "path": str(DABAN_MODEL),
            "target": "T+1 收盘涨停概率",
            "n_features": len(_load_daban_model().feature_name()),
        },
        "universe": {
            "listed": len(universe),
            "after_st_filter": len(codes),
            "scanned": len(results),
            "valid": n_valid,
            "after_filters": len(df),
            "skipped": {k: v for k, v in status.items() if k != "ok"},
        },
        "filters": {
            "skip_open_limit_today": True,
            "exclude_limit_today": exclude_limit_today,
            "include_st": include_st,
            "min_amount": min_amount,
            "min_close": 1.0,
            "skip_new": True,
        },
        "picks": picks,
        "warnings": warnings_out,
        "elapsed_sec": round(time.time() - t0, 1),
        "note": ("daban 模型预测 T+1 收盘涨停概率；T 日开盘即涨停（实盘买不进）样本已剔除；"
                 "Top-N 为候选而非买入建议，打板属高风险策略"),
    }


# === MCP dispatcher ========================================================
TOOLS = {
    "list_strategies": {
        "description": "列出内置策略与默认参数",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda a: list_strategies(),
    },
    "run_backtest": {
        "description": "按内置策略回测单只股票（前复权、T+1 开盘成交）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码，如 sh600000 / 600000"},
                "strategy": {"type": "string", "default": "ma_cross",
                             "enum": list(STRATEGIES.keys())},
                "start": {"type": "string", "description": "起始日期 YYYYMMDD / YYYY-MM-DD"},
                "end": {"type": "string", "description": "结束日期"},
                "params": {"type": "object",
                           "description": "策略参数 + take_profit/stop_loss/max_hold/cooldown"},
                "fee_bps": {"type": "number", "default": DEFAULT_FEE_BPS},
                "stamp_bps": {"type": "number", "default": DEFAULT_STAMP_BPS},
                "slippage_bps": {"type": "number", "default": DEFAULT_SLIP_BPS},
                "include_equity_curve": {"type": "boolean", "default": True},
            },
            "required": ["code"],
        },
        "handler": lambda a: run_backtest(**a),
    },
    "run_signal_backtest": {
        "description": "按显式信号日期回测（用于预测/选股信号验证）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码"},
                "signals": {"type": "array", "items": {"type": "string"},
                            "description": "信号日期数组（YYYYMMDD 或 YYYY-MM-DD）"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "hold_days": {"type": "integer", "default": 5},
                "take_profit": {"type": "number", "description": "止盈比例，如 0.2"},
                "stop_loss": {"type": "number", "description": "止损比例，如 0.1"},
                "cooldown": {"type": "integer", "default": 0},
                "fee_bps": {"type": "number", "default": DEFAULT_FEE_BPS},
                "stamp_bps": {"type": "number", "default": DEFAULT_STAMP_BPS},
                "slippage_bps": {"type": "number", "default": DEFAULT_SLIP_BPS},
                "include_equity_curve": {"type": "boolean", "default": True},
            },
            "required": ["code", "signals"],
        },
        "handler": lambda a: run_signal_backtest(**a),
    },
    "daban_top_picks": {
        "description": "全市场打板选股：daban 模型打分，输出 T+1 收盘涨停概率 Top-N",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string",
                         "description": "信号日 YYYYMMDD / YYYY-MM-DD，默认最新交易日"},
                "top": {"type": "integer", "default": 5,
                        "description": "返回前 N 只（1-50）"},
                "exclude_limit_today": {
                    "type": "boolean", "default": False,
                    "description": "剔除 T 日已收盘涨停的股票（默认保留，连板接力是模型主要信号）",
                },
                "min_amount": {"type": "number", "default": 0,
                               "description": "T 日最低成交额（元），0=不过滤"},
                "include_st": {"type": "boolean", "default": False,
                               "description": "是否保留 ST 股"},
            },
        },
        "handler": lambda a: daban_top_picks(**a),
    },
}


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-backtest", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "tools": [{"name": n, "description": s["description"],
                           "inputSchema": s["inputSchema"]} for n, s in TOOLS.items()],
            }}
        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name not in TOOLS:
                raise McpError(-32602, f"未知工具：{name}")
            t0 = time.time()
            result = TOOLS[name]["handler"](args)
            logging.info("tool=%s latency=%.1fms", name, (time.time() - t0) * 1000)
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, default=str)}],
            }}
        if method == "notifications/initialized":
            return None
        raise McpError(-32601, f"未知方法：{method}")
    except McpError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        logging.exception("handler error")
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)}}


def main():
    print("[mcp:backtest] 已启动，stdio 模式（日K + 前复权因子）", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    main()
