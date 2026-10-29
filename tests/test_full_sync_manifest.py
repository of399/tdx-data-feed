"""manifest.last_date 必须是 8 位 YYYYMMDD 字符串：bars 是 list-of-list，不能取整行 repr。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tdxfeed.full_sync import manifest_last


def test_manifest_last_uses_bar_date():
    bars = [[datetime(2026, 10, 28, 0, 0), 9.8, 9.93, 9.73, 9.85, 647276.35, 0.0]]
    assert manifest_last(bars) == "20261028"


def test_manifest_last_is_digits_only():
    bars = [[datetime(2026, 1, 5, 0, 0), 1, 1, 1, 1, 1, 1]]
    assert manifest_last(bars) == "20260105"


def test_manifest_last_rejects_empty():
    with pytest.raises(ValueError):
        manifest_last([])


def test_manifest_last_takes_latest_bar():
    """两根 bar 时取更晚的那根：改成 bars[0] 会红。"""
    bars = [
        [datetime(2026, 1, 5, 0, 0), 1, 1, 1, 1, 1, 1],
        [datetime(2026, 1, 6, 0, 0), 2, 2, 2, 2, 2, 2],
    ]
    assert manifest_last(bars) == "20260106"


def test_full_sync_call_sites_route_through_manifest_last():
    """源码级守卫：调用点回退成内联旧写法（整行 repr）会红。"""
    src = (Path(__file__).resolve().parents[1] / "tdxfeed" / "full_sync.py").read_text(
        encoding="utf-8"
    )
    assert "last = manifest_last(bars)" in src
    assert "str(bars[-1])" not in src
