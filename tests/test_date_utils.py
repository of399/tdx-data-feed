"""tdxfeed.date_utils 单元测试（自包含，无需 pytest 亦可运行）。

运行：
    python3 tests/test_date_utils.py
    python3 -m pytest tests/test_date_utils.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from tdxfeed.date_utils import parse_date, parse_date_series, to_date_str


def test_series_int():
    s = pd.Series([20260930, 20261001])
    out = parse_date(s)
    assert pd.api.types.is_datetime64_any_dtype(out)
    assert str(out.iloc[0].date()) == "2026-09-30"
    assert str(out.iloc[1].date()) == "2026-10-01"


def test_series_datetime():
    s = pd.Series(pd.to_datetime(["2026-09-30", "2026-10-01"]))
    out = parse_date(s)
    assert out.iloc[0] == pd.Timestamp("2026-09-30")


def test_series_string():
    s = pd.Series(["20260930", "20261001"])
    out = parse_date(s)
    assert str(out.iloc[0].date()) == "2026-09-30"


def test_scalar_int():
    assert parse_date(20260930) == pd.Timestamp("2026-09-30")


def test_scalar_numpy_int():
    assert parse_date(pd.Series([20260930]).iloc[0]) == pd.Timestamp("2026-09-30")


def test_scalar_timestamp():
    assert parse_date(pd.Timestamp("2026-09-30 10:00:00")) == pd.Timestamp("2026-09-30")


def test_to_date_str():
    assert to_date_str(20260930) == "2026-09-30"
    assert to_date_str(pd.Timestamp("2026-09-30")) == "2026-09-30"
    assert to_date_str(12345) == "12345"
    assert to_date_str(pd.Series([20260930]).iloc[0]) == "2026-09-30"


def test_parse_date_series_alias():
    s = pd.Series([20260930])
    assert (parse_date_series(s) == parse_date(s)).all()


if __name__ == "__main__":
    test_series_int()
    test_series_datetime()
    test_series_string()
    test_scalar_int()
    test_scalar_numpy_int()
    test_scalar_timestamp()
    test_to_date_str()
    test_parse_date_series_alias()
    print("ALL date_utils TESTS PASSED")
