"""journal 的出场判定与回测同源；持有期按交易日而不是自然日。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("journal_under_test",
                                                  ROOT / "scripts" / "plan_e_journal.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_journal_exit_plan_is_canonical():
    mod = _load()
    assert mod.EXIT_PLAN.judge == "intrabar"
    assert mod.EXIT_PLAN.sl_first is True


def test_journal_gap_down_uses_open():
    mod = _load()
    from tdxfeed.execution.single import check_exit
    hit = check_exit(10.0, open_=8.0, high=8.4, low=7.8, close=8.0, plan=mod.EXIT_PLAN)
    assert hit.reason == "SL" and hit.price == 8.0


def test_journal_counts_trading_days():
    mod = _load()
    assert mod._bars_held(pd.Timestamp("2026-01-09"), pd.Timestamp("2026-01-12")) == 1


def test_journal_double_trigger_settles_loss_side():
    """同一根 K 线双触发结算亏损侧——旧实现先判止盈，这是本轮的口径变化。"""
    mod = _load()
    from tdxfeed.execution.single import check_exit
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0, plan=mod.EXIT_PLAN)
    assert hit.reason == "SL" and hit.price == 9.0


def test_time_due_matches_kernel_window():
    """TIME 边界与内核一致：hold_days=10 含入场根 → 入场后第 9 根到期。"""
    mod = _load()
    assert mod._time_due(8) is False
    assert mod._time_due(9) is True
