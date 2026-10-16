"""P2-4 正交投影 / Gram-Schmidt / 对称正交化 / CVaR Riesz 对偶 单测。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.multifactor.factors import (
    cross_sectional_projection,
    cvar_riesz_dual,
    gram_schmidt_orthogonalize,
    orthogonalize_factor_panel,
    symmetric_orthogonalize,
)
from quant.multifactor.portfolio import cvar_from_scenarios


def test_gram_schmidt_orthonormal():
    rng = np.random.default_rng(0)
    F = rng.standard_normal((200, 5))
    Q = gram_schmidt_orthogonalize(F, mode="modified")
    # 标准正交：QᵀQ = I
    assert np.allclose(Q.T @ Q, np.eye(5), atol=1e-8)


def test_symmetric_uncorrelated():
    rng = np.random.default_rng(1)
    F = rng.standard_normal((200, 5)) @ rng.standard_normal((5, 5))  # 相关因子
    S = symmetric_orthogonalize(F)
    corr = np.corrcoef(S, rowvar=False)
    off = corr - np.eye(5)
    assert np.allclose(off, 0.0, atol=1e-8), "对称正交化后因子应互不相关"


def test_projection_residual_orthogonal():
    rng = np.random.default_rng(2)
    T, K = 200, 4
    F = rng.standard_normal((T, K))
    b = rng.standard_normal(K)
    y = F @ b + 0.01 * rng.standard_normal(T)
    proj, resid = cross_sectional_projection(y, F)
    # 残差与每个因子正交（因子暴露归零）
    assert np.allclose(F.T @ resid, 0.0, atol=1e-8)
    # 投影应等于最小二乘拟合
    beta, *_ = np.linalg.lstsq(F, y, rcond=None)
    assert np.allclose(proj, F @ beta, atol=1e-8)


def test_riesz_dual_equals_cvar():
    rng = np.random.default_rng(3)
    rets = rng.standard_normal((60, 8)) * 0.01
    w = np.full(8, 1.0 / 8)
    losses = -(rets @ w)
    alpha = 0.95
    dual = cvar_riesz_dual(losses, alpha)
    n = losses.size
    n_tail = max(1, int(np.ceil((1.0 - alpha) * n)))
    # 对偶 = 最差 (1−α) 分位损失均值（RU 上确界取到）
    assert abs(dual - float(np.mean(np.sort(losses)[-n_tail:]))) < 1e-12
    # 与 cvar_from_scenarios（收益口径）关系：dual = −cvar(returns)；经验尾部边界差异，容差 1e-3
    assert abs(dual - (-cvar_from_scenarios(rets, w, alpha))) < 1e-3


def test_orthogonalize_factor_panel_shape():
    rng = np.random.default_rng(4)
    panel = pd.DataFrame(
        rng.standard_normal((50, 4)),
        columns=["f1", "f2", "f3", "f4"],
        index=[f"{i:06d}" for i in range(50)],
    )
    out = orthogonalize_factor_panel(panel, method="symmetric")
    assert out.shape == panel.shape
    corr = out[["f1", "f2", "f3", "f4"]].corr().to_numpy()
    off = corr - np.eye(4)
    assert np.allclose(off, 0.0, atol=1e-8)
