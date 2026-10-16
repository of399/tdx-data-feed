"""factor_significance.py 的 P2-4 正交化集成测试。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直接按文件路径加载脚本模块（scripts/ 未必是包），避免包导入假设
_SPEC = importlib.util.spec_from_file_location(
    "factor_significance", ROOT / "scripts" / "factor_significance.py"
)
fs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fs)

from quant.multifactor.factors import (  # noqa: E402
    cross_sectional_projection,
    symmetric_orthogonalize,
)


def _make_df(n: int = 3000, seed: int = 0) -> pd.DataFrame:
    """构造含共线因子的合成数据。

    latent1 驱动 label 且被 fA/fB 共同承载（二者高度共线）；
    latent2 独立驱动 fC（独立信号）；
    fNoise 纯噪声。用于验证「冗余 / 独立」判定。
    """
    rng = np.random.default_rng(seed)
    latent1 = rng.standard_normal(n)
    latent2 = rng.standard_normal(n)
    fA = latent1 + 0.1 * rng.standard_normal(n)
    fB = 2.0 * fA  # 与 fA 精确线性共线 → 二者互相冗余
    fC = latent2 + 0.1 * rng.standard_normal(n)  # 独立信号
    fNoise = rng.standard_normal(n)
    label = ((latent1 + latent2) > 0).astype(int)
    return pd.DataFrame(
        {"fA": fA, "fB": fB, "fC": fC, "fNoise": fNoise, "label": label}
    )


def test_orthogonalize_returns_frame_with_columns():
    df = _make_df()
    res = fs.orthogonalize_factor_significance(df, ["fA", "fB", "fC", "fNoise"], method="symmetric")
    assert res is not None
    for col in [
        "factor", "rank_ic_raw", "rank_ic_p_raw", "rank_ic_orth", "rank_ic_p_orth",
        "rank_ic_resid", "rank_ic_p_resid", "redundant", "independent",
    ]:
        assert col in res.columns


def test_redundancy_detection():
    df = _make_df()
    # 仅用确定性核心：fA/fB 精确共线（互相冗余）、fC 独立信号源
    res = fs.orthogonalize_factor_significance(
        df, ["fA", "fB", "fC"], method="symmetric"
    ).set_index("factor")
    # fC：独立信号（残差仍显著）
    assert bool(res.loc["fC", "independent"]) is True
    # fA/fB：原始显著但残差被同伴解释 → 冗余
    assert bool(res.loc["fA", "redundant"]) is True
    assert bool(res.loc["fB", "redundant"]) is True


def test_symmetric_decorrelates():
    rng = np.random.default_rng(7)
    F = rng.standard_normal((200, 4)) @ rng.standard_normal((4, 4))
    S = symmetric_orthogonalize(F)
    corr = np.corrcoef(S, rowvar=False)
    assert np.allclose(corr - np.eye(4), 0.0, atol=1e-8)


def test_residual_orthogonal_to_peers():
    rng = np.random.default_rng(8)
    F = rng.standard_normal((300, 3))
    _, resid = cross_sectional_projection(F[:, 0], F[:, 1:])
    assert np.allclose(F[:, 1:].T @ resid, 0.0, atol=1e-8)


def test_insufficient_data_returns_none():
    df = _make_df(n=10)  # 完整子集 < 50
    res = fs.orthogonalize_factor_significance(df, ["fA", "fB"], method="symmetric")
    assert res is None


def test_gram_schmidt_method_also_works():
    df = _make_df()
    res = fs.orthogonalize_factor_significance(
        df, ["fA", "fB", "fC", "fNoise"], method="gram_schmidt"
    )
    assert res is not None
    assert "rank_ic_resid" in res.columns


def test_symmetric_orthogonalize_handles_inf_and_constant():
    rng = np.random.default_rng(9)
    base = rng.standard_normal((200, 2))
    # 第 3 列含 inf（特征工程除零典型），第 4 列近恒定
    inf_col = base[:, 0].copy()
    inf_col[0] = np.inf
    const_col = np.full(200, 1.0)
    F = np.column_stack([base, inf_col, const_col])
    S = symmetric_orthogonalize(F)
    assert np.all(np.isfinite(S)), "inf/恒定列应被稳健化为有限值"
    # 前两列仍应近似不相关
    corr = np.corrcoef(S[:, :2].T)
    assert np.allclose(corr - np.eye(2), 0.0, atol=1e-8)


def test_cross_sectional_projection_handles_inf():
    rng = np.random.default_rng(10)
    F = rng.standard_normal((300, 3))
    y = F[:, 0].copy()
    y[0] = np.inf  # 非有限值
    _, resid = cross_sectional_projection(y, F)
    assert np.all(np.isfinite(resid)), "inf 输入不应产生 NaN 残差"
    assert np.allclose(F.T @ resid, 0.0, atol=1e-6)
