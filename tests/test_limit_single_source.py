"""三份涨跌幅实现必须同源：改规则只改 tdxfeed/execution/limits.py 一处。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tdxfeed import features_5d
from tdxfeed.daban import limit_price
from tdxfeed.execution import limits


def test_features_get_limit_delegates():
    for code in ("600519", "sz300750", "sh688981", "bj920001", "830799"):
        assert features_5d.get_limit(code) == limits.limit_threshold(code)


def test_features_get_limit_keeps_st_branch():
    assert features_5d.get_limit("600519", "*ST某某", "2026-07-03") == 0.045
    assert features_5d.get_limit("600519", "*ST某某", "2026-07-06") == 0.095


def test_daban_limit_ratio_delegates():
    assert limit_price.limit_ratio("600519") == limits.limit_ratio("600519")
    assert limit_price.limit_ratio("600519", "*ST某某", "2026-07-03") == Decimal("0.05")


def test_daban_price_helpers_delegate():
    assert limit_price.limit_up_price(10.0, Decimal("0.10")) == limits.limit_up_price(10.0, Decimal("0.10"))
    assert limit_price.limit_down_price(10.0, Decimal("0.10")) == limits.limit_down_price(10.0, Decimal("0.10"))


def test_quant_get_limit_delegates():
    from quant.multifactor import factors
    assert factors.get_limit("300750") == pytest.approx(0.195)
    assert factors.get_limit("600519") == pytest.approx(0.095)


def test_features_get_limit_bse_without_prefix():
    """无前缀的北交所代码也按 30% 档——旧实现只有 bj/920 前缀才判北交所。"""
    for code in ("830799", "430047", "871981"):
        assert features_5d.get_limit(code) == pytest.approx(0.295)


def test_market_of_and_prefix_agree():
    for code in ("600519", "000001", "300750", "688981", "920001"):
        assert limit_price.market_of(code) == limits.market_of(code)
