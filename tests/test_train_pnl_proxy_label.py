"""训练标签必须与回测同一口径，否则模型学的是「乐观成交」。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("train_pnl_under_test",
                                                  ROOT / "scripts" / "train_pnl_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _seed(mod, bars):
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(bars)),
        "open": [b[0] for b in bars], "high": [b[1] for b in bars],
        "low": [b[2] for b in bars], "close": [b[3] for b in bars],
        "volume": [1e6] * len(bars),
    })
    mod._load_one = lambda code: df


def test_label_double_touch_is_loss_side():
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.0, 13.5, 8.5, 12.0)])
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    # 内核成本是乘性的（CostModel.round_trip），不是 ret - 0.0026 的加法：
    # 加法口径在这里会给 -0.1026，差 2.6e-4，足以让这条用例红。
    from tdxfeed.execution.costs import CostModel
    expected = CostModel(commission=0.0003, slippage=0.0010).round_trip(10.0, 9.0)
    assert pnl == pytest.approx(expected, abs=1e-12)
    assert pnl < -0.10


def test_label_ignores_entry_bar_high_low():
    """入场根自己的 high/low 不算触发（旧实现从入场根就开始判）。"""
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0), (10.0, 14.0, 6.0, 10.0),
                (10.0, 10.2, 9.9, 10.0)])
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    assert pnl is not None and pnl > -0.01
