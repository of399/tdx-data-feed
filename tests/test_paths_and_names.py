"""路径只定位不 fallback；ST 判定只有一个来源。"""
from __future__ import annotations

import json

import pytest

from tdxfeed import names, paths


def test_daily_path_uses_exchange_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    p = paths.daily_path("600519")
    assert p.name == "sh600519.parquet"
    assert p.relative_to(tmp_path).as_posix() == "parquet/daily/sh600519.parquet"


def test_bj_code_gets_bj_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.daily_path("920001").name == "bj920001.parquet"
    assert paths.daily_path("bj920001").name == "bj920001.parquet"


@pytest.mark.parametrize(
    ("period", "expected_dir"),
    [
        ("1", "min1"),
        ("m1", "min1"),
        ("min1", "min1"),
        ("1min", "min1"),
        ("5min", "min5"),
        ("MIN5", "min5"),
        ("60", "min60"),
        ("min60", "min60"),
    ],
)
def test_minute_path_normalizes_period_to_single_min_prefix(
    tmp_path, monkeypatch, period, expected_dir
):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    p = paths.minute_path("600519", period)
    assert p.parent.name == expected_dir
    assert p.name == "sh600519.parquet"


def test_adj_and_derived_naming_differ_by_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.adj_factor_path("600519").name == "600519.parquet"
    assert paths.hfq_factor_path("600519").name == "600519.parquet"
    assert paths.derived_path("sh600519").name == "sh600519_indicators.parquet"


def test_missing_file_raises_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        paths.read_path(paths.daily_path("600519"))


def test_is_st_name_covers_star_st_and_spaces():
    assert names.is_st_name("*ST美丽") is True
    assert names.is_st_name("ST 康佳") is True
    assert names.is_st_name("平安银行") is False
    assert names.is_st_name("") is False


def test_load_universe_reads_code_to_name(tmp_path, monkeypatch):
    fp = tmp_path / "all_a_stocks.json"
    fp.write_text(json.dumps({"000001": "平安银行", "000010": "*ST美丽"}), encoding="utf-8")
    monkeypatch.setattr(paths, "universe_path", lambda: fp)
    monkeypatch.setattr(names, "_CACHE", None)
    uni = names.load_universe()
    assert uni["000001"] == "平安银行"
    assert names.name_of("sz000010") == "*ST美丽"
    assert names.name_of("999999") == ""
