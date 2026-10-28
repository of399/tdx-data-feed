"""MCP 回测的阶梯必须与内核同源，但 max_hold/cooldown 仍归它自己。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("mcp_backtest_under_test",
                                                  ROOT / "mcp" / "servers" / "backtest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _frame(opens=None, highs=None, lows=None, closes=None, n=6):
    opens = opens or [10.0] * n
    highs = highs or [10.1] * n
    lows = lows or [9.9] * n
    closes = closes or [10.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n),
        "open_adj": opens, "high_adj": highs, "low_adj": lows,
        "close_adj": closes, "volume": [1e6] * n,
    })


def _run(mod, df, *, exits=None, max_hold=None):
    n = len(df)
    entries = np.zeros(n, dtype=bool)
    entries[0] = True
    exits = exits if exits is not None else np.zeros(n, dtype=bool)
    # 本模块的 stop_loss 是正数（0.10，校验区间 0.001..0.95），止损线 = entry*(1-stop_loss)
    return mod._simulate(df, entries, exits, 0, n - 1, fee_bps=0.0, stamp_bps=0.0,
                         slip_bps=0.0, take_profit=0.30, stop_loss=0.10,
                         max_hold=max_hold, cooldown=0)


def test_exit_ladder_delegates_to_kernel(monkeypatch):
    mod = _load()
    import tdxfeed.execution.single as single

    seen = []
    original = single.check_exit

    def spy(*args, **kwargs):
        seen.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(single, "check_exit", spy)

    df = _frame(lows=[9.9, 9.9, 8.5, 9.9, 9.9, 9.9],
                highs=[10.1, 10.1, 10.5, 10.1, 10.1, 10.1])
    sim = _run(mod, df)
    assert seen, "MCP 回测的止盈止损没有走内核 check_exit"
    assert sim["trades"][0]["exit_reason"] == "stop_loss"
    assert sim["trades"][0]["exit_price"] == 9.0


@pytest.mark.parametrize("case,opens,highs,lows,expect_reason,expect_price", [
    # 入场 i=1 @10.0；止损 9.0 / 止盈 13.0
    ("stop_gap", [10, 10, 8.5, 10, 10, 10], [10.1, 10.1, 8.8, 10.1, 10.1, 10.1],
     [9.9, 9.9, 8.0, 9.9, 9.9, 9.9], "stop_gap", 8.5),
    ("take_profit_gap", [10, 10, 14.0, 10, 10, 10], [10.1, 10.1, 14.2, 10.1, 10.1, 10.1],
     [9.9, 9.9, 13.8, 9.9, 9.9, 9.9], "take_profit_gap", 14.0),
    ("stop_loss", [10] * 6, [10.1] * 6, [9.9, 9.9, 8.5, 9.9, 9.9, 9.9], "stop_loss", 9.0),
    ("take_profit", [10] * 6, [10.1, 10.1, 13.5, 10.1, 10.1, 10.1], [9.9] * 6,
     "take_profit", 13.0),
])
def test_price_ladder_reasons_unchanged(case, opens, highs, lows,
                                        expect_reason, expect_price):
    mod = _load()
    sim = _run(mod, _frame(opens=opens, highs=highs, lows=lows))
    t = sim["trades"][0]
    assert t["exit_reason"] == expect_reason, case
    assert t["exit_price"] == expect_price, case


def test_max_hold_stays_outside_kernel_ladder():
    mod = _load()
    sim = _run(mod, _frame(), max_hold=2)
    t = sim["trades"][0]
    assert t["exit_reason"] == "max_hold"
    assert t["hold_days"] == 2


def test_signal_exit_and_end_exit_reasons_unchanged():
    mod = _load()
    exits = np.zeros(6, dtype=bool)
    exits[2] = True
    assert _run(mod, _frame(), exits=exits)["trades"][0]["exit_reason"] == "signal"
    assert _run(mod, _frame())["trades"][0]["exit_reason"] == "end"


def test_gap_up_beyond_take_profit_plus_intraday_stop_settles_loss_side():
    """开盘已在止盈线之上、日内又跌破止损线的宽幅 K 线。

    迁移前的 MCP 实现先判「开盘越线」→ take_profit_gap@开盘（+40%），内核按
    sl_first 记亏损侧（-10%）。这是本任务接受的唯一口径变化：方向是保守
    （盈利笔记成亏损笔），与 AGENTS.md「同一根 K 线双触发时结算亏损侧」一致。
    不要为了对齐旧行为在内核之上再加一层「开盘越线优先」的分叉。
    """
    mod = _load()
    df = _frame(opens=[10, 10, 14.0, 10, 10, 10],
                highs=[10.1, 10.1, 14.2, 10.1, 10.1, 10.1],
                lows=[9.9, 9.9, 8.5, 9.9, 9.9, 9.9])
    t = _run(mod, df)["trades"][0]
    assert t["exit_reason"] == "stop_loss"
    assert t["exit_price"] == 9.0
