"""同一除权窗口：raw 标签吃到假止损，hfq 标签连续。"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import train_pnl_proxy as tpp  # noqa: E402


def _bars(closes, factor):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    raw = pd.DataFrame({
        "date": pd.to_datetime([date(2020, 1, 2) + timedelta(days=i) for i in range(n)]),
        "open": closes * 1.0,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })
    hfq = raw.copy()
    for c in ("open", "high", "low", "close"):
        hfq[c] = raw[c] * np.asarray(factor, dtype=float)
    return raw, hfq


def _with_attrs(df: pd.DataFrame, **kw) -> pd.DataFrame:
    """DataFrame.assign(attrs=...) 建的是列；frame attrs 只能这样挂。"""
    out = df.copy()
    out.attrs.update(kw)
    return out


def test_raw_label_takes_fake_stop_hfq_label_does_not(monkeypatch):
    # 第 2 行是除权日：raw 从 10 跌到 6.8（−32%），hfq 连续
    closes = [10.0, 10.0, 6.8, 6.9, 7.0, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7]
    factor = [1.0, 1.0] + [10 / 6.8] * 10
    raw, hfq = _bars(closes, factor)
    monkeypatch.setattr(tpp, "_load_one", lambda code: raw)
    monkeypatch.setattr(tpp, "adj_frame",
                        lambda code, kind, strict: _with_attrs(hfq, factor_cov=1.0))
    pnl_raw = tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "raw")
    pnl_hfq = tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "hfq")
    assert pnl_raw < -0.09          # 假止损
    assert pnl_hfq > pnl_raw + 0.2  # 连续路径不触发止损


def test_hfq_label_missing_factor_returns_none(monkeypatch):
    raw, _ = _bars([10.0] * 12, [1.0] * 12)
    monkeypatch.setattr(tpp, "_load_one", lambda code: raw)
    monkeypatch.setattr(tpp, "adj_frame",
                        lambda code, kind, strict: _with_attrs(raw, factor_cov=0.5))
    assert tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "hfq") is None


def test_recompute_writes_separate_file_for_hfq(monkeypatch, tmp_path):
    import recompute_pnl_labels as rpl
    src = tmp_path / "in.csv"
    pd.DataFrame({
        "code": ["600000", "600001"], "signal_date": ["20200102", "20200103"],
        "f": [1.0, 2.0], "hypothetical_pnl": [0.1, 0.2],
    }).astype({"code": str}).to_csv(src, index=False)
    out = tmp_path / "out.csv"
    monkeypatch.setattr(rpl, "daily_files", lambda: [tmp_path / "600000.parquet",
                                                     tmp_path / "600001.parquet"])
    monkeypatch.setattr(rpl, "attach_pnl",
                        lambda df, max_workers=8, price_basis="raw": df.assign(
                            hypothetical_pnl=lambda d: d["code"].map(
                                {"600000": 0.05, "600001": 0.06})))
    assert rpl.main(["--price-basis", "hfq", "--src", str(src), "--out", str(out)]) == 0
    got = pd.read_csv(out)
    assert list(got["hypothetical_pnl"]) == pytest.approx([0.05, 0.06])
    assert "hypothetical_pnl" in got.columns
    assert str(out).endswith("out.csv")
