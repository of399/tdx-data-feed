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


def test_features_get_limit_689_is_gem():
    """689 是科创板 CDR（689009 九号公司）；旧实现只认 688，落到主板 0.095。"""
    assert features_5d.get_limit("689009") == pytest.approx(0.195)
    assert features_5d.get_limit("sh689009") == pytest.approx(0.195)


def test_quant_get_limit_689_is_gem():
    from quant.multifactor import factors
    assert factors.get_limit("689009") == pytest.approx(0.195)


def test_market_of_and_prefix_agree():
    for code in ("600519", "000001", "300750", "688981", "920001"):
        assert limit_price.market_of(code) == limits.market_of(code)
    assert limit_price.exchange_prefix("830799") == "bj"
    assert limit_price.exchange_prefix("sh688981") == "sh"
    assert limit_price.exchange_prefix("300750") == "sz"


def test_features_and_quant_follow_kernel_threshold(monkeypatch):
    """替换内核后入口必须跟着变——否则只是「值碰巧相等」的假委托。"""
    monkeypatch.setattr(limits, "limit_threshold", lambda *a, **k: 0.4242)
    assert features_5d.get_limit("600519") == 0.4242
    from quant.multifactor import factors
    assert factors.get_limit("600519") == 0.4242


def test_daban_helpers_follow_kernel(monkeypatch):
    monkeypatch.setattr(limits, "limit_ratio", lambda *a, **k: Decimal("0.66"))
    assert limit_price.limit_ratio("600519") == Decimal("0.66")
    monkeypatch.setattr(limits, "limit_up_price", lambda *a, **k: 12.34)
    assert limit_price.limit_up_price(10.0) == 12.34
    monkeypatch.setattr(limits, "limit_down_price", lambda *a, **k: 8.88)
    assert limit_price.limit_down_price(10.0) == 8.88


def test_daban_is_sealed_follows_kernel(monkeypatch):
    """内核实现下 1.0 < 2.0-0.005 应判 False；哨兵 True 能抓住本地实现回退。"""
    monkeypatch.setattr(limits, "is_sealed", lambda *a, **k: True)
    assert limit_price.is_sealed(1.0, 2.0) is True


def test_daban_as_date_follows_kernel(monkeypatch):
    """正常解析得 2026-01-01，哨兵 2030-01-01 与本地实现结果不同。"""
    from datetime import date
    monkeypatch.setattr(limits, "as_date", lambda *a, **k: date(2030, 1, 1))
    assert limit_price.as_date("20260101") == date(2030, 1, 1)
