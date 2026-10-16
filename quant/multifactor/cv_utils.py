"""过拟合防御工具：purged + embargo 时序交叉验证、IC 分年衰减。

说明：
- purged_embargo_splits：将有序时间序列切分为训练/测试折，并在测试集两侧
  留出 embargo 间隙，避免重叠样本泄漏（Lopez de Prado 范式）。
- ic_by_year：按年聚合 IC，用于衰减检验（逐年 IC 转负/衰减>50% 报警）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CVConfig:
    n_splits: int = 5
    embargo: int = 5          # 折间 embargo 交易日数


def purged_embargo_splits(n: int, cfg: CVConfig | None = None):
    """返回 list[(train_idx, test_idx)]，基于顺序切分 + embargo 剔除。"""
    cfg = cfg or CVConfig()
    if n < cfg.n_splits * 2:
        return []
    fold = n // cfg.n_splits
    splits = []
    for i in range(cfg.n_splits):
        # 测试窗（顺序）
        te_start = i * fold
        te_end = te_start + fold if i < cfg.n_splits - 1 else n
        tr_mask = np.ones(n, dtype=bool)
        # 测试窗
        tr_mask[te_start:te_end] = False
        # embargo：测试窗前后各 embargo 个样本从训练剔除
        lo = max(0, te_start - cfg.embargo)
        hi = min(n, te_end + cfg.embargo)
        tr_mask[lo:hi] = False
        splits.append((np.where(tr_mask)[0], np.arange(te_start, te_end)))
    return splits


def ic_by_year(ic_records: list[dict]) -> pd.DataFrame:
    """ic_records: list of {'date':Timestamp, 'ic':float, 'factor':str}。
    返回按年 × 因子的 IC 均值表。"""
    df = pd.DataFrame(ic_records)
    if df.empty:
        return pd.DataFrame()
    df["year"] = pd.to_datetime(df["date"]).dt.year
    out = df.pivot_table(index="year", columns="factor", values="ic", aggfunc="mean")
    return out


def icir(ic_series: pd.Series) -> float:
    """IC 信息比率 = mean/std，样本不足返回 0。"""
    s = ic_series.dropna()
    if len(s) < 5 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std())


def ic_newey_west(ic_series: pd.Series, maxlags: int = 5) -> dict:
    """Newey-West HAC 检验：IC 序列均值是否显著 ≠ 0（IC 强自相关，OLS 标准误会低估）。

    返回 {n, mean_ic, se_hac, t_stat, p_value}；样本不足返回 p=nan。
    """
    from statsmodels.api import OLS

    s = ic_series.dropna()
    n = len(s)
    if n < 10:
        return {"n": n, "mean_ic": float(s.mean()) if n else 0.0,
                "se_hac": float("nan"), "t_stat": float("nan"), "p_value": float("nan")}
    model = OLS(s.values, np.ones((n, 1))).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    return {"n": n, "mean_ic": float(s.mean()),
            "se_hac": float(model.bse[0]), "t_stat": float(model.tvalues[0]),
            "p_value": float(model.pvalues[0])}


def bh_fdr_correct(pvals, alpha: float = 0.05):
    """Benjamini-Hochberg FDR 校正（P0-1 多重检验校正核心）。

    对一组裸 p 值做 BH 控制错误发现率，返回校正后 q 值（= 经 BH 调整后的最小 FDR）
    与拒绝掩码（q < alpha）。用于在「N 个因子同时做单点检验」时压低多重检验假阳性。

    性质：
    - q 值对排序后的 p 值单调非减；`reject` 当且仅当 `q < alpha`。
    - 含 `nan` 的 p 值不参与校正：对应 `q=nan`、`reject=False`（不误判也不漏判）。

    参数：
        pvals: 裸 p 值序列（list / ndarray / Series）。
        alpha: 目标 FDR 水平，默认 0.05。

    返回：
        (reject, qvals)：两个等长 ndarray；reject 为 bool，qvals 为 float（nan 处为 nan）。
    """
    from statsmodels.stats.multitest import multipletests

    p = np.asarray(pvals, dtype=float)
    if p.ndim != 1:
        p = p.ravel()
    n = p.shape[0]
    reject = np.zeros(n, dtype=bool)
    qvals = np.full(n, float("nan"), dtype=float)
    mask = ~np.isnan(p)
    n_valid = int(mask.sum())
    if n_valid == 0:
        return reject, qvals
    _, q_corr, _, _ = multipletests(p[mask], method="fdr_bh", alpha=alpha)
    qvals[mask] = q_corr
    reject[mask] = q_corr < alpha
    return reject, qvals


def _lag1_autocorr(x: np.ndarray) -> float:
    """日收益滞后-1 自相关（序列依赖 / 时序动量结构度量）。"""
    x = np.asarray(x, dtype=float)
    if len(x) < 3 or np.std(x) == 0:
        return 0.0
    return float(np.corrcoef(x[:-1], x[1:])[0, 1])


def permutation_return_test(returns, stat_fn=None, b: int = 2000, seed: int = 42) -> dict:
    """置换检验（P0-1 ④：H₀ 策略日盈亏顺序随机 → 无时序/择时 alpha，≈ 随机抽样同分布）。

    对日收益序列做 b 次**无放回随机重排**（打乱时间），每次重算统计量，经验
    p = 重排统计量 ≥ 观测统计量的比例。这是审计 ④ 要求的非参数"假设检验"
    （区别于 `_bootstrap_ci` 的分位 CI 与 `_bootstrap_pvalue` 的 bootstrap-SE z 检验）。

    注意：默认统计量取**滞后-1 自相关**而非夏普/均值——后者对重排不变（加法/缩放交换），
    会使置换检验退化（p 恒为 1）。自相关捕捉"日内盈亏是否成序（momentum/反转结构）"，
    是顺序相关统计量，能在打乱时间后产生有意义的 null 分布。

    判定：
    - `p_one`（单侧）：随机时序 ≥ 观测的比例；小 p ⇒ 观测显著优于随机排序（有时序结构）。
    - `p_two`（两尾，min 法则）：保守双侧显著性，用于"是否显著异于随机"。
    - 对近 iid 噪声序列（无 edge），自相关 ≈ 0，p_two ≈ 1 ⇒ 工具正确"不拒绝"噪声。

    返回：{stat_obs, stat_null_mean, p_one, p_two, b}；样本不足返回全 nan。
    """
    if stat_fn is None:
        stat_fn = _lag1_autocorr  # 默认：日收益滞后-1 自相关（顺序相关）

    rng = np.random.default_rng(seed)
    vals = np.asarray(returns, dtype=float)
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n < 5:
        return {"stat_obs": float("nan"), "stat_null_mean": float("nan"),
                "p_one": float("nan"), "p_two": float("nan"), "b": b}
    obs = float(stat_fn(vals))
    perm = np.empty(b)
    idx = np.arange(n)
    for i in range(b):
        perm[i] = stat_fn(vals[rng.permutation(idx)])
    null_mean = float(perm.mean())
    p_ge = float(np.mean(perm >= obs))   # 单侧：随机重排 ≥ 观测
    p_le = float(np.mean(perm <= obs))
    p_one = min(max(p_ge, 0.0), 1.0)
    p_two = min(max(2.0 * min(p_ge, p_le), 0.0), 1.0)
    return {"stat_obs": obs, "stat_null_mean": null_mean,
            "p_one": p_one, "p_two": p_two, "b": b}
