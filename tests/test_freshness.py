"""freshness 单元测试：交易日感知逻辑 + 抽样闸门。

运行:
    venv/bin/python -m pytest tests/test_freshness.py -v
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tdxfeed.freshness import _today, last_trading_day, require_fresh

# ---------- last_trading_day 纯函数 ----------

def test_last_trading_day_weekday():
    # 周二 2026-10-13 -> 周一 2026-10-12
    assert last_trading_day(date(2026, 10, 13)) == date(2026, 10, 12)


def test_last_trading_day_monday_skips_weekend():
    # 周一 2026-10-12 -> 周五 2026-10-09（跳过周六、周日）
    assert last_trading_day(date(2026, 10, 12)) == date(2026, 10, 9)


def test_last_trading_day_holiday():
    # 2026-10-09 为周五；其前一个交易日 2026-10-08（周四）设为休市 -> 跳过 -> 2026-10-07（周三）
    hol = {"2026-10-08"}
    assert last_trading_day(date(2026, 10, 9), holidays=hol) == date(2026, 10, 7)


# ---------- require_fresh 集成（monkeypatch 今天为周一） ----------

def _make_parquet(d: Path, last_date: str) -> None:
    df = pd.DataFrame({
        "date": pd.to_datetime([last_date]),
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    })
    df.to_parquet(d, index=False)


def test_require_fresh_passes_on_monday(tmp_path: Path, monkeypatch):
    # 今天是周一 2026-10-12，数据为上周五 2026-10-09 → 应放行（不抛异常）
    monkeypatch.setattr("tdxfeed.freshness._today", lambda: date(2026, 10, 12))
    _make_parquet(tmp_path / "600000.parquet", "2026-10-09")
    # 不应抛出 SystemExit
    require_fresh(parquet_dir=tmp_path, sample_size=1, allowed_stale_trading_days=1)


def test_require_fresh_blocks_when_stale(tmp_path: Path, monkeypatch):
    # 今天是周一 2026-10-12，数据远早于最近交易日 → 应 sys.exit(2)
    monkeypatch.setattr("tdxfeed.freshness._today", lambda: date(2026, 10, 12))
    _make_parquet(tmp_path / "600000.parquet", "2026-09-01")
    with pytest.raises(SystemExit) as exc:
        require_fresh(parquet_dir=tmp_path, sample_size=1, allowed_stale_trading_days=1)
    assert exc.value.code == 2
