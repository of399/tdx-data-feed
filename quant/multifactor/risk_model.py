"""Barra 式风险模型（近似实现，v1）。

实现要点：
- 行业暴露：申万一级哑变量（data/industry/sw_stock_industry.csv，取 industry_level==1）。
- 风格暴露：规模(size)=log(20日成交额)、价值(value)=估值代理/PE、动量(momentum)=mom_60d。
- 因子协方差 Σf 由「截面回归估计因子收益序列」后取协方差，并用 Ledoit-Wolf 收缩保证 PSD。
- 特质风险 D 为回归残差方差（对角）。
- 个股风险贡献 RC_i = w_i (Σw)_i，供 ERC 优化与暴露监控。
- 维度仅在「入选组合」（默认 30 只）上估计，30×30 量级，数值稳定且廉价。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from sklearn.covariance import OAS

    HAS_OAS = True
except Exception:  # pragma: no cover - sklearn 为可选依赖
    OAS = None
    HAS_OAS = False

ROOT = Path(__file__).resolve().parent.parent.parent
IND_CSV = ROOT / "data" / "industry" / "sw_stock_industry.csv"


class IndustryMap:
    def __init__(self):
        self.map: dict[str, str] = {}
        try:
            d = pd.read_csv(IND_CSV, dtype=str)
            d = d[d["industry_level"] == "1"]
            for _, row in d.iterrows():
                raw = str(row["code"])
                for _p in ("sh", "sz", "bj"):
                    if raw.startswith(_p):
                        raw = raw[len(_p):]
                        break
                code = raw.zfill(6)
                self.map[code] = str(row.get("industry_name", "未知"))
        except Exception:
            pass

    def get(self, code: str) -> str:
        return self.map.get(str(code).zfill(6), "未知")


def ledoit_wolf_cov(X: np.ndarray, shrinkage: float | None = None) -> np.ndarray:
    """Ledoit-Wolf 常数相关收缩估计（无 sklearn 依赖）。

    X: (T, K) 样本。返回 (K, K) 收缩协方差。
    """
    T, K = X.shape
    if T < 2 or K == 0:
        return np.eye(K) * 1e-6
    X = X - X.mean(axis=0)
    S = (X.T @ X) / (T - 1)
    var = np.diag(S)
    d = np.sqrt(var)
    d[d == 0] = 1e-12
    corr = S / np.outer(d, d)
    rho = (corr - np.diag(np.diag(corr))).sum() / (K * (K - 1)) if K > 1 else 0.0
    F = np.diag(var).copy()
    if K > 1:
        F = F + rho * (np.sqrt(np.outer(var, var)) - np.diag(var))
    if shrinkage is None:
        pi_ = np.sum((S - F) ** 2)
        gamma_ = np.sum(np.diag(S) ** 2) - np.sum(np.diag(S)) ** 2 / K if K > 0 else 0.0
        shrinkage = np.clip(pi_ / (pi_ + gamma_ + 1e-12), 0.0, 1.0)
        if not np.isfinite(shrinkage):
            shrinkage = 0.2
    return (1 - shrinkage) * S + shrinkage * F


def _ewma_cov(F: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """RiskMetrics 风格 EWMA 协方差（λ=0.94，与 sim_daily_compare.py 口径一致）。

    F: (T, P) 因子收益。权重 w_t = λ^{T-1-t}（最新为 1）归一化；
    减去样本均值后做指数加权外积，对称化输出（PSD 由调用方 clip 保证）。
    """
    F = np.asarray(F, dtype=float)
    T, P = F.shape
    if T < 2 or P == 0:
        return np.eye(P) * 1e-6
    w = lam ** np.arange(T - 1, -1, -1, dtype=float)
    w = w / w.sum()
    d = F - F.mean(axis=0)
    Sigma = (d * w[:, None]).T @ d
    Sigma = np.nan_to_num(Sigma, nan=0.0)
    return (Sigma + Sigma.T) / 2


def cov_diagnostics(Sigma: np.ndarray) -> dict:
    """量化协方差病态程度：{cond, min_eig, max_eig, n}。"""
    Sigma = np.asarray(Sigma, dtype=float)
    n = Sigma.shape[0]
    try:
        ev = np.linalg.eigvalsh(Sigma)  # 升序
        min_eig = float(ev[0])
        max_eig = float(ev[-1])
        cond = float(max_eig / min_eig) if min_eig > 0 else float("inf")
    except np.linalg.LinAlgError:
        min_eig = max_eig = cond = float("nan")
    return {"cond": cond, "min_eig": min_eig, "max_eig": max_eig, "n": n}


class RiskModel:
    def __init__(self, industry: IndustryMap | None = None):
        self.industry = industry or IndustryMap()

    def build_exposures(self, panel: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        """panel: index=code, columns 含 amount_20d_avg/val_proxy/pe_ttm/mom_60d。
        返回 (B, factor_names)。"""
        codes = list(panel.index)
        size = np.log(panel["amount_20d_avg"].replace(0, np.nan)).fillna(0)
        val = panel.get("val_proxy")
        if val is None or pd.to_numeric(val, errors="coerce").isna().all():
            pe = pd.to_numeric(panel.get("pe_ttm"), errors="coerce")
            val = -pe
        val = pd.to_numeric(val, errors="coerce").fillna(0)
        mom = panel["mom_60d"].fillna(0)

        def z(s):
            s = pd.to_numeric(s, errors="coerce")
            mu, sd = s.mean(), s.std()
            return ((s - mu) / sd).fillna(0) if sd and np.isfinite(sd) and sd > 0 else s * 0.0

        style = pd.DataFrame(
            {"size": z(size), "value": z(val), "momentum": z(mom)},
            index=codes,
        )
        inds = [self.industry.get(c) for c in codes]
        ind_names = sorted(set(inds))
        ind_df = pd.DataFrame(0.0, index=codes, columns=ind_names)
        for c, ind in zip(codes, inds, strict=True):
            if ind in ind_names:
                ind_df.loc[c, ind] = 1.0
        B = pd.concat([ind_df, style], axis=1)
        return B, list(B.columns)

    def _factor_cov(self, F: np.ndarray, cov_method: str, ewma_lambda: float) -> np.ndarray:
        """对因子收益序列 F(T×P) 估计 P×P 协方差；支持 lw(默认)/oas/ewma。"""
        if cov_method == "oas" and HAS_OAS:
            try:
                return np.asarray(OAS().fit(F).covariance_, dtype=float)
            except Exception:
                logger.warning("OAS 估计失败，回退 Ledoit-Wolf")
        if cov_method == "ewma":
            return _ewma_cov(F, ewma_lambda)
        return ledoit_wolf_cov(F)

    def estimate_cov(self, rets: pd.DataFrame, panel: pd.DataFrame,
                     cov_method: str = "lw", ewma_lambda: float = 0.94) -> np.ndarray:
        """rets: (T, N) 日收益，index=date, columns=codes（入选组合）。
        panel: 当前截面因子面板（用于 B）。返回 N×N 协方差。"""
        codes = list(rets.columns)
        n = len(codes)
        sub = panel.reindex(codes)
        if sub.isna().all().all() or n < 2:
            return np.eye(n) * 1e-4
        B, _ = self.build_exposures(sub)
        Bv = np.nan_to_num(B.values.astype(float), nan=0.0, posinf=1e-6, neginf=-1e-6)
        R = np.nan_to_num(rets.values.astype(float), nan=0.0)
        T = R.shape[0]
        if T < 5:
            return np.eye(n) * 1e-4
        BtB = Bv.T @ Bv
        BtB = np.nan_to_num(BtB, nan=0.0, posinf=1e-6, neginf=-1e-6)
        BtB_inv = np.linalg.pinv(BtB)
        F = (BtB_inv @ Bv.T @ R.T).T
        F = np.nan_to_num(F, nan=0.0)
        resid = R.T - Bv @ F.T
        D = np.nan_to_num(np.var(resid, axis=1, ddof=1), nan=1e-8)
        D = np.clip(D, 1e-8, None)
        Sigma_f = self._factor_cov(F, cov_method=cov_method, ewma_lambda=ewma_lambda)
        Sigma_f = np.nan_to_num(Sigma_f, nan=0.0) + np.eye(Sigma_f.shape[0]) * 1e-8
        Sigma = Bv @ Sigma_f @ Bv.T + np.diag(D)
        Sigma = (Sigma + Sigma.T) / 2
        Sigma = np.nan_to_num(Sigma, nan=0.0)
        try:
            w, V = np.linalg.eigh(Sigma)
            w = np.clip(w, 1e-8, None)
            Sigma = V @ np.diag(w) @ V.T
        except np.linalg.LinAlgError:
            Sigma = np.diag(np.clip(np.diag(Sigma), 1e-8, None))
        diag = cov_diagnostics(Sigma)
        logger.debug("estimate_cov method=%s n=%d cond=%.3g min_eig=%.3g",
                     cov_method, Sigma.shape[0], diag["cond"], diag["min_eig"])
        return Sigma

    @staticmethod
    def risk_contributions(weights: np.ndarray, cov: np.ndarray) -> np.ndarray:
        port_var = weights @ cov @ weights
        mrc = cov @ weights
        rc = weights * mrc
        return rc / port_var if port_var > 0 else rc

    def industry_exposure(self, weights: pd.Series, top_n_codes: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for c, w in zip(top_n_codes, weights, strict=True):
            ind = self.industry.get(c)
            out[ind] = out.get(ind, 0.0) + float(w)
        return out
