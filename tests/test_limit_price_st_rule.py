"""主板 ST 涨跌幅 2026-07-06 并轨 10% 的规则切换，以及涨停价四舍五入的分位精度。

规则依据：上证发〔2026〕41 号《上海证券交易所交易规则（2026 年修订）》第 3.3.13 条，
2026-07-06 起主板风险警示股票涨跌幅由 5% 调整为 10%；科创板/创业板 ST 维持 20%，北交所维持 30%。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tdxfeed.daban.limit_price import (
    MAIN_ST_RULE_CHANGE_DATE,
    board_label,
    limit_ratio,
    limit_up_price,
)


@pytest.mark.parametrize(
    "on_date, expected",
    [
        ("20260703", Decimal("0.05")),   # 切换前最后一个交易日附近
        ("2026-07-04", Decimal("0.05")),
        (MAIN_ST_RULE_CHANGE_DATE, Decimal("0.10")),
        ("20261022", Decimal("0.10")),
        (None, Decimal("0.10")),         # 不传日期按现行规则
    ],
)
def test_main_st_ratio_is_date_aware(on_date, expected):
    assert limit_ratio("603838", "*ST四通", on_date=on_date) == expected
    assert limit_ratio("000078", "ST海王", on_date=on_date) == expected


def test_non_main_st_bands_did_not_change():
    assert limit_ratio("301030", "*ST仕净", on_date="20260601") == Decimal("0.20")
    assert limit_ratio("301030", "*ST仕净", on_date="20261022") == Decimal("0.20")
    assert limit_ratio("688121", "*ST卓然", on_date="20261022") == Decimal("0.20")
    assert limit_ratio("920578", "*ST某北证", on_date="20261022") == Decimal("0.30")


def test_plain_main_board_unaffected_by_date():
    for d in ("20200101", "20260703", "20261022", None):
        assert limit_ratio("600148", "长春一东", on_date=d) == Decimal("0.10")


@pytest.mark.parametrize(
    "prev, ratio, expected",
    [
        # 半分钱进位：银行家舍入会算错，交易所是四舍五入
        (2.10, "0.05", 2.21),
        (9.57, "0.05", 10.05),
        (1.78, "0.05", 1.87),
        (12.25, "0.05", 12.86),
        (16.49, "0.05", 17.31),
        (13.09, "0.05", 13.74),
        (2.45, "0.10", 2.70),      # 低价股 10%：+10.204%
        (12.19, "0.10", 13.41),
        (16.89, "0.10", 18.58),
        (10.19, "0.10", 11.21),
        (17.91, "0.10", 19.70),
        (16.60, "0.20", 19.92),
        (27.07, "0.20", 32.48),    # 20cm 向下取整的分位
        (95.00, "0.20", 114.00),
    ],
)
def test_limit_up_price_rounds_half_up_to_cents(prev, ratio, expected):
    assert limit_up_price(prev, Decimal(ratio)) == pytest.approx(expected)


def test_board_label_follows_the_date():
    assert board_label("603838", "*ST四通", on_date="20260601") == "主板ST5%"
    assert board_label("603838", "*ST四通", on_date="20261022") == "主板ST10%"
    assert board_label("603838", "*ST四通") == "主板ST10%"
    assert board_label("600148", "长春一东") == "主板10%"
    assert board_label("300839", "博汇股份") == "双创20%"
