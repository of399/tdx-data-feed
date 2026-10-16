"""P0-1 BH-FDR 多重检验校正（quant/multifactor/cv_utils.bh_fdr_correct）测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quant.multifactor.cv_utils import bh_fdr_correct, permutation_return_test  # noqa: E402


def test_bh_fdr_matches_statsmodels():
    """作为 thin wrapper，结果必须与 statsmodels multipletests(fdr_bh) 完全一致。"""
    from statsmodels.stats.multitest import multipletests

    pvals = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074,
                      0.205, 0.212, 0.25, 0.27, 0.27, 0.27, 0.35, 0.4,
                      0.42, 0.45, 0.56, 0.7, 0.74, 0.87, 0.99])
    rej, q = bh_fdr_correct(pvals, alpha=0.05)
    exp_rej, exp_q, _, _ = multipletests(pvals, method="fdr_bh", alpha=0.05)
    assert np.array_equal(rej, exp_rej)
    assert np.allclose(q, exp_q, equal_nan=True)


def test_bh_fdr_reject_iff_q_below_alpha():
    """基础性质：reject 当且仅当 q < alpha。"""
    pvals = np.array([0.001, 0.02, 0.04, 0.06, 0.30, 0.90])
    rej, q = bh_fdr_correct(pvals, alpha=0.05)
    assert np.array_equal(rej, q < 0.05)


def test_bh_fdr_monotonic_nondecreasing():
    """q 值对排序后的 p 值单调非减（按 p 升序排列后判定）。"""
    rng = np.random.default_rng(1)
    pvals = rng.uniform(0, 1, size=50)
    _, q = bh_fdr_correct(pvals)
    valid = ~np.isnan(q)
    q_valid = q[valid]
    order = np.argsort(pvals[valid])  # 按 p 升序
    assert np.all(np.diff(q_valid[order]) >= -1e-12)


def test_bh_fdr_reject_subset_of_p_le_alpha():
    """被拒绝的 p 值必 ≤ alpha（BH 拒绝集 ⊆ {p ≤ alpha}）。"""
    rng = np.random.default_rng(2)
    pvals = rng.uniform(0, 1, size=100)
    rej, _ = bh_fdr_correct(pvals, alpha=0.05)
    assert np.all(pvals[rej] <= 0.05 + 1e-12)


def test_bh_fdr_nan_handling():
    """含 nan 的 p 值不参与校正：对应 q=nan、reject=False，其余不受影响。"""
    pvals = np.array([0.01, np.nan, 0.5, 0.001, np.nan])
    rej, q = bh_fdr_correct(pvals, alpha=0.05)
    assert np.isnan(q[1]) and np.isnan(q[4])
    assert bool(rej[1]) is False and bool(rej[4]) is False
    # 其余位置与去掉 nan 后单独算一致
    rej2, q2 = bh_fdr_correct(pvals[[0, 2, 3]], alpha=0.05)
    assert np.allclose(q[[0, 2, 3]], q2, equal_nan=True)
    assert np.array_equal(rej[[0, 2, 3]], rej2)


def test_bh_fdr_all_nan():
    """全 nan 不报错，返回全 False / 全 nan。"""
    pvals = np.array([np.nan, np.nan])
    rej, q = bh_fdr_correct(pvals)
    assert rej.dtype == bool
    assert rej.shape == (2,)
    assert np.all(~rej)
    assert np.all(np.isnan(q))


def test_bh_fdr_controls_false_positives():
    """P0-1 核心收益：纯 null 因子（与 label 独立）经 BH-FDR 后假阳性大幅减少。

    80 个随机因子对随机 label 做 RankIC 检验，裸 p<0.05 期望 ~4 个；
    FDR 校正后应在 5% 目标下显著更少（≤ raw 数）。
    """
    from scipy.stats import spearmanr

    rng = np.random.default_rng(7)
    n, m = 2000, 80
    X = rng.standard_normal((n, m))
    label = (rng.standard_normal(n) > 0).astype(int)
    pvals = np.array([spearmanr(X[:, j], label).pvalue for j in range(m)])
    raw_sig = int((pvals < 0.05).sum())
    rej, _ = bh_fdr_correct(pvals, alpha=0.05)
    fdr_sig = int(rej.sum())
    # BH 拒绝集 ⊆ {p ≤ alpha} ⊆ {p < alpha} ∪ {边界}，故 fdr_sig ≤ raw_sig（边界概率≈0）
    assert fdr_sig <= raw_sig + 1
    # 在 5% FDR 目标下，80 个 null 期望假阳性 ≤ ~4
    assert fdr_sig <= 5


def test_bh_fdr_alpha_param():
    """alpha 放宽到 0.10 应至少不比 0.05 拒绝得更少。"""
    pvals = np.array([0.02, 0.04, 0.07, 0.09, 0.30])
    rej05, _ = bh_fdr_correct(pvals, alpha=0.05)
    rej10, _ = bh_fdr_correct(pvals, alpha=0.10)
    assert int(rej10.sum()) >= int(rej05.sum())


# ---------- P0-1 ④ 置换检验 ----------

def test_permutation_noise_not_significant():
    """近 iid 噪声（无 edge）：自相关 ≈ 0，p_two 接近 1（工具正确"不拒绝"噪声）。"""
    rng = np.random.default_rng(3)
    ret = rng.standard_normal(600)
    res = permutation_return_test(ret, seed=3)
    assert np.isfinite(res["p_two"])
    assert 0.1 <= res["p_one"] <= 0.9                     # 单侧比例居中
    assert res["p_two"] >= 0.3                            # 双侧不显著（正确"不拒绝"噪声）


def test_permutation_detects_serial_structure():
    """构造强正自相关序列（AR(1), ρ≈0.8）：置换检验应判显著（p_one 很小）。"""
    rng = np.random.default_rng(11)
    n = 600
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = 0.8 * x[t - 1] + rng.standard_normal()
    res = permutation_return_test(x, seed=11)
    # 观测自相关应明显 > 0
    assert res["stat_obs"] > 0.5
    # 随机重排后自相关 ≈ 0，故极少重排 ≥ 观测 → 单侧 p 极小
    assert res["p_one"] < 0.05


def test_permutation_small_sample_nan():
    """样本 < 5 返回全 nan/边界，不报错。"""
    res = permutation_return_test(np.array([0.01, -0.02, 0.0]), seed=1)
    assert np.isnan(res["stat_obs"])
    assert np.isnan(res["p_one"])
    assert np.isnan(res["p_two"])


def test_permutation_deterministic_with_seed():
    """同种子可复现。"""
    rng = np.random.default_rng(5)
    ret = rng.standard_normal(400)
    a = permutation_return_test(ret, seed=5)
    b = permutation_return_test(ret, seed=5)
    assert a["p_one"] == b["p_one"]
    assert a["p_two"] == b["p_two"]


def test_permutation_default_is_order_dependent():
    """默认统计量必须是顺序相关的（自相关）；若用均值则退化 p_one=1.0。

    该测试同时验证：默认非均值（否则置换检验对重排不变、恒 p=1）。
    """
    rng = np.random.default_rng(7)
    ret = rng.standard_normal(500)
    # 自定义均值统计量 → 重排近似不变（numpy pairwise 求和有 ULP 级顺序敏感）→ p≈1
    res_mean = permutation_return_test(ret, stat_fn=lambda x: float(x.mean()), seed=7)
    assert res_mean["p_one"] >= 0.95
    assert res_mean["p_two"] >= 0.90
    # 默认（自相关）→ 顺序相关，p_one 不退化到 ≈1
    res_def = permutation_return_test(ret, seed=7)
    assert res_def["p_one"] < 0.90
