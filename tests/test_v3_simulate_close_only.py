"""v3 是收盘价判定的老口径，迁移时把 judge 显式写出来，别顺手改成日内。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_v3_declares_close_only_plan():
    plan = _load("v3_simulate").EXIT_PLAN
    assert plan.judge == "close_only"
    assert plan.sl_first is False
    assert (plan.tp, plan.sl, plan.hold_days) == (0.30, -0.10, 30)


def test_v3_rolling_declares_close_only_plan():
    plan = _load("v3_simulate_rolling").EXIT_PLAN
    assert plan.judge == "close_only"
    assert plan.sl_first is False
    assert (plan.tp, plan.sl, plan.hold_days) == (0.30, -0.10, 30)


@pytest.mark.parametrize("name", ["v3_simulate", "v3_simulate_rolling"])
def test_effective_plan_honours_runtime_params(name):
    """模块级常量只是默认口径；CLI / 函数参数覆盖必须真的生效。"""
    plan = _load(name).exit_plan(0.5, -0.2, 7)
    assert (plan.tp, plan.sl, plan.hold_days) == (0.5, -0.2, 7)
    assert plan.judge == "close_only"
    assert plan.sl_first is False
