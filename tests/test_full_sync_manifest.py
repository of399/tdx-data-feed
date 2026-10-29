"""manifest.last_date 必须是 int YYYYMMDD：bars 是 list-of-list，不能取整行 repr。"""
from __future__ import annotations

from datetime import datetime

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
