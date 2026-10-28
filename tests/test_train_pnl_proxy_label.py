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
    expected = CostModel(commission=0.0003, slippage=0.0010, stamp_sell=0.0).round_trip(10.0, 9.0)
    assert pnl == pytest.approx(expected, abs=1e-12)
    assert pnl < -0.10


def test_label_ignores_entry_bar_high_low():
    """入场根自己的 high/low 不算触发（旧实现从入场根就开始判）。

    新语义：入场根 (14.0/6.0) 不判，唯一可判定根 (10.2/9.9) 不碰 13.0/9.0，
    所以一路走到末根收盘 10.0 结算 → END，净收益只剩成本。
    旧实现在入场根即命中 TP 13.0（14.0/10.0-1=0.4，且它先判止盈）→ +0.2974，
    本断言对它为红。
    """
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0), (10.0, 14.0, 6.0, 10.0),
                (10.0, 10.2, 9.9, 10.0)])
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    from tdxfeed.execution.costs import CostModel
    expected = CostModel(commission=0.0003, slippage=0.0010, stamp_sell=0.0).round_trip(10.0, 10.0)
    assert pnl == pytest.approx(expected, abs=1e-12)
    assert pnl < 0


def test_label_gap_through_stop_fills_at_open():
    """跳空穿过止损价按当日开盘成交：8.0 而不是阈值 9.0。"""
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0),   # 信号根
                (10.0, 10.0, 10.0, 10.0),   # 入场根（T+1 开盘 10.0）
                (8.0, 8.4, 7.8, 8.0)])      # 跳空低开，穿过 9.0
    from tdxfeed.execution.costs import CostModel
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    expected = CostModel(commission=0.0003, slippage=0.0010, stamp_sell=0.0).round_trip(10.0, 8.0)
    assert pnl == pytest.approx(expected, abs=1e-12)
    assert pnl < -0.20   # 阈值成交只会有 -0.1026


def test_label_entry_bar_only_yields_none():
    """只剩入场根时内核没有可判窗口——旧实现会回一个只扣成本的 -0.0026。"""
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0),   # 信号根
                (10.0, 10.0, 10.0, 10.0)])  # 入场根，之后无数据
    assert mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01")) is None


def test_label_rejects_bad_entry_open():
    """入场价非有限或非正时返回 None，不能把 NaN 传成标签。"""
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0),
                (float("nan"), 10.0, 10.0, 10.0),
                (10.0, 10.0, 10.0, 10.0)])
    assert mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01")) is None
    _seed(mod, [(10.0, 10.0, 10.0, 10.0),
                (0.0, 10.0, 10.0, 10.0),
                (10.0, 10.0, 10.0, 10.0)])
    assert mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01")) is None
