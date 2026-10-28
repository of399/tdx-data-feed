"""内核 B：打板持有期结算（次日开盘买、指定时点卖、跌停顺延）。

与内核 A 的区别不是参数，是口径：这里「买不进」和「卖不出」都是必须显式
输出的状态，不能当没发生过，也不能回填一个理想成交价。

已知建模简化：开盘卖模式（`next_open` / `t_plus_2_open`）按开盘无条件成交，
不判开盘是否跌停——既有 daban `topk` 就是这个口径，迁移要保数；跌停不可卖
只在收盘卖模式（`next_close`）上判定。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import pandas as pd

from .bars import date_str
from .costs import CostModel
from .limits import LimitRule
from .result import TradeResult

DEFAULT_DEFER = 3


@dataclass(frozen=True)
class HoldPlan:
    buy: Literal["next_open", "signal_close", "signal_limit_up"] = "next_open"
    sell: Literal["next_open", "next_close", "t_plus_2_open"] = "next_close"
    max_defer: int = DEFAULT_DEFER
    sell_offset: int = 2   # 仅 t_plus_2_open 用：卖在 signal_idx + sell_offset 的开盘


def _row(bars: pd.DataFrame, idx: int) -> dict:
    """只取内核真正读的四列：开盘卖出与跌停顺延都不看 high/low。"""
    r = bars.iloc[idx]
    return {"date": date_str(r["date"]), "open": float(r["open"]),
            "close": float(r["close"]), "volume": float(r["volume"])}


def _gap_result(entry_idx: int, bar: dict, entry_price: float, status: str) -> TradeResult:
    """没有成交的一笔：entry_idx 由调用方给（signal 买入模式下它不等于 signal_idx+1）。"""
    return TradeResult(entry_idx=entry_idx, entry_date=bar["date"],
                       entry_price=entry_price, exit_idx=entry_idx,
                       exit_date=bar["date"], exit_price=0.0, reason=status,
                       gross_ret=0.0, net_ret=0.0, status=status)


def settle_hold_trade(bars: pd.DataFrame, signal_idx: int, plan: HoldPlan,
                      costs: CostModel, rule: LimitRule) -> TradeResult:
    n = len(bars)
    if signal_idx < 0 or signal_idx >= n:
        raise IndexError(f"signal_idx={signal_idx} 超出 bars 长度 {n}")

    sig = _row(bars, signal_idx)
    entry_idx = signal_idx + 1
    if plan.buy == "signal_close":
        entry_idx = signal_idx
        entry_price = sig["close"]
    elif plan.buy == "signal_limit_up":
        entry_price = rule.up_price(sig["close"], sig["date"])
        entry_idx = signal_idx
    else:
        if entry_idx >= n:
            return _gap_result(entry_idx, sig, 0.0, "no_entry")
        eb = _row(bars, entry_idx)
        if eb["open"] <= 0 or eb["volume"] <= 0:
            return _gap_result(entry_idx, eb, 0.0, "suspended")
        if rule.is_open_limit_up(eb["open"], sig["close"], eb["date"]):
            return _gap_result(entry_idx, eb, eb["open"], "unbuyable_open_limit")
        entry_price = eb["open"]

    # 信号帧收盘价为 0/负（停牌脏数据）时入场价无效：不是算出来的 0 元成交，是买不进
    if entry_price <= 0:
        return _gap_result(entry_idx, sig, 0.0, "suspended")

    if plan.sell == "t_plus_2_open":
        offset = int(plan.sell_offset)
        # sell_offset<=1 会让出场落在入场当根：零收益却 status="ok"，是静默腐化
        if offset < 2:
            raise ValueError(
                f"t_plus_2_open 至少卖在 signal_idx+2，收到 sell_offset={offset}")
        exit_idx = signal_idx + offset
        sell_reason = f"t{offset}_open"
        sell_at = "open"
    elif plan.sell == "next_open":
        exit_idx = entry_idx + 1
        sell_reason = "t2_open"
        sell_at = "open"
    else:
        exit_idx = entry_idx
        sell_reason = "t1_close"
        sell_at = "close"

    if exit_idx >= n:
        return _gap_result(entry_idx, _row(bars, entry_idx), entry_price, "no_exit")

    bar = _row(bars, exit_idx)
    # exit_idx 可能等于 0（signal_close 买在第 0 根），此时前收取入场价而非绕到末根
    prev_close = float(bars["close"].iloc[exit_idx - 1]) if exit_idx > 0 else entry_price
    if sell_at == "open":
        if bar["open"] <= 0:
            return _gap_result(entry_idx, bar, entry_price, "no_exit")
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["open"],
                       sell_reason, costs, bars)

    if bar["close"] <= 0:
        return _gap_result(entry_idx, bar, entry_price, "no_exit")
    lim_dn = rule.down_price(prev_close, bar["date"])
    if lim_dn > 0 and bar["close"] > lim_dn + rule.tol:
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["close"],
                       sell_reason, costs, bars)

    # max_defer=0：不顺延，跌停也按当日收盘强制成交（daban topk 的「到期就走」）
    if plan.max_defer <= 0:
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["close"],
                       f"{sell_reason}_forced", costs, bars)

    # 收盘跌停 → 顺延：每个顺延日先试开盘、再试收盘，最后一日强制按收盘
    max_days = int(plan.max_defer)
    for k in range(1, max_days + 1):
        j = exit_idx + k
        if j >= n:
            r = _gap_result(entry_idx, bar, entry_price, "deferred_unsold")
            return replace(r, deferred=k - 1)
        b = _row(bars, j)
        pc = float(bars["close"].iloc[j - 1])
        dn = rule.down_price(pc, b["date"])
        is_last = k >= max_days
        if b["open"] > 0 and (dn <= 0 or b["open"] > dn + rule.tol):
            return _finish(entry_idx, entry_price, j, b["date"], b["open"],
                           f"t{j - entry_idx + 1}_open", costs, bars,
                           deferred=k)
        if b["close"] > 0 and (dn <= 0 or b["close"] > dn + rule.tol or is_last):
            px = b["close"]
            reason = f"t{j - entry_idx + 1}_close" + ("_forced" if is_last else "")
            return _finish(entry_idx, entry_price, j, b["date"], px, reason,
                           costs, bars, deferred=k)
    r = _gap_result(entry_idx, bar, entry_price, "deferred_unsold")
    return replace(r, deferred=max_days)


def _finish(entry_idx: int, entry_price: float, exit_idx: int, exit_date: str,
            exit_price: float, reason: str, costs: CostModel,
            bars: pd.DataFrame, *, deferred: int = 0) -> TradeResult:
    gross = exit_price / entry_price - 1.0
    net = costs.round_trip(entry_price, exit_price)
    return TradeResult(
        entry_idx=entry_idx,
        entry_date=date_str(bars["date"].iloc[entry_idx]),
        entry_price=entry_price,
        exit_idx=exit_idx,
        exit_date=exit_date,
        exit_price=exit_price,
        reason=reason,
        gross_ret=gross,
        net_ret=net,
        deferred=deferred,
    )
