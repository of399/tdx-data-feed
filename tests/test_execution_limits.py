"""两套合同必须分开：判定阈值留 0.5% 缓冲，挂单价用精确比例取整到分。"""
from __future__ import annotations

from decimal import Decimal

from tdxfeed.execution.limits import (
    LimitRule,
    is_limit_up_ret,
    is_sealed,
    limit_down_price,
    limit_ratio,
    limit_threshold,
    limit_up_price,
    market_of,
    open_is_limit_up,
)


def test_market_classification():
    assert market_of("sh600519") == "main"
    assert market_of("sz000001") == "main"
    assert market_of("sz300750") == "gem"
    assert market_of("sh688981") == "gem"
    assert market_of("bj920001") == "bse"
    assert market_of("830799") == "bse"


def test_exact_ratio_is_decimal():
    assert limit_ratio("600519") == Decimal("0.10")
    assert limit_ratio("300750") == Decimal("0.20")
    assert limit_ratio("920001") == Decimal("0.30")


def test_main_st_ratio_follows_merger_date():
    assert limit_ratio("600519", is_st=True, on_date="2026-07-03") == Decimal("0.05")
    assert limit_ratio("600519", is_st=True, on_date="2026-07-06") == Decimal("0.10")
    assert limit_ratio("600519", is_st=True) == Decimal("0.10")


def test_gem_st_is_always_20pct():
    assert limit_ratio("300750", is_st=True, on_date="2020-01-02") == Decimal("0.20")


def test_threshold_equals_ratio_minus_buffer():
    assert limit_threshold("600519") == 0.095
    assert limit_threshold("300750") == 0.195
    assert limit_threshold("920001") == 0.295
    assert limit_threshold("600519", is_st=True, on_date="2026-07-03") == 0.045


def test_prices_round_to_cent_half_up():
    assert limit_up_price(10.005, Decimal("0.10")) == 11.01
    assert limit_down_price(10.00, Decimal("0.10")) == 9.0
    assert limit_up_price(0, Decimal("0.10")) == 0.0


def test_open_limit_up_uses_threshold_contract():
    assert open_is_limit_up(11.0, 10.0, "600519") is True
    assert open_is_limit_up(10.94, 10.0, "600519") is False   # 9.4% < 9.5%
    # 9.5%：过阈值（9.5%−0.1%），未到精确比例 10%。
    # 比例口径下这里返回 False，只有 limit_threshold 口径才能通过。
    assert open_is_limit_up(10.95, 10.0, "600519") is True
    assert open_is_limit_up(11.93, 10.0, "300750") is False   # 19.3% < 19.4%
    assert open_is_limit_up(12.0, 10.0, "300750") is True     # 20% 板 +20% 即涨停


def test_limit_up_ret_uses_tolerance():
    assert is_limit_up_ret(0.094, 0.095) is True    # 容差 0.1%：0.094 ≥ 0.094
    assert is_limit_up_ret(0.093, 0.095) is False   # 低于 0.094，容差救不了


def test_sealed_uses_exact_price_contract():
    assert is_sealed(10.995, 11.0) is True    # 正好卡在容差内（半分钱）
    assert is_sealed(10.99, 11.0) is False    # 差一分钱，超出半分钱容差


def test_limit_rule_bundles_code_and_name():
    rule = LimitRule(code="600519", is_st=True)
    assert rule.up_price(10.0) == 11.0
    assert rule.down_price(10.0, on_date="2026-07-03") == 9.5
    assert rule.is_open_limit_up(11.0, 10.0, on_date="2026-07-06") is True
    assert rule.is_sealed(10.995, 10.0, on_date="2026-07-06") is True
