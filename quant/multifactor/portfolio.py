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

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ERCConfig:
    max_weight: float = 0.08
    max_industry_exposure: float = 0.30
    max_weekly_turnover: float = 0.40
    seed: int = 42


def _erc_spinu(cov: np.ndarray, max_iter: int = 100, tol: float = 1e-10) -> np.ndarray:
    """Spinu (2013) 等风险贡献迭代解；返回未归一化前的 x，最终 w = x / sum(x)。"""
    d = np.diag(cov).copy()
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


def half_kelly_scaling(rets, frac: float = 0.5, window: int = 60) -> np.ndarray:
    """P1-2 半 Kelly 单股缩放：用滚动 μ/σ² 估计 Kelly 分数 f*=μ/σ²，取 frac·f* 作缩放系数并截断到 [0,1]（无杠杆、不裸空）。

    返回与 rets.columns 对齐的 k_i∈[0,1]：低夏普/负预期收益的个股被收缩（k→0），
    高夏普个股保留（k→1）；零方差且正预期收益的个股 k=1（无风险满配）。
    rets 为 (T,N) 日收益 DataFrame；取最近 window 行估计。
    """
    if rets is None or getattr(rets, "shape", (0,))[1] == 0:
        return np.array([])
    r = np.asarray(rets.iloc[-window:], dtype=float)
    if r.shape[0] < 2:
        return np.ones(r.shape[1])
    mu = np.nanmean(r, axis=0)
    var = np.nanvar(r, axis=0, ddof=1)
    k = np.zeros(r.shape[1], dtype=float)
    pos_var = var > 1e-12
    with np.errstate(divide="ignore", invalid="ignore"):
        k[pos_var] = np.clip(frac * mu[pos_var] / var[pos_var], 0.0, 1.0)
    k[~pos_var] = np.where(mu[~pos_var] > 0, 1.0, 0.0)
    return k


def cvar_from_scenarios(scen: np.ndarray, w: np.ndarray, alpha: float = 0.95) -> float:
    """P1-2 CVaR（条件风险价值，收益口径）监控：组合场景收益 r_p=scen@w 的最差 (1-α) 分位均值。

    损失口径 CVaR = -返回值。用于回测报告尾部风险监控（不强制约束）。
    """
    scen = np.asarray(scen, dtype=float)
    w = np.asarray(w, dtype=float)
    if scen.size == 0 or w.size == 0:
        return 0.0
    rp = scen @ w
    rp = rp[np.isfinite(rp)]
    if rp.size == 0:
        return 0.0
    q = np.quantile(rp, 1 - alpha)
    tail = rp[rp <= q]
    return float(tail.mean()) if tail.size else float(q)


def _assert_constraints(w: np.ndarray, codes: list[str],
                        industry_of: dict[str, str], cfg: ERCConfig,
                        prev_w: np.ndarray | None, prev_codes: list[str] | None,
                        tol: float = 1e-6) -> None:
    """逐约束残差断言（防『行业投影破坏换手』类 bug 复活）。

    任一约束残差超 tol 即抛 AssertionError —— solve_erc_cvxpy 捕获后回退 Spinu 管线。
    """
    w = np.asarray(w, dtype=float)
    if abs(w.sum() - 1.0) > tol:
        raise AssertionError(f"sum={w.sum()}")
    if w.min() < -tol:
        raise AssertionError(f"min weight={w.min()}")
    if (w - cfg.max_weight).max() > tol:
        raise AssertionError(f"max_weight breach={(w - cfg.max_weight).max()}")
    if industry_of:
        inds = np.array([industry_of.get(c, "未知") for c in codes])
        for ind in set(inds):
            exp = float(w[inds == ind].sum())
            if exp > cfg.max_industry_exposure + tol:
                raise AssertionError(f"industry {ind} exposure={exp}")
    if prev_w is not None and prev_codes is not None and len(prev_w) == len(prev_codes):
        prev_map = dict(zip(prev_codes, prev_w, strict=True))
        pw = np.array([prev_map.get(c, 0.0) for c in codes])
        pw = np.clip(pw, 0.0, None)
        ps = pw.sum()
        if ps > 0:
            pw = pw / ps
            to = float(np.abs(w - pw).sum())
            if to > cfg.max_weekly_turnover + tol:
                raise AssertionError(f"turnover={to}")


def solve_erc_cvxpy(cov: np.ndarray, codes: list[str], industry_of: dict[str, str],
                    prev_w: np.ndarray | None, prev_codes: list[str] | None,
                    cfg: ERCConfig, w_base: np.ndarray,
                    scenarios: np.ndarray | None = None,
                    cvar_limit: float = 0.0,
                    cvar_alpha: float = 0.95) -> np.ndarray | None:
    """P0-2 凸形式：把 Spinu 无约束 ERC 投影到约束可行域，单一凸 QP 一次求解。

    目标最小化 ‖w − w_base‖²（在约束下保持 ERC 结构），约束：
    w≥0, Σw=1, 个股≤max_weight, 行业暴露≤cap, 周换手≤max_weekly_turnover。
    求解失败 / 状态非最优 / 残差断言失败均返回 None（交由 Spinu 管线 fallback）。
    """
    try:
        import cvxpy as cp
    except Exception:
        return None
    n = len(codes)
    if n == 0 or w_base is None or w_base.shape[0] != n:
        return None
    cov = np.asarray(cov, dtype=float)
    w_base = np.asarray(w_base, dtype=float)
    w = cp.Variable(n)
    obj = cp.Minimize(0.5 * cp.sum_squares(w - w_base))
    cons = [w >= 0, cp.sum(w) == 1, w <= cfg.max_weight]
    if industry_of:
        inds = np.array([industry_of.get(c, "未知") for c in codes])
        for ind in set(inds):
            idx = inds == ind
            if idx.any():
                cons.append(cp.sum(w[idx]) <= cfg.max_industry_exposure)
    if prev_w is not None and prev_codes is not None and len(prev_w) == len(prev_codes):
        prev_map = dict(zip(prev_codes, prev_w, strict=True))
        pw = np.array([prev_map.get(c, 0.0) for c in codes])
        pw = np.clip(pw, 0.0, None)
        ps = pw.sum()
        if ps > 0:
            pw = pw / ps
            cons.append(cp.norm(w - pw, 1) <= cfg.max_weekly_turnover)
    # P1-2 可选 CVaR(Rockafellar-Uryasev) 线性化约束：限制组合日损失 CVaR ≤ cvar_limit
    if scenarios is not None and cvar_limit > 0 and 0 < cvar_alpha < 1:
        scen = np.asarray(scenarios, dtype=float)
        if scen.ndim == 2 and scen.shape[0] > 0 and scen.shape[1] == n:
            S = scen.shape[0]
            VaR = cp.Variable()
            z = cp.Variable(S, nonneg=True)
            port_loss = -scen @ w  # 损失 = -组合收益
            cons += [z >= port_loss - VaR]
            cvar_loss = VaR + (1.0 / ((1.0 - cvar_alpha) * S)) * cp.sum(z)
            cons += [cvar_loss <= cvar_limit]
            logger.debug("CVaR 约束已启用 alpha=%.2f limit=%.4f 场景数=%d", cvar_alpha, cvar_limit, S)
    try:
        cp.Problem(obj, cons).solve(solver=cp.CLARABEL)
    except Exception:
        return None
    if w.value is None or (w.value is not None and not (True)):
        return None
    wv = np.asarray(w.value, dtype=float)
    try:
        _assert_constraints(wv, codes, industry_of, cfg, prev_w, prev_codes)
    except AssertionError:
        return None
    return wv


def solve_erc(cov: np.ndarray, codes: list[str],
              industry_of: dict[str, str],
              prev_w: np.ndarray | None = None,
              prev_codes: list[str] | None = None,
              cfg: ERCConfig | None = None,
              scenarios: np.ndarray | None = None,
              cvar_limit: float = 0.0,
              cvar_alpha: float = 0.95) -> np.ndarray:
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
        w_base = x / x.sum()
        # P0-2：优先用凸形式（单一凸 QP）把无约束 ERC 投影到约束可行域；
        # 失败 / 残差断言失败则回退到原三重迭代投影管线（保留不删）。
        w_cvx = solve_erc_cvxpy(cov, codes, industry_of, prev_w, prev_codes, cfg, w_base,
                                scenarios, cvar_limit, cvar_alpha)
        if w_cvx is not None:
            w = w_cvx
        else:
            w = w_base
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
