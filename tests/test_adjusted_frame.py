"""读侧咽喉：raw 逐值同盘、hfq=OHLC×当日因子、四条 strict 门禁。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed import adjusted
from tdxfeed.adjusted import AdjustmentError

CODE = "600000"


def _daily(n: int, start=date(2020, 1, 2)) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "open": np.linspace(10.0, 20.0, n),
        "high": np.linspace(10.5, 20.5, n),
        "low": np.linspace(9.5, 19.5, n),
        "close": np.linspace(10.0, 20.0, n),
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })


def _factor(daily: pd.DataFrame, values, start: int = 0, stop: int | None = None) -> pd.DataFrame:
    """按日线切片造因子表：raw_close 必须等于日线 close，否则会触发同码双文件门禁。"""
    sub = daily.iloc[start:stop]
    v = np.asarray(values, dtype=float)
    assert len(v) == len(sub)
    return pd.DataFrame({"code": CODE, "date": list(sub["date"]), "hfq_factor": v,
                         "raw_close": sub["close"].to_numpy(dtype=float),
                         "hfq_close": sub["close"].to_numpy(dtype=float) * v})


def _wire(monkeypatch, tmp_path, daily: pd.DataFrame, factor: pd.DataFrame | None):
    d = tmp_path / "daily"; d.mkdir()
    dfp = d / f"sh{CODE}.parquet"
    daily.to_parquet(dfp, index=False)
    fdir = tmp_path / "hfq_factor"; fdir.mkdir()
    fp = fdir / f"{CODE}.parquet"
    if factor is not None:
        factor.to_parquet(fp, index=False)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: dfp)
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: fp)
    return dfp, fp


def test_raw_is_identical_to_disk(monkeypatch, tmp_path):
    daily = _daily(40)
    dfp, _ = _wire(monkeypatch, tmp_path, daily, None)
    got = adjusted.frame(CODE, kind="raw")
    pd.testing.assert_frame_equal(got, pd.read_parquet(dfp))


def test_hfq_multiplies_ohlc_only(monkeypatch, tmp_path):
    daily = _daily(40)
    fac = _factor(daily, np.where(np.arange(40) < 20, 1.0, 1.5))
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq")
    assert got.attrs["kind"] == "hfq"
    assert got.attrs["factor_cov"] == 1.0
    np.testing.assert_allclose(got["close"], daily["close"] * np.where(np.arange(40) < 20, 1.0, 1.5))
    np.testing.assert_allclose(got["open"], daily["open"] * np.where(np.arange(40) < 20, 1.0, 1.5))
    np.testing.assert_allclose(got["volume"], daily["volume"])
    np.testing.assert_allclose(got["amount"], daily["amount"])
    assert list(got["date"]) == list(daily["date"])


def test_missing_factor_file_is_factor_one_and_flagged(monkeypatch, tmp_path):
    daily = _daily(40)
    _wire(monkeypatch, tmp_path, daily, None)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])
    assert got.attrs["factor_missing"] is True
    assert got.attrs["factor_cov"] == 0.0
    with pytest.raises(AdjustmentError, match="缺"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_coverage_gate(monkeypatch, tmp_path):
    daily = _daily(1000)
    fac = _factor(daily, np.full(997, 1.2), start=3)
    _wire(monkeypatch, tmp_path, daily, fac)
    assert adjusted.frame(CODE, kind="hfq", strict=False).attrs["factor_cov"] < adjusted.MIN_FACTOR_COV
    with pytest.raises(AdjustmentError, match="factor_cov"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_head_lag_gate_on_long_history(monkeypatch, tmp_path):
    daily = _daily(10000)
    fac = _factor(daily, np.full(9994, 1.2), start=6)
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    assert got.attrs["factor_cov"] >= adjusted.MIN_FACTOR_COV   # 覆盖率过关
    assert got.attrs["factor_head_lag"] == 6
    with pytest.raises(AdjustmentError, match="factor_first"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_tail_lag_gate(monkeypatch, tmp_path):
    daily = _daily(100)
    fac = _factor(daily, np.full(98, 1.2), stop=-2)
    _wire(monkeypatch, tmp_path, daily, fac)
    with pytest.raises(AdjustmentError, match="factor_last"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_factor_raw_close_mismatch_is_treated_as_missing(monkeypatch, tmp_path):
    """sh000001 的裸 6 位因子文件其实是 000001 平安银行：raw_close 对不上就绝不能乘。"""
    daily = _daily(40)
    fac = _factor(daily, np.full(40, 1.2))
    fac["raw_close"] = fac["raw_close"] * 0.5      # 中位偏差 50%，远超 1%
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])   # 没有乘 1.2
    assert got.attrs["factor_mismatch"] is True
    assert got.attrs["factor_missing"] is True
    assert got.attrs["factor_raw_dev"] > adjusted.FACTOR_RAW_TOL
    with pytest.raises(AdjustmentError, match="raw_close"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_never_falls_back_to_adj_factor(monkeypatch, tmp_path):
    daily = _daily(40)
    _wire(monkeypatch, tmp_path, daily, None)
    adj_dir = tmp_path / "adj_factor"; adj_dir.mkdir()
    pd.DataFrame({"date": daily["date"], "adj_factor": np.full(40, 2.0)}).to_parquet(
        adj_dir / f"{CODE}.parquet", index=False)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])   # 没有乘 2.0


def test_unknown_kind_raises(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _daily(40), None)
    with pytest.raises(ValueError):
        adjusted.frame(CODE, kind="qfq")
