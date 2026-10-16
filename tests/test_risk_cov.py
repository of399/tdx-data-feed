"""P1-1 协方差估计升级：OAS/EWMA 选项 + 条件数/最小特征值诊断。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.multifactor.risk_model import (
    RiskModel,
    _ewma_cov,
    cov_diagnostics,
    ledoit_wolf_cov,
)


def _panel(n: int) -> pd.DataFrame:
    codes = [f"{i:06d}" for i in range(n)]
    return pd.DataFrame(
        {
            "amount_20d_avg": np.full(n, 1e8),
            "val_proxy": np.linspace(-5, 5, n),
            "pe_ttm": np.linspace(5, 50, n),
            "mom_60d": np.linspace(-0.3, 0.3, n),
        },
        index=codes,
    )


def test_ewma_cov_psd():
    rng = np.random.default_rng(1)
    F = rng.standard_normal((100, 6)) * 0.02
    S = _ewma_cov(F, 0.94)
    assert S.shape == (6, 6)
    assert (np.linalg.eigvalsh(S) > -1e-10).all()


def test_factor_cov_methods_psd():
    rng = np.random.default_rng(2)
    F = rng.standard_normal((120, 8)) * 0.02
    rm = RiskModel()
    for m in ("lw", "ewma", "oas"):
        S = rm._factor_cov(F, m, 0.94)
        assert (np.linalg.eigvalsh(S) > -1e-8).all(), m


def test_estimate_cov_methods_run_and_psd():
    rng = np.random.default_rng(3)
    n = 30
    panel = _panel(n)
    dates = pd.date_range("2024-01-01", periods=120, freq="B")
    rets = pd.DataFrame(
        rng.standard_normal((120, n)) * 0.02, index=dates, columns=list(panel.index)
    )
    rm = RiskModel()
    for m in ("lw", "ewma", "oas"):
        S = rm.estimate_cov(rets, panel, cov_method=m, ewma_lambda=0.94)
        assert S.shape == (n, n)
        dd = cov_diagnostics(S)
        assert np.isfinite(dd["cond"]) or dd["cond"] == float("inf")
        # PSD clip 后最小特征值必须 > 0
        assert (np.linalg.eigvalsh(S) > 0).all()


def test_cov_diagnostics_cond():
    S = np.diag([1.0, 1e-4])  # cond ≈ 1e4
    d = cov_diagnostics(S)
    assert d["cond"] == 1e4
    assert d["min_eig"] == 1e-4
    assert d["n"] == 2


def test_ledoit_wolf_psd():
    rng = np.random.default_rng(4)
    X = rng.standard_normal((80, 5))
    S = ledoit_wolf_cov(X)
    assert (np.linalg.eigvalsh(S) > -1e-8).all()
