"""内核只认 open/high/low/close/volume；_adj 后缀与 date 类型在入口归一。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.bars import as_bars, date_str
from tdxfeed.execution.result import TradeResult


def test_as_bars_renames_adj_suffix():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=2),
        "open_adj": [1.0, 2.0], "high_adj": [1.1, 2.1],
        "low_adj": [0.9, 1.9], "close_adj": [1.0, 2.0],
        "volume_adj": [100, 200],
    })
    out = as_bars(df, suffix="_adj")
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert out["close"].tolist() == [1.0, 2.0]


def test_adj_suffix_requires_all_five_columns():
    """suffix 非空时不回退到裸列：缺一个 _adj 列就抛 KeyError，避免混用复权/不复权。"""
    partial = pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=2),
        "open_adj": [2.0, 3.0], "high_adj": [2.1, 3.1],
        "low_adj": [1.9, 2.9], "close_adj": [2.0, 3.0],
        "volume": [10, 20],
    })
    with pytest.raises(KeyError) as excinfo:
        as_bars(partial, suffix="_adj")
    assert "volume_adj" in str(excinfo.value)

    full = pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=2),
        "open": [20.0, 30.0], "high": [21.0, 31.0],
        "low": [19.0, 29.0], "close": [20.0, 30.0], "volume": [2000, 3000],
        "open_adj": [2.0, 3.0], "high_adj": [2.1, 3.1],
        "low_adj": [1.9, 2.9], "close_adj": [2.0, 3.0], "volume_adj": [10, 20],
    })
    out = as_bars(full, suffix="_adj")
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert out["open"].tolist() == [2.0, 3.0]
    assert out["high"].tolist() == [2.1, 3.1]
    assert out["low"].tolist() == [1.9, 2.9]
    assert out["close"].tolist() == [2.0, 3.0]
    assert out["volume"].tolist() == [10.0, 20.0]
    assert out["date"].tolist() == list(pd.date_range("2026-01-05", periods=2))


def test_as_bars_rejects_missing_column():
    df = pd.DataFrame({"date": [1], "open": [1.0], "high": [1.0], "low": [1.0]})
    with pytest.raises(KeyError):
        as_bars(df)


def test_as_bars_keeps_row_order_and_index_reset():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=3),
        "open": [3.0, 1.0, 2.0], "high": [3.0, 1.0, 2.0],
        "low": [3.0, 1.0, 2.0], "close": [3.0, 1.0, 2.0], "volume": [1, 1, 1],
    }, index=[7, 8, 9])
    out = as_bars(df)
    assert list(out.index) == [0, 1, 2]
    assert out["open"].tolist() == [3.0, 1.0, 2.0]


def test_date_str_handles_date32_and_timestamp():
    assert date_str(pd.Timestamp("2026-01-05")) == "2026-01-05"
    assert date_str("20260105") == "2026-01-05"


def test_trade_result_defaults():
    r = TradeResult(entry_idx=1, entry_date="2026-01-02", entry_price=10.0,
                    exit_idx=3, exit_date="2026-01-06", exit_price=13.0,
                    reason="TP", gross_ret=0.3, net_ret=0.29)
    assert r.status == "ok"
    assert r.deferred == 0
    assert r.daily == ()
    assert r.max_gain == 0.0 and r.max_loss == 0.0
