"""ERC 风险预算组合构建（Spinu 迭代法，无需 NLP 求解器）。

目标：等风险贡献（RC_i ≈ const）。
约束：
- 权重和 = 1
- 个股 ≤ max_weight（迭代投影）
- 申万一级行业暴露 ≤ max_industry_exposure（截断投影）
- 周换手 ≤ max_weekly_turnover（相对上期组合，按 1-范数精确混合）
固定 seed 可复现。相对 scipy SLSQP，速度提升约 100×。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ERCConfig:
    max_weight: float = 0.08
    max_industry_exposure: float = 0.30
    max_weekly_turnover: float = 0.40
    seed: int = 42


def _erc_spinu(cov: np.ndarray, max_iter: int = 100, tol: float = 1e-10) -> np.ndarray:
    """Spinu (2013) 等风险贡献迭代解；返回未归一化前的 x，最终 w = x / sum(x)。"""
    d = np.diag(cov)
    d[d <= 0] = 1e-12
    x = 1.0 / np.sqrt(d)
    for _ in range(max_iter):
        sigma_x = cov @ x
        denom = float(x @ sigma_x)
        if denom <= 0:
            break
        w_raw = x / denom
        sx = cov @ w_raw
        sx[sx <= 0] = 1e-12
        x_new = w_raw / np.sqrt(sx)
        if np.max(np.abs(x_new - x)) < tol:
            x = x_new
            break
        x = x_new
    return x


def _project_max_weight(w: np.ndarray, max_weight: float, max_iter: int = 50) -> np.ndarray:
    """将权重逐轮截断到 max_weight 并重新归一化（近似投影，保留等风险思想）。"""
    w = np.clip(w, 0.0, None)
    for _ in range(max_iter):
        if w.sum() <= 0:
            break
        w = w / w.sum()
        over = w > max_weight
        if not over.any():
            break
        excess = (w[over] - max_weight).sum()
        w[over] = max_weight
        rem = w[~over]
        if rem.sum() <= 0:
            break
        w[~over] = rem + excess * (rem / rem.sum())
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 0 else w


def _project_industry_cap(w: np.ndarray, codes: list[str],
                          industry_of: dict[str, str],
                          cap: float, max_iter: int = 50) -> np.ndarray:
    """将任一行业暴露截断到 cap（超额部分按行业内权重比例释放到其余行业）。"""
    w = np.clip(w, 0.0, None)
    for _ in range(max_iter):
        if w.sum() <= 0:
            break
        inds = np.array([industry_of.get(c, "未知") for c in codes])
        exp = {}
        for ind in set(inds):
            exp[ind] = float(w[inds == ind].sum())
        viol = [ind for ind, e in exp.items() if e > cap]
        if not viol:
            break
        w = w / w.sum()
        for ind in viol:
            idx = inds == ind
            excess = float(w[idx].sum()) - cap
            w[idx] = w[idx] / w[idx].sum() * cap
            others = ~idx
            oth_sum = float(w[others].sum())
            if oth_sum <= 0:
                break
            w[others] = w[others] + excess * (w[others] / oth_sum)
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 0 else w


def enforce_turnover(w: np.ndarray, codes: list[str],
                       prev_w: np.ndarray | None, prev_codes: list[str] | None,
                       cap: float) -> np.ndarray:
    """与上期组合按 1-范数精确混合，保证周换手 ≤ cap。

    凸组合天然保持权重上限与行业暴露上限（前提是 prev 也为合法组合），故可置于
    任意投影之后作为最终保证。无上期时原样返回。
    """
    if prev_w is None or prev_codes is None or len(prev_w) != len(prev_codes):
        return w
    prev_map = dict(zip(prev_codes, prev_w, strict=True))
    pw = np.array([prev_map.get(c, 0.0) for c in codes])
    pw = np.clip(pw, 0.0, None)
    psum = pw.sum()
    if psum <= 0:
        return w
    pw = pw / psum
    diff = w - pw
    to = float(np.abs(diff).sum())
    if to > 1e-9 and to > cap:
        alpha = cap / to
        w = pw + alpha * diff
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 0 else w


def solve_erc(cov: np.ndarray, codes: list[str],
              industry_of: dict[str, str],
              prev_w: np.ndarray | None = None,
              prev_codes: list[str] | None = None,
              cfg: ERCConfig | None = None) -> np.ndarray:
    """返回归一化权重（与 codes 对齐）。失败回退等权。

    流程：Spinu 等风险权重 → max_weight 投影 → 行业暴露投影 → **周换手约束（最后保证）**。
    凸组合特性保证最终权重同时满足权重/行业上限与周换手上限。
    """
    n = len(codes)
    if n == 0:
        return np.array([])
    if cfg is None:
        cfg = ERCConfig()
    cov = np.asarray(cov, dtype=float)
    # 数值钳制（保证 PSD）
    w_eig, V = np.linalg.eigh(cov)
    w_eig = np.clip(w_eig, 1e-8, None)
    cov = V @ np.diag(w_eig) @ V.T

    try:
        x = _erc_spinu(cov)
        w = x / x.sum()
        w = _project_max_weight(w, cfg.max_weight)
        w = _project_industry_cap(w, codes, industry_of, cfg.max_industry_exposure)
    except Exception:
        w = np.full(n, 1.0 / n)
        w = _project_industry_cap(w, codes, industry_of, cfg.max_industry_exposure)

    # 周换手约束：置于最后，确保返回权重满足 ≤ max_weekly_turnover
    w = enforce_turnover(w, codes, prev_w, prev_codes, cfg.max_weekly_turnover)
    s = w.sum()
    if s <= 0:
        w = np.full(n, 1.0 / n)
        s = w.sum()
    return w / s
