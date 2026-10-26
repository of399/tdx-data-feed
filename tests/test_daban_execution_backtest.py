"""成交感知回测：成本、选信号、成交/顺延与汇总测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tdxfeed.daban.execution_backtest import (
    ExecutionConfig,
    aggregate,
    net_return,
    run_rule,
    select_signals,
    simulate_signals,
)


def test_net_return_applies_sell_costs_only():
    cfg = ExecutionConfig(fee_bps=2.5, stamp_bps=5.0, slip_bps=5.0)
    fee, stamp, slip = 2.5 / 1e4, 5.0 / 1e4, 5.0 / 1e4
    expected = (1 - fee) * (1 - fee - stamp) * (11.0 * (1 - slip)) / 10.0 - 1.0
    assert net_return(10.0, 11.0, cfg) == pytest.approx(expected)

    assert np.isnan(net_return(0.0, 11.0, cfg))
    assert np.isnan(net_return(10.0, np.nan, cfg))
    assert np.isnan(net_return("bad", 11.0, cfg))


def test_select_signals_top_k_and_probability_threshold():
    events = pd.DataFrame({
        "date": ["20260901", "20260901", "20260901", "20260902"],
        "code": ["600003", "600001", "600002", "600001"],
        "seal_prob": [0.70, 0.90, 0.80, 0.60],
    })

    top1 = select_signals(events, ExecutionConfig(top_k_per_day=1))
    assert top1["code"].tolist() == ["600001", "600001"]
    assert top1["date"].tolist() == ["20260901", "20260902"]

    above = select_signals(events, ExecutionConfig(min_prob=0.75))
    assert len(above) == 2
    assert set(above["code"]) == {"600001", "600002"}

    with pytest.raises(ValueError):
        select_signals(events.drop(columns="seal_prob"),
                       ExecutionConfig(top_k_per_day=1))


def _signals() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "code": "600001", "date": "20260901", "touch_bar": 1, "cb_prev": 1,
            "seal_prob": 0.9, "y_sealed_close": 1,
            "fill_w1": True, "entry_w1": 10.0, "fill_gap_w1": 0,
            "next_open": 10.5, "next_close": 10.6,
            "next_lim_dn": 9.0, "next2_open": 10.2,
        },
        {
            "code": "600002", "date": "20260901", "touch_bar": 2, "cb_prev": 0,
            "seal_prob": 0.8, "y_sealed_close": 0,
            "fill_w1": False, "entry_w1": np.nan, "fill_gap_w1": 0,
            "next_open": 10.5, "next_close": 10.6,
            "next_lim_dn": 9.0, "next2_open": 10.2,
        },
        {
            "code": "600003", "date": "20260902", "touch_bar": 3, "cb_prev": 2,
            "seal_prob": 0.7, "y_sealed_close": 0,
            "fill_w1": True, "entry_w1": 11.0, "fill_gap_w1": 1,
            "next_open": 9.9, "next_close": 9.9,
            "next_lim_dn": 9.9, "next2_open": 10.3,
        },
    ])


def test_simulate_signals_keeps_no_fill_and_defers_limit_down_exit():
    cfg = ExecutionConfig()
    trades = simulate_signals(_signals(), cfg)

    assert trades["status"].tolist() == ["ok", "no_fill", "ok"]
    assert np.isnan(trades.loc[1, "net"])
    assert trades.loc[1, "entry"] != trades.loc[1, "entry"]  # NaN

    assert trades.loc[0, "exit"] == pytest.approx(10.5)
    assert trades.loc[0, "gross"] == pytest.approx(0.05)
    assert trades.loc[0, "deferred"] == 0
    assert trades.loc[0, "net"] == pytest.approx(net_return(10.0, 10.5, cfg))

    # T+1 开盘跌停卖不掉 → 顺延 T+2 开盘
    assert trades.loc[2, "deferred"] == 1
    assert trades.loc[2, "exit"] == pytest.approx(10.3)
    assert trades.loc[2, "net"] == pytest.approx(net_return(11.0, 10.3, cfg))


def test_simulate_signals_close_mode_also_defers():
    trades = simulate_signals(
        _signals().iloc[[2]], ExecutionConfig(exit_mode="close"))
    assert trades.loc[0, "exit"] == pytest.approx(10.3)
    assert trades.loc[0, "deferred"] == 1


def test_simulate_signals_marks_invalid_entry():
    signals = _signals().iloc[[0]].copy()
    signals.loc[signals.index[0], "entry_w1"] = np.nan
    trades = simulate_signals(signals, ExecutionConfig())
    assert trades.loc[0, "status"] == "invalid"
    assert np.isnan(trades.loc[0, "net"])


def test_aggregate_reports_fill_rate_and_daily_stats():
    trades = pd.DataFrame({
        "status": ["ok", "ok", "no_fill"],
        "signal_date": ["20260901", "20260902", "20260902"],
        "net": [0.01, -0.01, np.nan],
        "gross": [0.012, -0.008, np.nan],
        "y_sealed_close": [1, 0, 0],
        "deferred": [0, 0, 0],
    })
    out = aggregate(trades, n_signals=3)

    assert out["n_signals"] == 3
    assert out["n_fills"] == 2
    assert out["n_no_fill"] == 1
    assert out["fill_rate"] == pytest.approx(2.0 / 3.0)
    assert out["avg_net"] == pytest.approx(0.0)
    assert out["win_rate"] == pytest.approx(0.5)
    assert out["profit_factor"] == pytest.approx(1.0)
    assert out["daily_mean"] == pytest.approx(0.0)
    assert np.isnan(out["t"])  # 只有两个有效交易日
    assert out["total_return"] == pytest.approx(1.01 * 0.99 - 1.0)
    assert out["max_drawdown"] < 0


def test_aggregate_empty_fills():
    trades = pd.DataFrame({
        "status": ["no_fill"],
        "signal_date": ["20260901"],
        "net": [np.nan],
        "gross": [np.nan],
        "y_sealed_close": [0],
        "deferred": [0],
    })
    out = aggregate(trades, n_signals=1)
    assert out["n_fills"] == 0
    assert np.isnan(out["avg_net"])
    assert out["fill_rate"] == pytest.approx(0.0)


def test_run_rule_returns_summary_and_trade_rows():
    events = pd.DataFrame({
        "date": ["20260901", "20260901", "20260902"],
        "code": ["600001", "600002", "600003"],
        "seal_prob": [0.9, 0.8, 0.7],
        "touch_bar": [1, 2, 3],
        "cb_prev": [1, 0, 2],
        "y_sealed_close": [1, 0, 0],
        "fill_w1": [False, False, False],
        "entry_w1": [np.nan] * 3,
        "next_open": [10.0] * 3,
        "next_close": [10.0] * 3,
        "next_lim_dn": [9.0] * 3,
        "next2_open": [10.0] * 3,
    })
    summary, trades = run_rule(
        events, "top1", ExecutionConfig(top_k_per_day=1))

    assert summary["rule"] == "top1"
    assert summary["n_signals"] == 2
    assert summary["n_fills"] == 0
    assert summary["fill_rate"] == pytest.approx(0.0)
    assert summary["config"]["top_k_per_day"] == 1
    assert len(trades) == 2
