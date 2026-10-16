"""P1-2 仓位与尾部风险：半 Kelly 缩放、CVaR 监控、CVaR 约束。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.multifactor.factors import MFConfig
from quant.multifactor.portfolio import (
    ERCConfig,
    cvar_from_scenarios,
    half_kelly_scaling,
    solve_erc,
    solve_erc_cvxpy,
)
from quant.multifactor.risk_model import RiskModel


def test_half_kelly_scaling_clip_and_shrink():
    rng = np.random.default_rng(0)
    n = 5
    r = rng.standard_normal((60, n)) * 0.01
    r[:, 0] += 0.001   # 正 mu、正 var → k∈(0,1] 或触顶
    r[:, 1] -= 0.001   # 负 mu → k=0
    r[:, 2] = 0.002    # 零方差、正 mu → k=1
    rets = pd.DataFrame(r, columns=[f"{i:06d}" for i in range(n)])
    k = half_kelly_scaling(rets, frac=0.5, window=60)
    assert k.shape == (n,)
    assert ((k >= 0) & (k <= 1)).all()
    assert k[1] == 0.0          # 负预期收益 → 收缩到 0
    assert k[2] == 1.0          # 零方差正收益 → 满配
    assert k[0] > 0.0


def test_half_kelly_scaling_none():
    assert half_kelly_scaling(None).size == 0


def test_cvar_from_scenarios_known_tail():
    # 单资产、20 个场景，最差 1 个（5%）为 -0.10，其余 0 → CVaR(95%) = -0.10
    scen = np.zeros((20, 1))
    scen[0, 0] = -0.10
    w = np.array([1.0])
    cvar = cvar_from_scenarios(scen, w, alpha=0.95)
    assert abs(cvar - (-0.10)) < 1e-9


def test_cvar_from_scenarios_empty():
    assert cvar_from_scenarios(np.array([]), np.array([1.0])) == 0.0


def test_solve_erc_cvxpy_cvar_constraint_satisfied():
    rng = np.random.default_rng(1)
    n = 6
    codes = [f"{i:06d}" for i in range(n)]
    cov = np.eye(n) * 0.0004 + rng.standard_normal((n, n)) * 1e-6
    cov = (cov + cov.T) / 2
    w_base = np.full(n, 1.0 / n)
    # 构造温和场景，使 cvar_limit 宽松可行
    scen = rng.standard_normal((40, n)) * 0.01
    erc = ERCConfig(max_weight=1.0)
    w = solve_erc_cvxpy(cov, codes, {}, None, None, erc, w_base,
                        scenarios=scen, cvar_limit=0.20, cvar_alpha=0.95)
    assert w is not None
    # 约束满足：组合日损失 CVaR ≤ 0.20
    rp = scen @ w
    q = np.quantile(rp, 0.05)
    cvar_loss = -float(rp[rp <= q].mean()) if (rp <= q).any() else -q
    assert cvar_loss <= 0.20 + 1e-6


def test_solve_erc_cvxpy_no_cvar_default():
    n = 5
    codes = [f"{i:06d}" for i in range(n)]
    cov = np.eye(n)
    w_base = np.full(n, 1.0 / n)
    erc = ERCConfig(max_weight=1.0)
    w = solve_erc_cvxpy(cov, codes, {}, None, None, erc, w_base)
    assert w is not None and abs(w.sum() - 1.0) < 1e-6


def test_solve_erc_forwards_cvar():
    rng = np.random.default_rng(3)
    n = 5
    codes = [f"{i:06d}" for i in range(n)]
    cov = np.eye(n)
    scen = rng.standard_normal((30, n)) * 0.01
    erc = ERCConfig(max_weight=1.0)
    w = solve_erc(cov, codes, {}, None, None, erc,
                  scenarios=scen, cvar_limit=0.20, cvar_alpha=0.95)
    assert w is not None and abs(w.sum() - 1.0) < 1e-6


def test_mf_backtest_kelly_vol_cvar_metrics():
    # 默认（全关）：应有 CVaR 监控指标，无 Kelly/vol 指标
    from quant.multifactor.engine import run_mf_backtest
    _, m = run_mf_backtest(MFConfig(limit=80, end="2026-10-15", valuation_mode="proxy"))
    assert "max_cvar" in m and "mean_cvar" in m
    assert "kelly_median_scale" not in m  # 默认关
    # 开启半 Kelly + 波动率目标
    _, m2 = run_mf_backtest(MFConfig(
        limit=80, end="2026-10-15", valuation_mode="proxy",
        kelly_frac=0.5, vol_target=0.30,
    ))
    assert "kelly_median_scale" in m2
    assert "mean_vol_scale" in m2
    assert m2["mean_vol_scale"] <= 1.0 + 1e-9
