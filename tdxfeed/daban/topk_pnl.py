"""daban Top-K 逐笔 PnL：T 日收盘信号 → T+1 开盘买入 → T+2 开盘卖出。"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from tdxfeed.daban.limit_price import exchange_prefix
from tdxfeed.features_5d import get_limit, is_open_limit_up


def net_return(entry_price: float, exit_price: float, *, fee_bps: float = 2.5,
               stamp_bps: float = 5.0, slip_bps: float = 5.0) -> float:
    fee, stamp, slip = fee_bps / 1e4, stamp_bps / 1e4, slip_bps / 1e4
    entry_exec = entry_price * (1 + slip)
    exit_exec = exit_price * (1 - slip)
    if entry_exec <= 0:
        return float("nan")
    return (1 - fee) * (1 - fee - stamp) * exit_exec / entry_exec - 1.0


def _factor(df: pd.DataFrame, idx: int) -> float:
    if "adj_factor" not in df.columns:
        return 1.0
    v = df.at[idx, "adj_factor"]
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    return v if np.isfinite(v) and v > 0 else 1.0


def _skip(code: str, signal_date: str, reason: str) -> dict[str, Any]:
    return {"status": "skip", "code": code, "signal_date": signal_date,
            "reason": reason}


def simulate_trade(df: pd.DataFrame, signal_idx: int, code: str, *,
                   exit_offset: int = 2, fee_bps: float = 2.5,
                   stamp_bps: float = 5.0, slip_bps: float = 5.0) -> dict[str, Any]:
    """信号日 T 收盘 → T+1 开盘买入 → T+exit_offset 开盘卖出。"""
    signal_date = str(df.at[signal_idx, "date"])[:10]
    entry_idx = signal_idx + 1
    if entry_idx >= len(df):
        return _skip(code, signal_date, "no_entry")
    if exit_offset < 2:
        return _skip(code, signal_date, "t_plus_1")
    exit_idx = signal_idx + exit_offset
    if exit_idx >= len(df):
        return _skip(code, signal_date, "no_exit")

    entry_open = float(df.at[entry_idx, "open"])
    entry_vol = float(df.at[entry_idx, "volume"])
    prev_close = float(df.at[signal_idx, "close"])
    if entry_open <= 0 or entry_vol <= 0:
        return _skip(code, signal_date, "suspended")
    if is_open_limit_up(entry_open, prev_close, get_limit(code)):
        return _skip(code, signal_date, "open_limit")

    exit_open = float(df.at[exit_idx, "open"])
    if exit_open <= 0:
        return _skip(code, signal_date, "no_exit")

    entry_adj = entry_open * _factor(df, entry_idx)
    exit_adj = exit_open * _factor(df, exit_idx)
    if entry_adj <= 0:
        return _skip(code, signal_date, "bad_price")
    gross = exit_adj / entry_adj - 1.0
    net = net_return(entry_adj, exit_adj, fee_bps=fee_bps,
                     stamp_bps=stamp_bps, slip_bps=slip_bps)
    return {
        "status": "ok",
        "code": code,
        "sym": exchange_prefix(code) + code.zfill(6),
        "signal_date": signal_date,
        "entry_date": str(df.at[entry_idx, "date"])[:10],
        "exit_date": str(df.at[exit_idx, "date"])[:10],
        "entry_price": round(entry_open, 4),
        "exit_price": round(exit_open, 4),
        "ret_gross": gross,
        "ret_net": net,
        "hold_days": exit_idx - entry_idx,
    }


def aggregate(trades: list[dict], n_signal_days: int, k: int) -> dict[str, Any]:
    ok = [t for t in trades if t.get("status") == "ok"]
    n_slots = max(n_signal_days * k, 1)
    if not ok:
        return {"n_trades": 0, "fill_rate": 0.0, "total_return": 0.0,
                "annualized_return": None, "max_drawdown": 0.0, "sharpe": None,
                "win_rate": None, "avg_trade_return": None,
                "median_trade_return": None, "profit_factor": None,
                "daily_mean": None, "n_days_with_trades": 0}
    df = pd.DataFrame(ok)
    daily = df.groupby("signal_date", sort=True)["ret_net"].mean()
    equity = (1 + daily).cumprod()
    total = float(equity.iloc[-1] - 1)
    years = len(daily) / 243.0
    annual = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else None
    dd = float((equity / equity.cummax() - 1).min())
    rets = daily.to_numpy(dtype=float)
    sharpe = (float(rets.mean() / rets.std(ddof=1) * np.sqrt(243))
              if len(rets) > 1 and rets.std(ddof=1) > 0 else None)
    net = df["ret_net"]
    wins = net[net > 0]
    losses = net[net <= 0]
    return {
        "n_trades": len(df),
        "fill_rate": len(df) / n_slots,
        "total_return": total,
        "annualized_return": annual,
        "max_drawdown": dd,
        "sharpe": sharpe,
        "win_rate": float((net > 0).mean()),
        "avg_trade_return": float(net.mean()),
        "median_trade_return": float(net.median()),
        "profit_factor": (float(wins.sum() / abs(losses.sum()))
                          if len(losses) and losses.sum() < 0 else None),
        "daily_mean": float(daily.mean()),
        "n_days_with_trades": len(daily),
    }
