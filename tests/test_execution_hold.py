"""内核 B：买不进要显式说出来，跌停卖不出要顺延而不是当没发生。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.hold import HoldPlan, settle_hold_trade
from tdxfeed.execution.limits import LimitRule

FREE = CostModel(commission=0.0, slippage=0.0)
RULE = LimitRule(code="600519")


def _bars(rows):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows)),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [r[4] if len(r) > 4 else 1_000_000 for r in rows],
    })


def test_buy_next_open_sell_next_close():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.4, 10.6, 10.2, 10.5)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r.entry_idx == 1 and r.entry_price == 10.0
    assert r.exit_idx == 1 and r.exit_price == 10.4
    assert r.reason == "t1_close"


def test_open_limit_up_is_unbuyable():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (11.0, 11.0, 11.0, 11.0), (11.1, 11.2, 11.0, 11.1)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r is not None and r.status == "unbuyable_open_limit"
    assert r.entry_idx == 1


def test_suspended_entry():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0, 0), (10.1, 10.2, 10.0, 10.1)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r.status == "suspended"


def test_limit_down_close_defers_and_limit_down_open_blocks_open_sell():
    # 买入 10.0；T+1 收盘跌停 9.0；T+2 前收 9.0 → 跌停价 8.1，开盘恰在 8.1（卖不出），
    # 收盘拉回 9.2 → 按 T+2 收盘卖
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (8.1, 9.3, 8.1, 9.2),
        (9.2, 9.3, 9.1, 9.2),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.exit_idx == 2
    assert r.exit_price == 9.2
    assert r.reason == "t2_close"
    assert r.deferred == 1


def test_defer_day_can_sell_at_open_when_open_not_limit_down():
    # T+1 收盘跌停；T+2 开盘 9.1 未跌停 → 按 T+2 开盘卖
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (9.1, 9.3, 9.0, 9.2),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.exit_idx == 2
    assert r.reason == "t2_open"
    assert r.exit_price == 9.1


def test_last_defer_day_forces_close_even_at_limit_down():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (8.1, 8.2, 8.1, 8.1),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=1), FREE, RULE)
    assert r.exit_idx == 2
    assert r.reason == "t2_close_forced"
    assert r.exit_price == 8.1


def test_t_plus_2_open_sell():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.6, 10.8, 10.5, 10.7)])
    r = settle_hold_trade(df, 0, HoldPlan(sell="t_plus_2_open"), FREE, RULE)
    assert r.exit_idx == 2 and r.exit_price == 10.6
    assert r.reason == "t2_open"


def test_signal_close_buy_at_first_bar_uses_entry_price_as_prev_close():
    """signal_idx=0 且卖点也在第 0 根时，`exit_idx-1` 为负——不得绕到最后一根取前收。"""
    # 若误取最后一根收盘（100）当前收，跌停价变 90，第 0 根的 10.0 会被当成跌停而顺延
    df = _bars([(10.0, 10.0, 10.0, 10.0), (100.0, 100.0, 100.0, 100.0)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_close"), FREE, RULE)
    assert r.status == "ok" and r.reason == "t1_close"
    assert r.exit_idx == 0 and r.exit_price == 10.0


def test_missing_exit_bar_returns_no_exit():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4)])
    r = settle_hold_trade(df, 0, HoldPlan(sell="t_plus_2_open"), FREE, RULE)
    assert r.status == "no_exit"


def test_zero_defer_forces_close_on_limit_down():
    # max_defer=0 = 到期就走：收盘跌停也按当日收盘成交
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.0, 9.0), (9.0, 9.1, 8.0, 8.9)])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=0), FREE, RULE)
    assert r.exit_idx == 1 and r.exit_price == 9.0
    assert r.reason == "t1_close_forced" and r.deferred == 0


def test_unsold_when_data_ends_during_defer():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.0, 9.0)])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.status == "deferred_unsold" and r.reason == "deferred_unsold"
    assert r.deferred == 0 and r.entry_idx == 1


def test_signal_limit_up_buy_uses_exact_ratio():
    # 信号日收盘 10.0 → 主板涨停价 11.0（精确比例，不是阈值合同的 9.5%）
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.9, 10.2), (10.3, 10.4, 10.1, 10.2)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_limit_up", sell="next_open"), FREE, RULE)
    assert r.entry_idx == 0 and r.entry_price == 11.0
    assert r.exit_idx == 1 and r.exit_price == 10.0


def test_next_open_sell_is_relative_to_entry_bar():
    # buy=signal_close 时 next_open 卖在信号次根，不是 signal+2
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.5, 10.6, 10.4, 10.5), (11.0, 11.1, 10.9, 11.0)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_close", sell="next_open"), FREE, RULE)
    assert r.entry_idx == 0 and r.exit_idx == 1 and r.exit_price == 10.5


def test_net_uses_cost_model():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.4, 10.6, 10.2, 10.5)])
    cm = CostModel(commission=0.00025, stamp_sell=0.0005, slippage=0.0005)
    r = settle_hold_trade(df, 0, HoldPlan(), cm, RULE)
    assert r.net_ret == pytest.approx(cm.round_trip(10.0, 10.4))
