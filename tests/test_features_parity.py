"""features_5d 是 26/29 维唯一实现：polars 复刻逐位一致，第三份副本只是取键。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import feature_engineering as fe  # noqa: E402

from tdxfeed.factors_polars import compute_window_features_extended_polars  # noqa: E402
from tdxfeed.features_5d import compute_window_features_extended  # noqa: E402

V9_KEYS = fe.FEATURE_BASE + [name for name, _ in fe.NEW_FEATURES]


def bars(n: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10 * (1 + np.cumsum(rng.normal(0, 0.012, n)))
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=n),
        "open": close * (1 + rng.normal(0, 0.004, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.008, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.008, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        "amount": close * rng.integers(100_000, 1_000_000, n),
    })


@pytest.mark.parametrize("sig_idx", [-1, -3])
def test_pandas_and_polars_agree_on_29_keys(sig_idx):
    df = bars()
    a = compute_window_features_extended(df, "sh600519", sig_idx=sig_idx)
    b = compute_window_features_extended_polars(pl.from_pandas(df), "sh600519", sig_idx=sig_idx)
    assert a is not None and b is not None
    assert len(a) == len(b) == 29
    assert set(a) == set(b)
    for key in a:
        assert a[key] == pytest.approx(b[key], rel=1e-12, abs=1e-12, nan_ok=True), key


def test_third_copy_is_a_projection_of_the_authority():
    df = bars()
    got = fe.build_extended_features(df, "sh600519")
    ext = compute_window_features_extended(df, "sh600519")
    assert list(got) == V9_KEYS
    assert len(got) == 26
    for key in V9_KEYS:
        assert got[key] == pytest.approx(ext[key], rel=1e-12, abs=1e-12, nan_ok=True), key


def test_short_frame_returns_none():
    df = bars(n=4)
    assert compute_window_features_extended(df, "sh600519") is None
    assert fe.build_extended_features(df, "sh600519") is None


def test_third_copy_has_no_own_math():
    """结构性断言：数值早就一致，能抓到的回归是「有人把自算又抄回来」。"""
    src = inspect.getsource(fe.build_extended_features)
    assert "compute_window_features_extended(" in src
    for token in ("pct_change", "daily_range", "amount_3d_mean", "dist_high", "amt_per_price"):
        assert token not in src, token
