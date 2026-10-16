"""P2-5 Almgren-Chriss 最优执行 单测。"""
from __future__ import annotations

import numpy as np

from quant.execution.optimal_execution import (
    ac_params_from_cost,
    calibrate_gamma_ac,
    execution_table_markdown,
    optimal_execution,
    suggest_execution,
)
from quant.multifactor.cost_model import CostConfig


def _run(lam: float):
    return optimal_execution(X=1e6, T=1.0, gamma=0.1, sigma=0.02, lam=lam, eta=0.0, n=400)


def test_ac_reduces_objective_vs_uniform():
    r = _run(lam=2.0)
    assert r["objective"] <= r["uniform_objective"] + 1e-6


def test_ac_variance_lower_than_uniform():
    r = _run(lam=2.0)
    assert r["variance"] <= r["uniform_variance"]


def test_uniform_at_zero_risk():
    # λ=0 退化为匀速执行，目标应与匀速基准完全一致
    r = _run(lam=0.0)
    t = r["t"]
    uniform = 1e6 * (1.0 - t / 1.0)
    assert np.allclose(r["inventory"], uniform, atol=1e-6)
    assert abs(r["objective"] - r["uniform_objective"]) < 1e-9


def test_inventory_constraints():
    r = optimal_execution(X=1e6, T=1.0, gamma=0.1, sigma=0.02, lam=1.0, n=400)
    assert r["_check_nonneg"]
    assert r["_check_end_zero"]
    assert r["_check_monotone"]


def test_high_lambda_frontloads():
    # 风险厌恶越高，越前置成交 → 中点库存低于匀速的 X/2
    r = _run(lam=5.0)
    mid = len(r["t"]) // 2
    x_mid = r["inventory"][mid]
    x_uniform_mid = 1e6 * (1.0 - r["t"][mid] / 1.0)
    assert x_mid < x_uniform_mid - 1e-3


def test_params_from_cost():
    cfg = CostConfig(impact_gamma=0.1)
    p = ac_params_from_cost(cfg, adv_shares=1e6, T_days=1.0)
    assert abs(p["gamma"] - 0.1 / 1e6) < 1e-15


def test_calibrate_gamma_monotone_in_size():
    # 小单→γ 大（≈匀速）；大单相对 ADV→γ 小（→前置）
    g_small = calibrate_gamma_ac(price=10, X=1000, adv_shares=5e6, T_days=1.0)
    g_large = calibrate_gamma_ac(price=10, X=2e5, adv_shares=2e5, T_days=1.0)
    assert g_small > g_large


def test_suggest_small_order_twap():
    s = suggest_execution(qty=1000, price=10.0, adv_shares=5e6, daily_vol=0.03,
                          T_days=1.0, lam=0.05, n_buckets=6)
    total = sum(b["shares"] for b in s["schedule"])
    assert abs(total - 1000) < 1e-6
    assert all(b["shares"] >= -1e-9 for b in s["schedule"])
    assert len(s["schedule"]) == 6
    # 小单流动性好 → 不应强烈前置
    assert s["front_load_pct"] <= s["twap_front_pct"] + 10.0 + 1e-6


def test_suggest_large_order_frontload():
    s = suggest_execution(qty=2e5, price=10.0, adv_shares=2e5, daily_vol=0.05,
                          T_days=1.0, lam=2.0, n_buckets=6)
    total = sum(b["shares"] for b in s["schedule"])
    assert abs(total - 2e5) < 1e-3
    # 大单相对 ADV → 前置成交（前 1/3 段占比显著高于匀速）
    assert s["front_load_pct"] > s["twap_front_pct"] + 10.0
    assert s["var_reduce_pct"] > 0.0
    assert "建议前置成交" in s["recommendation"]


def test_suggest_table_markdown():
    s = suggest_execution(qty=1000, price=10.0, adv_shares=5e6, daily_vol=0.03, n_buckets=4)
    md = execution_table_markdown(s)
    assert "| 时段 |" in md
    assert md.count("|") >= 5
