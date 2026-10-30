"""因子跳变审计：以 hfq 经济收益不变式判定源侧因子异常。

econ = (1+raw_ret)×(1+factor_chg)−1 是除权日的真实经济收益，必在涨跌停带内；
上跳类源错误（302132 的 ×5.98、000981 的 +58%）与晚一天调整（003038 的 +33%）都会被它抓住。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from tdxfeed import adjusted

CODE = "600000"


def _daily(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    return pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })


def _factor(daily: pd.DataFrame, values) -> pd.DataFrame:
    v = np.asarray(values, dtype=float)
    return pd.DataFrame({"code": CODE, "date": list(daily["date"]), "hfq_factor": v,
                         "raw_close": daily["close"].to_numpy(dtype=float),
                         "hfq_close": daily["close"].to_numpy(dtype=float) * v})


def _wire(monkeypatch, tmp_path, daily: pd.DataFrame, factor: pd.DataFrame | None):
    dfp = tmp_path / "daily.parquet"
    daily.to_parquet(dfp, index=False)
    fp = tmp_path / "factor.parquet"
    if factor is not None:
        factor.to_parquet(fp, index=False)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: dfp)
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: fp)


def test_upward_factor_jump_is_suspect(monkeypatch, tmp_path):
    """302132 式：原始价不动、因子 ×6 —— econ +500%，必须判异常。"""
    daily = _daily(np.full(40, 10.0))
    fac = np.where(np.arange(40) < 20, 1.0, 6.0)
    _wire(monkeypatch, tmp_path, daily, _factor(daily, fac))

    got = adjusted.factor_jumps(CODE)
    assert len(got) == 1
    row = got.iloc[0]
    assert str(row["date"])[:10] == str(daily["date"].iloc[20])
    assert row["factor_chg"] == 5.0
    assert row["raw_ret"] == 0.0
    assert row["suspect"] is True or row["suspect"] == True  # noqa: E712
    assert abs(row["econ"] - 5.0) < 1e-12


def test_genuine_ex_right_is_not_suspect(monkeypatch, tmp_path):
    """10 送 10：原始价 −50%、因子 ×2 —— econ 0%，不是异常。"""
    closes = np.r_[np.full(20, 10.0), np.full(20, 5.0)]
    daily = _daily(closes)
    fac = np.where(np.arange(40) < 20, 1.0, 2.0)
    _wire(monkeypatch, tmp_path, daily, _factor(daily, fac))

    got = adjusted.factor_jumps(CODE)
    assert len(got) == 1
    assert got.iloc[0]["suspect"] == False  # noqa: E712
    assert abs(got.iloc[0]["econ"]) < 1e-12


def test_dividend_is_not_suspect(monkeypatch, tmp_path):
    """分红：原始价 −2%、因子 +2.04% —— econ ≈ 0。"""
    closes = np.r_[np.full(20, 10.0), np.full(20, 9.8)]
    daily = _daily(closes)
    fac = np.where(np.arange(40) < 20, 1.0, 1.0204081632653061)
    _wire(monkeypatch, tmp_path, daily, _factor(daily, fac))

    got = adjusted.factor_jumps(CODE)
    assert len(got) == 1
    assert got.iloc[0]["suspect"] == False  # noqa: E712
    assert abs(got.iloc[0]["econ"]) < 1e-9


def test_band_depends_on_market(monkeypatch, tmp_path):
    """+25% 对主板（10% 带）异常，对创业板（20% 带）也超；+15% 只对主板异常。"""
    daily = _daily(np.full(40, 10.0))
    fac = np.where(np.arange(40) < 20, 1.0, 1.15)
    _wire(monkeypatch, tmp_path, daily, _factor(daily, fac))
    assert adjusted.factor_jumps("600000").iloc[0]["suspect"] == True  # noqa: E712
    assert adjusted.factor_jumps("300001").iloc[0]["suspect"] == False  # noqa: E712

    fac = np.where(np.arange(40) < 20, 1.0, 1.25)
    _wire(monkeypatch, tmp_path, daily, _factor(daily, fac))
    assert adjusted.factor_jumps("300001").iloc[0]["suspect"] == True  # noqa: E712


def test_no_factor_file_returns_empty(monkeypatch, tmp_path):
    daily = _daily(np.full(40, 10.0))
    _wire(monkeypatch, tmp_path, daily, None)
    got = adjusted.factor_jumps(CODE)
    assert got.empty
    assert list(got.columns) == ["date", "factor_prev", "factor", "factor_chg",
                                 "raw_ret", "econ", "limit", "suspect"]
