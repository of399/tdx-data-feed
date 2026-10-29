"""get_price_at 必须同时吃 date32[day]（在役口径）与 int YYYYMMDD（旧表口径）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

from tdxfeed.parquet_io import write_table

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SITES = [
    ("v5/scripts/v4_aggregate_report.py", "v4_aggregate_report"),
    ("v5/scripts/v6_data_prep.py", "v6_data_prep"),
]


def _load(rel: str, name: str):
    for extra in (SCRIPTS, ROOT / "v5" / "scripts"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [20200102, 20200103, 20200106],
            "open": [9.5, 10.5, 11.5],
            "high": [10.5, 11.5, 12.5],
            "low": [9.0, 10.0, 11.0],
            "close": [10.0, 11.0, 12.0],
            "volume": [100.0, 110.0, 120.0],
            "amount": [1000.0, 1210.0, 1440.0],
        }
    )


def _write_daily(path: Path, kind: str) -> Path:
    df = _daily_frame()
    if kind == "date32":
        return write_table(path, df)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


@pytest.mark.parametrize("rel,name", SITES)
@pytest.mark.parametrize("kind", ["date32", "int"])
@pytest.mark.parametrize(
    "cutoff,expected",
    [
        (20200101, 0.0),   # 早于首日
        (20200102, 10.0),  # 命中首日
        (20200104, 11.0),  # 周末，取之前最近一根
        (20200106, 12.0),  # 命中末日
        (20200131, 12.0),  # 晚于末日
    ],
)
def test_get_price_at_accepts_date32_and_int(
    tmp_path, monkeypatch, rel, name, kind, cutoff, expected
):
    p = _write_daily(tmp_path / "sh600000.parquet", kind)
    mod = _load(rel, f"_gpa_{name}_{kind}")
    monkeypatch.setattr(mod, "daily_path", lambda code: p)
    assert mod.get_price_at("sh600000", cutoff) == expected


@pytest.mark.parametrize("rel,name", SITES)
def test_get_price_at_missing_file_returns_zero(tmp_path, monkeypatch, rel, name):
    mod = _load(rel, f"_gpa_missing_{name}")
    monkeypatch.setattr(mod, "daily_path", lambda code: tmp_path / "nope.parquet")
    assert mod.get_price_at("sh600000", 20200102) == 0.0
