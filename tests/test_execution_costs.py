"""费用口径只有两个合同：比例式（无最低佣金）与金额式（有最低佣金）。"""
from __future__ import annotations

import pytest

from tdxfeed.execution.costs import DABAN_COSTS, PLAN_E_COSTS, CostModel


def test_round_trip_matches_plan_e_formula_exactly():
    entry, exit_price = 10.0, 13.0
    fee, slip = 0.0003, 0.0010
    expected = (exit_price * (1 - slip) * (1 - fee)
                / (entry * (1 + slip) * (1 + fee)) - 1)
    assert CostModel(commission=fee, slippage=slip).round_trip(entry, exit_price) == expected


def test_round_trip_without_cost_is_gross():
    assert CostModel(commission=0.0, slippage=0.0).round_trip(10.0, 11.0) == pytest.approx(0.1)


def test_round_trip_refuses_min_commission():
    with pytest.raises(ValueError):
        CostModel(min_commission=5.0).round_trip(10.0, 11.0)


def test_sell_applies_stamp_only_on_sell_side():
    cm = CostModel(commission=0.0, stamp_sell=0.001, slippage=0.0)
    assert cm.buy(10.0, 1000).fee == 0.0
    assert cm.sell(10.0, 1000).tax == 10.0


def test_cash_pnl_reproduces_paper_net_actual():
    # paper._net_actual 的公式：base=买入金额（未含滑点），佣金双边各有最低值
    cm = CostModel(commission=2.5e-4, min_commission=5.0, stamp_sell=5e-4, slippage=0.0)
    fill, sell, shares = 10.0, 11.0, 1000
    buy_amt, sell_amt = fill * shares, sell * shares
    commission = max(5.0, buy_amt * 2.5e-4) + max(5.0, sell_amt * 2.5e-4)
    expected = (sell_amt - buy_amt - commission - sell_amt * 5e-4) / buy_amt
    assert cm.cash_pnl(fill, sell, shares) == pytest.approx(expected, abs=1e-12)


def test_cash_pnl_uses_fill_not_slipped_price_as_base():
    cm = CostModel(commission=0.0, slippage=0.001)
    pnl = cm.cash_pnl(10.0, 11.0, 1000)
    assert pnl == pytest.approx((11.0 * 0.999 - 10.0 * 1.001) / 10.0)


def test_presets():
    assert PLAN_E_COSTS.round_trip(10.0, 10.0) < 0
    assert DABAN_COSTS.stamp_sell == 5e-4


def test_plan_e_costs_have_no_stamp_tax():
    # 回测与 sim_live 共用这一个常量：印花税必须显式钉 0，别靠 dataclass 默认值隐性维持
    assert PLAN_E_COSTS.stamp_sell == 0.0
    assert PLAN_E_COSTS.commission == 0.0003
    assert PLAN_E_COSTS.slippage == 0.0010
