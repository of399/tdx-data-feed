"""跳空穿过止损价时按开盘成交；入场当日仍要判；数据到头仍报超时。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    name = "daban_bt_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "daban_backtest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Trade 是 @dataclass(slots=True)，3.14 下 dataclasses 会回查 sys.modules[cls.__module__]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _frame(closes, opens, highs, lows):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026010{i}" for i in range(1, n + 1)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1e6] * n,
    })


# d0 收 10.00，d1 收 11.00 = 涨停（limit_up_price(10.0)=11.0），d2 = T+1 入场 @11.00
# 止损线 9.90 / 止盈线 12.10（tp=sl=0.10，便于手算）
_GAP = dict(
    closes=[10.0, 11.0, 11.0, 9.0, 9.0],
    opens=[10.0, 11.0, 11.0, 9.0, 9.0],     # d3 低开 9.0，穿过 9.90
    highs=[10.2, 11.0, 11.2, 9.2, 9.2],
    lows=[9.9, 11.0, 10.9, 8.8, 8.8],
)


def test_gap_down_settles_at_open_not_at_stop_level():
    mod = _load()
    trades, skipped = mod._backtest_chase(
        "600000", "浦发银行", _frame(**_GAP), tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert (len(trades), skipped) == (1, 0)
    t = trades[0]
    assert t.reason == "止损"
    assert t.exit == 9.0                 # 旧实现给 9.9（抹回止损价）
    assert t.exit_date == "20260104"


def test_entry_bar_is_still_judged(monkeypatch):
    """T+1 开盘买入当日就可能被止损——旧实现从 k=j 起判，迁移不许丢这一根。"""
    mod = _load()
    df = _frame(closes=[10.0, 11.0, 10.9],
                opens=[10.0, 11.0, 11.0],
                highs=[10.2, 11.0, 11.1],
                lows=[9.9, 11.0, 9.5])          # 入场当日最低 9.5 ≤ 9.90
    trades, skipped = mod._backtest_chase(
        "600000", "浦发银行", df, tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert (len(trades), skipped) == (1, 0)
    t = trades[0]
    assert t.reason == "止损"
    assert t.exit == 9.9
    assert t.exit_date == t.entry_date        # 同日出场


def test_data_ends_on_entry_day_still_reports_timeout():
    mod = _load()
    df = _frame(closes=[10.0, 11.0, 10.5],
                opens=[10.0, 11.0, 11.0],
                highs=[10.2, 11.0, 11.2],
                lows=[9.9, 11.0, 10.0])
    trades, _ = mod._backtest_chase(
        "600000", "浦发银行", df, tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert len(trades) == 1
    assert trades[0].reason == "超时"
    assert trades[0].exit == 10.5


def test_ladder_delegates_to_kernel(monkeypatch):
    mod = _load()
    import tdxfeed.execution.single as single

    seen = []
    original = single.settle_single_trade

    def spy(*args, **kwargs):
        seen.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(single, "settle_single_trade", spy)
    trades, _ = mod._backtest_chase(
        "600000", "浦发银行", _frame(**_GAP), tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert seen, "daban 回测的止盈止损没有走内核"
    assert trades[0].exit == 9.0
