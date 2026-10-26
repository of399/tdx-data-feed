"""交易前风险闸门：仓位上限、过滤、亏损暂停与状态落盘测试。"""
from __future__ import annotations

import pytest

from tdxfeed.daban.risk_gateway import (
    Order,
    RiskConfig,
    RiskState,
    check_order,
    load_state,
    parse_cap,
    record_entry,
    record_result,
    roll_state,
    save_state,
    week_key,
)

DAY = "2026-10-22"


def _state(**kw) -> RiskState:
    base = {
        "equity": 100_000.0,
        "day": DAY,
        "day_start_equity": 100_000.0,
        "week": week_key(DAY),
        "week_start_equity": 100_000.0,
    }
    base.update(kw)
    return RiskState(**base)


def _order(**kw) -> Order:
    base = {
        "code": "600001",
        "side": "buy",
        "price": 10.0,
        "shares": 1000,
        "stop_price": 9.5,
        "avg_amount_5d": 1e8,
    }
    base.update(kw)
    return Order(**base)


def test_week_key_and_parse_cap():
    assert week_key("2026-10-22") == "2026-43"
    assert week_key("2026-10-23") == "2026-43"
    assert parse_cap("0-10%") == pytest.approx(0.10)
    assert parse_cap("≤30%") == pytest.approx(0.30)
    assert parse_cap("20-25%") == pytest.approx(0.25)
    assert parse_cap("0%") == pytest.approx(0.0)
    assert parse_cap("150%") == pytest.approx(1.0)
    assert parse_cap("—") is None
    assert parse_cap(None) is None
    assert parse_cap("abc") is None


def test_default_order_approved_and_capped_by_phase_single():
    decision = check_order(
        _order(), _state(), RiskConfig(), phase="震荡市", date_str=DAY)

    assert decision.approved is True
    assert decision.approved_shares == 1000
    assert decision.caps["risk"] == 2000
    assert decision.caps["position"] == 2000
    assert decision.caps["phase_single"] == 1000
    assert decision.caps["phase_total"] == 3000
    assert decision.caps["liquidity"] == 100_000
    assert decision.risk_pct == pytest.approx(0.005)
    assert "通过" in decision.reasons


def test_risk_budget_caps_shares():
    decision = check_order(
        _order(shares=1000, stop_price=8.0),
        _state(), RiskConfig(), phase="震荡市", date_str=DAY)

    assert decision.approved is True
    assert decision.caps["risk"] == 500
    assert decision.approved_shares == 500
    assert decision.risk_amount == pytest.approx(1000.0)
    assert decision.risk_pct == pytest.approx(0.01)


def test_position_cap_binds_in_hot_phase():
    decision = check_order(
        _order(shares=4000), _state(), RiskConfig(),
        phase="高潮期", date_str=DAY)

    assert decision.approved is True
    assert decision.caps["phase_single"] == 2500
    assert decision.caps["position"] == 2000
    assert decision.approved_shares == 2000


def test_exposure_cap_binds_with_existing_positions():
    decision = check_order(
        _order(shares=3000), _state(exposure=69_000.0), RiskConfig(),
        phase="高潮期", date_str=DAY)

    assert decision.approved is True
    assert decision.caps["exposure"] == 100
    assert decision.caps["phase_total"] == 1100
    assert decision.approved_shares == 100


def test_daily_loss_limit_rejects_new_buys():
    state = _state(equity=97_999.0)
    decision = check_order(_order(), state, RiskConfig(), date_str=DAY)
    assert decision.approved is False
    assert decision.approved_shares == 0
    assert "单日亏损" in decision.reasons[-1]


def test_weekly_loss_limit_rejects_new_buys():
    state = _state(equity=94_000.0, day_start_equity=94_000.0)
    decision = check_order(_order(), state, RiskConfig(), date_str=DAY)
    assert decision.approved is False
    assert "单周亏损" in decision.reasons[-1]


def test_max_new_trades_per_day():
    decision = check_order(
        _order(), _state(trades_today=3), RiskConfig(), date_str=DAY)
    assert decision.approved is False
    assert "当日新开仓" in decision.reasons[-1]


def test_pause_blocks_until_expiry_then_rolls_off():
    paused = _state(paused_until="2026-10-23")
    blocked = check_order(_order(), paused, RiskConfig(), date_str="2026-10-23")
    assert blocked.approved is False
    assert "连亏暂停" in blocked.reasons[-1]

    resumed = check_order(_order(), paused, RiskConfig(), date_str="2026-10-24")
    assert resumed.approved is True
    assert paused.paused_until == ""


@pytest.mark.parametrize("order_kw,reason", [
    ({"is_st": True}, "ST"),
    ({"is_new": True}, "新股"),
    ({"is_open_limit": True}, "一字"),
    ({"price": 1.99, "stop_price": 1.90}, "价格"),
    ({"avg_amount_5d": 4e7}, "均额"),
    ({"stop_price": 10.0}, "止损价"),
    ({"stop_price": 10.5}, "止损价"),
    ({"side": "hold"}, "未知方向"),
])
def test_order_filters(order_kw, reason):
    decision = check_order(
        _order(**order_kw), _state(), RiskConfig(), date_str=DAY)
    assert decision.approved is False
    assert decision.approved_shares == 0
    assert reason in decision.reasons[-1]


def test_sell_is_not_blocked_by_open_limits():
    decision = check_order(
        _order(side="sell", shares=500, stop_price=0.0, is_st=True, is_new=True),
        _state(), RiskConfig(), date_str=DAY)
    assert decision.approved is True
    assert decision.approved_shares == 500
    assert "卖出" in decision.reasons[-1]


def test_bear_phase_has_zero_total_position():
    decision = check_order(
        _order(), _state(), RiskConfig(), phase="熊市", date_str=DAY)
    assert decision.approved is False
    assert decision.caps["phase_total"] == 0


def test_record_entry_and_result_trigger_pause():
    state = _state()
    record_entry(state, DAY)
    assert state.trades_today == 1

    cfg = RiskConfig()
    for _ in range(3):
        record_result(state, -1000.0, DAY, cfg)
    assert state.equity == pytest.approx(97_000.0)
    assert state.consecutive_losses == 3
    assert state.paused_until == "2026-10-23"

    record_result(state, 500.0, "2026-10-23", cfg)
    assert state.equity == pytest.approx(97_500.0)
    assert state.consecutive_losses == 0


def test_roll_state_resets_day_week_and_pause():
    state = RiskState(
        equity=110_000.0, day="2026-10-21", day_start_equity=100_000.0,
        week="2026-42", week_start_equity=100_000.0,
        consecutive_losses=2, paused_until="2026-10-22",
        trades_today=2, exposure=5_000.0)

    roll_state(state, "2026-10-23")
    assert state.day == "2026-10-23"
    assert state.day_start_equity == pytest.approx(110_000.0)
    assert state.week == "2026-43"
    assert state.week_start_equity == pytest.approx(110_000.0)
    assert state.trades_today == 0
    assert state.paused_until == ""


def test_state_roundtrip_and_missing_file(tmp_path):
    path = tmp_path / "risk" / "state.json"
    state = _state(equity=103_500.0, exposure=12_000.0, trades_today=1)
    save_state(state, path)

    loaded = load_state(path)
    assert loaded == state

    fresh = load_state(tmp_path / "missing.json", capital=50_000.0)
    assert fresh.equity == pytest.approx(50_000.0)
    assert fresh.day_start_equity == pytest.approx(50_000.0)
    assert fresh.week_start_equity == pytest.approx(50_000.0)
