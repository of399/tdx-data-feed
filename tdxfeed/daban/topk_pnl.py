"""daban Top-K 逐笔 PnL：T 日收盘信号 → T+1 开盘买入 → T+2 开盘卖出。"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from tdxfeed.daban.limit_price import exchange_prefix
from tdxfeed.execution.bars import date_str
from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.hold import HoldPlan, settle_hold_trade
from tdxfeed.execution.limits import LimitRule


def net_return(entry_price: float, exit_price: float, *, fee_bps: float = 2.5,
               stamp_bps: float = 5.0, slip_bps: float = 5.0) -> float:
    """双边滑点 + 双边佣金 + 卖出印花税，委托 `CostModel.round_trip`（唯一费用公式）。"""
    if entry_price <= 0:
        return float("nan")
    cm = CostModel(commission=fee_bps / 1e4, stamp_sell=stamp_bps / 1e4,
                   slippage=slip_bps / 1e4)
    return cm.round_trip(entry_price, exit_price)


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
    """信号日 T 收盘 → T+1 开盘买入 → T+exit_offset 开盘卖出（判定委托内核 B）。"""
    signal_date = date_str(df["date"].iloc[signal_idx])
    if exit_offset < 2:
        return _skip(code, signal_date, "t_plus_1")
    plan = HoldPlan(buy="next_open", sell="t_plus_2_open", max_defer=0,
                    sell_offset=int(exit_offset))
    costs = CostModel(commission=fee_bps / 1e4, stamp_sell=stamp_bps / 1e4,
                      slippage=slip_bps / 1e4)
    r = settle_hold_trade(df, signal_idx, plan, costs, LimitRule(code))
    if r.status == "unbuyable_open_limit":
        return _skip(code, signal_date, "open_limit")
    if r.status != "ok":
        return _skip(code, signal_date, r.status)

    # 净收益按复权价重算：内核按原始价结算（涨停判定必须用真实价），复权是调用方口径
    entry_adj = r.entry_price * _factor(df, r.entry_idx)
    exit_adj = r.exit_price * _factor(df, r.exit_idx)
    return {
        "status": "ok",
        "code": code,
        "sym": exchange_prefix(code) + code.zfill(6),
        "signal_date": signal_date,
        "entry_date": r.entry_date,
        "exit_date": r.exit_date,
        "entry_price": round(r.entry_price, 4),
        "exit_price": round(r.exit_price, 4),
        "ret_gross": exit_adj / entry_adj - 1.0,
        "ret_net": net_return(entry_adj, exit_adj, fee_bps=fee_bps,
                              stamp_bps=stamp_bps, slip_bps=slip_bps),
        "hold_days": r.exit_idx - r.entry_idx,
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
