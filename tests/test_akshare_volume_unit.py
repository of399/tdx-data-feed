"""akshare 两条分支的 volume 单位必须各自换算，不能一刀切。

Sina `stock_zh_a_daily` 的 volume 是股，东财 `stock_zh_a_hist` 的成交量是手。
fetch_bars 曾对两条分支统一 ÷100，回退到东财时 volume 小 100 倍。
"""
from __future__ import annotations

NEEDS_DATA = False

import pandas as pd
import pytest

ak = pytest.importorskip("akshare")

from tdxfeed.sources import akshare_source  # noqa: E402


def _frame(volume: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-10-28"],
        "open": [9.8], "high": [9.93], "low": [9.73], "close": [9.85],
        "volume": [volume], "amount": [647276.35],
    })


def _source(monkeypatch, sina, em):
    src = akshare_source.AkshareSource()
    monkeypatch.setattr(akshare_source.time, "sleep", lambda s: None)
    monkeypatch.setattr(src, "_fetch_sina", sina)
    monkeypatch.setattr(src, "_fetch_em", em)
    return src


def test_sina_volume_is_converted_from_shares(monkeypatch):
    src = _source(monkeypatch, lambda code: _frame(1000000), lambda code: _frame(1))
    bars = src.fetch_bars("sh600000")
    assert bars[0][5] == 10000.0   # 股 → 手


def test_em_fallback_volume_is_already_hands(monkeypatch):
    def boom(code):
        raise RuntimeError("sina down")

    src = _source(monkeypatch, boom, lambda code: _frame(10000))
    bars = src.fetch_bars("sh600000")
    assert bars[0][5] == 10000.0   # 手原样保留，不再 ÷100
