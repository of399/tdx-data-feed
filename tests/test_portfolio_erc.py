"""P0-2 回归 / 单元：cvxpy 凸投影 ERC、约束残差断言、整手整数化。"""
from __future__ import annotations

import numpy as np

from quant.multifactor.engine import apply_lot_rounding
from quant.multifactor.portfolio import (
    ERCConfig,
    _assert_constraints,
    _erc_spinu,
    solve_erc,
    solve_erc_cvxpy,
)


def _random_cov(n: int = 20, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    A = rng.standard_normal((n, n))
    return A @ A.T / n + np.eye(n) * 1e-3


def test_cvxpy_solve_satisfies_constraints():
    n = 20
    cov = _random_cov(n)
    codes = [f"{i:06d}" for i in range(n)]
    # 5 行业 × 4 股，cap=0.30 → 5*0.30=1.5≥1 可行；max_weight=0.08 亦可行
    ind = {c: f"I{i // 4}" for i, c in enumerate(codes)}
    cfg = ERCConfig(max_weight=0.08, max_industry_exposure=0.30, max_weekly_turnover=0.40)
    x = _erc_spinu(cov)
    w_base = x / x.sum()
    w = solve_erc_cvxpy(cov, codes, ind, None, None, cfg, w_base)
    assert w is not None, "cvxpy solve failed（约束应可行）"
    _assert_constraints(w, codes, ind, cfg, None, None)  # 不应抛异常


def test_cvxpy_matches_spinu_when_unconstrained():
    # 放宽约束，cvxpy 投影应≈base（偏差<1e-3）
    n = 15
    cov = _random_cov(n, seed=3)
    codes = [f"{i:06d}" for i in range(n)]
    cfg = ERCConfig(max_weight=1.0, max_industry_exposure=1.0, max_weekly_turnover=1.0)
    w_base = _erc_spinu(cov) / _erc_spinu(cov).sum()
    w = solve_erc_cvxpy(cov, codes, {}, None, None, cfg, w_base)
    assert w is not None
    assert np.max(np.abs(w - w_base)) < 1e-3


def test_solve_erc_returns_feasible():
    n = 25
    cov = _random_cov(n, seed=7)
    codes = [f"{i:06d}" for i in range(n)]
    ind = {c: f"I{i % 5}" for i, c in enumerate(codes)}
    cfg = ERCConfig()
    w = solve_erc(cov, codes, ind, None, None, cfg)
    assert abs(w.sum() - 1) < 1e-6
    assert w.min() >= -1e-8
    assert (w - cfg.max_weight).max() <= 1e-6


def test_lot_rounding_integers_and_bounds():
    w = np.full(10, 0.1)
    prices = np.full(10, 10.0)
    nav = 1_000_000.0
    rw = apply_lot_rounding(w, prices, nav, lot=100)
    assert (rw >= -1e-12).all()
    assert rw.sum() <= 1.0 + 1e-9
    # 隐含股数必须是 100 的整数倍
    shares_implied = (rw * nav) / prices
    assert np.allclose(shares_implied % 100, 0, atol=1e-6)
