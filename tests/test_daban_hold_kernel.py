"""daban 三处结算同源：买不进、卖出时点、成本公式只有一套。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.daban import topk_pnl
from tdxfeed.daban.execution_backtest import ExecutionConfig
from tdxfeed.daban.execution_backtest import net_return as eb_net_return
from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.hold import HoldPlan, settle_hold_trade
from tdxfeed.execution.limits import LimitRule


def _bars(n: int = 5) -> pd.DataFrame:
    rows = [(10.0, 10.2, 9.8, 10.0), (10.0, 10.5, 9.9, 10.4),
            (10.6, 10.8, 10.5, 10.7), (10.7, 10.9, 10.6, 10.8),
            (10.8, 11.0, 10.7, 10.9)][:n]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n),
        "open": [r[0] for r in rows], "close": [r[3] for r in rows],
        "volume": [1e6] * n,
    })


def test_topk_ret_net_uses_round_trip():
    """#10 的成本公式与内核同一份：旧式 (1-fee)^2 与 round_trip 差约 6.3e-8。"""
    out = topk_pnl.simulate_trade(_bars(), 0, "600000")
    assert out["status"] == "ok"
    cm = CostModel(commission=2.5e-4, stamp_sell=5e-4, slippage=5e-4)
    assert out["ret_net"] == pytest.approx(cm.round_trip(10.0, 10.6), abs=1e-12)


def test_topk_exit_offset_beyond_t_plus_2():
    """--exit-offset 允许 >2：内核用 sell_offset 表达，不能退化成固定 T+2。"""
    out = topk_pnl.simulate_trade(_bars(), 0, "600000", exit_offset=3)
    assert out["status"] == "ok"
    assert out["exit_date"] == "2026-01-04"
    assert out["exit_price"] == pytest.approx(10.7)


def test_topk_delegates_to_hold_kernel(monkeypatch):
    """把判定改回内联时本用例红：它钉的是调用点，不是参数对象。"""
    seen = {}
    real = topk_pnl.settle_hold_trade

    def spy(bars, signal_idx, plan, costs, rule):
        seen["plan"] = plan
        return real(bars, signal_idx, plan, costs, rule)

    monkeypatch.setattr(topk_pnl, "settle_hold_trade", spy)
    topk_pnl.simulate_trade(_bars(), 0, "600000", exit_offset=3)
    assert seen["plan"].buy == "next_open"
    assert seen["plan"].sell == "t_plus_2_open"
    assert seen["plan"].sell_offset == 3


def test_suspended_entry_takes_priority_over_missing_exit_bar():
    """锚定判定顺序：先判入场根停牌、再判出场根缺失（旧实现先判 no_exit）。

    signal_idx=1 时入场根是末根（open=0、volume=0 ⇒ 停牌），而
    t_plus_2_open/sell_offset=2 的 exit_idx=3 已越界——两条状态同时成立时
    内核必须报 suspended。
    """
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=3),
        "open": [10.0, 10.0, 0.0], "close": [10.0, 10.4, 0.0],
        "volume": [1e6, 1e6, 0.0],
    })
    costs = CostModel(commission=0.0, slippage=0.0)
    r = settle_hold_trade(df, 1, HoldPlan(buy="next_open", sell="t_plus_2_open",
                                          sell_offset=2), costs, LimitRule("600000"))
    assert r.status == "suspended"


def test_execution_backtest_keeps_limit_buy_contract():
    """#9 数值不变：限价买入不打滑点，只有卖出侧吃 slip。"""
    cfg = ExecutionConfig(fee_bps=2.5, stamp_bps=5.0, slip_bps=5.0)
    fee, stamp, slip = 2.5 / 1e4, 5.0 / 1e4, 5.0 / 1e4
    expected = (1 - fee) * (1 - fee - stamp) * (10.6 * (1 - slip)) / 10.0 - 1.0
    assert eb_net_return(10.0, 10.6, cfg) == pytest.approx(expected, rel=0, abs=1e-15)
