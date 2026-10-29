"""宽表按口径取特征：hfq 宽表用 hfq 帧 + hfq 标签，缺因子的票整只跳过。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed import adjusted, stage1_wide_table as swt


def _daily(path: Path, closes, factor=1.0):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": closes * 0.995,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })
    df.to_parquet(path, index=False)
    return df


def test_hfq_wide_table_uses_hfq_features_and_labels(monkeypatch, tmp_path):
    d = tmp_path / "daily"; d.mkdir()
    f = tmp_path / "hfq"; f.mkdir()
    closes = [10.0] * 5 + [7.0, 7.1, 7.8, 8.0, 8.2]
    raw = _daily(d / "sh600000.parquet", closes)
    fac = pd.DataFrame({
        "code": "600000", "date": raw["date"],
        "hfq_factor": [1.0] * 5 + [10 / 7] * 5,
        "raw_close": closes, "hfq_close": [c * k for c, k in
                                           zip(closes, [1.0] * 5 + [10 / 7] * 5, strict=True)],
    })
    fac.to_parquet(f / "600000.parquet", index=False)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d)

    src = tmp_path / "src.csv"
    # code 必须带市场前缀：装配按 DAILY / f"{code}.parquet" 定位；
    # signal_date 必须带连字符：_str_to_date 按 s[:4]/s[5:7]/s[8:10] 切。
    pd.DataFrame({
        "code": ["sh600000"], "signal_date": ["2020-01-10"],
        "is_positive": [1], "hypothetical_pnl": [0.42],
        **{k: [0.0] for k in swt.FEAT26_ORDER},
    }).to_csv(src, index=False)
    out = tmp_path / "wide.parquet"
    swt.build_wide_table(src_csv=src, out_parquet=out, sample_codes=None,
                         verify_parity=True, price_basis="hfq")
    got = pd.read_parquet(out)
    assert len(got) == 1
    assert float(got["hypothetical_pnl"].iloc[0]) == 0.42
    # 除权日若按 raw 算，ret_5d 会是 −30% 附近；hfq 下应为正
    assert float(got["ret_5d"].iloc[0]) > 0


def test_missing_factor_drops_the_symbol(monkeypatch, tmp_path):
    d = tmp_path / "daily"; d.mkdir()
    f = tmp_path / "hfq"; f.mkdir()
    raw = _daily(d / "sh600000.parquet", [10.0] * 10)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d)
    src = tmp_path / "src.csv"
    pd.DataFrame({"code": ["sh600000"], "signal_date": ["2020-01-10"],
                  "is_positive": [1], "hypothetical_pnl": [0.42],
                  **{k: [0.0] for k in swt.FEAT26_ORDER}}).to_csv(src, index=False)
    out = tmp_path / "wide.parquet"
    stats = swt.build_wide_table(src_csv=src, out_parquet=out, sample_codes=None,
                                 verify_parity=True, price_basis="hfq")
    assert stats["n_hfq_skip"] == 1
    assert len(pd.read_parquet(out)) == 0
