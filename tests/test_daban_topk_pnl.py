"""daban Top-K 逐笔 PnL 回测测试。

口径：T 日收盘出信号 → T+1 开盘买入 → T+2 开盘卖出（A 股 T+1 最早卖点）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.daban.topk_pnl import aggregate, net_return, simulate_trade


def _bars(opens, closes=None, volumes=None, factors=None):
    n = len(opens)
    closes = closes or opens
    volumes = volumes or [100.0] * n
    data = {
        "date": [f"2026-10-{12 + i:02d}" for i in range(n)],
        "open": opens,
        "close": closes,
        "volume": volumes,
    }
    if factors is not None:
        data["adj_factor"] = factors
    return pd.DataFrame(data)


def test_net_return_matches_cost_formula():
    fee, stamp, slip = 2.5 / 1e4, 5.0 / 1e4, 5.0 / 1e4
    got = net_return(10.0, 10.0, fee_bps=2.5, stamp_bps=5.0, slip_bps=5.0)
    expected = (1 - fee) * (1 - fee - stamp) * (10.0 * (1 - slip)) / (10.0 * (1 + slip)) - 1
    assert got == pytest.approx(expected, abs=1e-12)


def test_simulate_trade_buys_t1_open_and_sells_t2_open():
    df = _bars([10.0, 10.2, 10.5, 11.0], closes=[10.0, 10.3, 10.6, 11.0])
    r = simulate_trade(df, 0, "600000")
    assert r["status"] == "ok"
    assert r["entry_date"] == "2026-10-13"
    assert r["exit_date"] == "2026-10-14"
    assert r["entry_price"] == pytest.approx(10.2)
    assert r["exit_price"] == pytest.approx(10.5)
    fee, stamp, slip = 2.5 / 1e4, 5.0 / 1e4, 5.0 / 1e4
    expected = (1 - fee) * (1 - fee - stamp) * (10.5 * (1 - slip)) / (10.2 * (1 + slip)) - 1
    assert r["ret_net"] == pytest.approx(expected, abs=1e-12)
    assert r["ret_gross"] == pytest.approx(10.5 / 10.2 - 1, abs=1e-12)


def test_simulate_trade_skips_open_limit():
    df = _bars([10.0, 11.0, 11.5], closes=[10.0, 11.0, 11.5])
    r = simulate_trade(df, 0, "600000")
    assert r["status"] == "skip"
    assert r["reason"] == "open_limit"


def test_simulate_trade_skips_suspended_entry():
    df = _bars([10.0, 10.2, 10.5], closes=[10.0, 10.2, 10.5],
               volumes=[100.0, 0.0, 100.0])
    r = simulate_trade(df, 0, "600000")
    assert r["status"] == "skip"
    assert r["reason"] == "suspended"


def test_simulate_trade_skips_when_exit_missing():
    df = _bars([10.0, 10.2], closes=[10.0, 10.2])
    r = simulate_trade(df, 0, "600000")
    assert r["status"] == "skip"
    assert r["reason"] == "no_exit"


def test_simulate_trade_applies_adj_factor():
    df = _bars([10.0, 10.0, 20.0], closes=[10.0, 10.0, 20.0],
               factors=[1.0, 1.0, 0.5])
    r = simulate_trade(df, 0, "600000")
    assert r["status"] == "ok"
    assert r["ret_gross"] == pytest.approx(0.0, abs=1e-12)
    assert r["ret_net"] < 0


def test_aggregate_equal_weight_and_compounding():
    trades = [
        {"status": "ok", "signal_date": "2026-10-12", "ret_net": 0.10},
        {"status": "ok", "signal_date": "2026-10-13", "ret_net": -0.10},
    ]
    s = aggregate(trades, n_signal_days=3, k=1)
    assert s["n_trades"] == 2
    assert s["fill_rate"] == pytest.approx(2 / 3)
    assert s["total_return"] == pytest.approx(-0.01)
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["max_drawdown"] == pytest.approx(-0.10)
