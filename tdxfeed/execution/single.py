"""内核 A：单笔 TP/SL 结算。

入场时点不进内核——它是策略差异（plan_e 在 T+1 确认后于 T+2 开盘入场，
sim_live 在信号日次日开盘入场）。调用方算好 entry_idx，内核只从该根往后跑。

口径（不可配，逐位对齐 plan_e 的 `_settle_from_entry`）：
  - 同日双触发按 `sl_first` 指定侧结算，默认亏损侧；日内高低点证明不了先后。
  - 跳空穿越触发价按当日开盘成交：止损 `min(sl_lvl, open)`，止盈 `max(tp_lvl, open)`。
  - 超时根含入场根计数：最晚在 `entry_idx + hold_days - 1` 收盘成交。
  - `daily` 在入场根与出场根各扣一次单边成本，出场根额外含印花税。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import pandas as pd

from .bars import date_str
from .costs import CostModel
from .result import TradeResult


@dataclass(frozen=True)
class SingleTradePlan:
    judge: Literal["intrabar", "close_only"] = "intrabar"
    tp: float | None = 0.30
    sl: float | None = -0.10
    hold_days: int = 10
    sl_first: bool = True


@dataclass(frozen=True)
class ExitHit:
    price: float
    reason: str


def exit_levels(entry_price: float, plan: SingleTradePlan) -> tuple[float | None, float | None]:
    tp_lvl = entry_price * (1 + plan.tp) if plan.tp is not None else None
    sl_lvl = entry_price * (1 + plan.sl) if plan.sl is not None else None
    return tp_lvl, sl_lvl


def check_exit(entry_price: float, *, open_: float, high: float, low: float,
               close: float, plan: SingleTradePlan) -> ExitHit | None:
    """单根 K 线的出场判定。日内高低点证明不了先后，双触发取 plan.sl_first 指定侧。"""
    tp_lvl, sl_lvl = exit_levels(entry_price, plan)
    if plan.judge == "close_only":
        ret = close / entry_price - 1.0 if entry_price > 0 else 0.0
        if plan.sl_first:
            if sl_lvl is not None and ret <= plan.sl:
                return ExitHit(price=close, reason="SL")
            if tp_lvl is not None and ret >= plan.tp:
                return ExitHit(price=close, reason="TP")
        else:
            if tp_lvl is not None and ret >= plan.tp:
                return ExitHit(price=close, reason="TP")
            if sl_lvl is not None and ret <= plan.sl:
                return ExitHit(price=close, reason="SL")
        return None

    hit_sl = sl_lvl is not None and low <= sl_lvl
    hit_tp = tp_lvl is not None and high >= tp_lvl
    if not (hit_sl or hit_tp):
        return None
    open_j = open_ if open_ > 0 else None
    if hit_sl and (plan.sl_first or not hit_tp):
        price = min(sl_lvl, open_j) if open_j is not None else sl_lvl
        return ExitHit(price=price, reason="SL")
    price = max(tp_lvl, open_j) if open_j is not None else tp_lvl
    return ExitHit(price=price, reason="TP")


def entry_index(bars: pd.DataFrame, signal_idx: int, lag: int) -> int | None:
    idx = signal_idx + lag
    return idx if 0 <= idx < len(bars) else None


def settle_single_trade(
    bars: pd.DataFrame,
    entry_idx: int,
    plan: SingleTradePlan,
    costs: CostModel,
    *,
    buyable: Callable[[int], bool] | None = None,
    insufficient: Literal["skip", "last_close"] = "skip",
) -> TradeResult | None:
    """从 entry_idx 的 open 入场，按出场阶梯结算一笔。

    ``insufficient`` 只在「出场窗口被数据端点截断」时起作用：
      - ``"skip"``：不因截断丢单，按最后一根收盘结算并仍报 ``TIME``
        （plan_e 的 ``_has_exit_window`` 只要求 ≥3 根可判定窗口，截断笔同样报 TIME）；
      - ``"last_close"``：同样按最后一根收盘结算，但标 ``END``，供调用方区分
        「计划超时」与「数据到头」。
    可判定根数为 0（入场根之后没有数据）时两种取值都返回 None。
    """
    n = len(bars)
    if entry_idx < 0 or entry_idx >= n:
        return None
    entry = float(bars["open"].iloc[entry_idx])
    if entry <= 0:
        return None
    if buyable is not None and not buyable(entry_idx):
        return None

    start = entry_idx + 1
    end = min(entry_idx + plan.hold_days, n)
    if end <= start:
        return None
    # 计划超时根（entry_idx + hold_days - 1）是否真的落在数据里
    complete = end - start >= plan.hold_days - 1

    exit_idx, reason, exit_price = end - 1, ("TIME" if complete else "END"), None
    for j in range(start, end):
        hit = check_exit(entry, open_=float(bars["open"].iloc[j]),
                         high=float(bars["high"].iloc[j]),
                         low=float(bars["low"].iloc[j]),
                         close=float(bars["close"].iloc[j]), plan=plan)
        if hit is not None:
            exit_idx, reason, exit_price = j, hit.reason, hit.price
            break

    if reason == "END" and insufficient == "skip":
        reason = "TIME"

    high_max = float(bars["high"].iloc[entry_idx:exit_idx + 1].max())
    low_min = float(bars["low"].iloc[entry_idx:exit_idx + 1].min())
    if reason in ("TIME", "END"):
        exit_price = float(bars["close"].iloc[exit_idx])
    if exit_price is None or exit_price <= 0:
        return None

    gross = exit_price / entry - 1.0
    net = costs.round_trip(entry, exit_price)

    side_cost = costs.commission + costs.slippage + costs.stamp_sell
    daily: list[tuple[str, float]] = []
    prev_close = entry
    for k in range(entry_idx, exit_idx + 1):
        close_k = exit_price if k == exit_idx else float(bars["close"].iloc[k])
        r = close_k / prev_close - 1.0 if prev_close > 0 else 0.0
        if k == entry_idx:
            r -= costs.commission + costs.slippage
        if k == exit_idx:
            r -= side_cost
        daily.append((date_str(bars["date"].iloc[k]), r))
        prev_close = close_k

    return TradeResult(
        entry_idx=entry_idx,
        entry_date=date_str(bars["date"].iloc[entry_idx]),
        entry_price=entry,
        exit_idx=exit_idx,
        exit_date=date_str(bars["date"].iloc[exit_idx]),
        exit_price=exit_price,
        reason=reason,
        gross_ret=gross,
        net_ret=net,
        max_gain=(high_max - entry) / entry,
        max_loss=(low_min - entry) / entry,
        daily=tuple(daily),
    )
