"""hfq 翻倍宇宙筛选：screen_series 纯函数口径 + suspect 上跳窗口剔除。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "find_double_up_hfq_under_test", ROOT / "scripts" / "find_double_up_hfq.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


fh = _load()


def _frame(closes_raw, factor=None, start="2020-01-02", amount=1.0e7):
    raw = np.asarray(closes_raw, dtype=float)
    fac = np.ones(len(raw)) if factor is None else np.asarray(factor, dtype=float)
    dates = pd.date_range(start, periods=len(raw), freq="D")
    return dates, raw * fac, raw, np.full(len(raw), amount)


def _screen(dates, hfq, raw, amt, **kw):
    kw.setdefault("limit", 0.095)
    return fh.screen_series("sh600000", dates, hfq, raw, amt, name="测试", **kw)


def test_defaults_match_v2_caliber():
    assert fh.WINDOW == 20
    assert fh.RATIO == 2.0
    assert pd.Timestamp(fh.START_DATE) == pd.Timestamp("2020-01-01")


def test_screen_series_finds_single_doubling_window():
    raw = np.full(60, 30.0)
    raw[20] = 10.0
    raw[21:25] = 12.0
    raw[25] = 21.0
    raw[26:] = 12.0
    hits, dropped = _screen(*_frame(raw))
    assert dropped == []
    assert len(hits) == 1
    h = hits[0]
    assert set(h) == set(fh.HIT_COLS)
    assert h["code"] == "sh600000" and h["name"] == "测试"
    assert h["start_date"] == "2020-01-22"
    assert h["peak_date"] == "2020-01-27"
    assert h["days_to_peak"] == 5
    assert h["pct_change"] == 110.0
    assert h["year"] == 2020
    assert h["is_new"] is True


def test_window_crossing_suspect_up_jump_is_excluded():
    raw = np.full(60, 30.0)
    raw[20] = 10.0
    raw[21:25] = 12.0
    raw[25] = 21.0
    raw[26:] = 12.0
    hits, dropped = _screen(*_frame(raw), suspect_up_dates=[pd.Timestamp("2020-01-24")])
    assert hits == []
    assert len(dropped) == 1
    d = dropped[0]
    assert d["start_date"] == "2020-01-22"
    assert d["suspect_date"] == "2020-01-24"
    assert d["n_suspect"] == 1
    assert d["exclude_reason"] == "suspect_up_jump"


def test_suspect_on_base_bar_does_not_exclude():
    raw = np.full(60, 30.0)
    raw[20] = 10.0
    raw[21:25] = 12.0
    raw[25] = 21.0
    raw[26:] = 12.0
    hits, dropped = _screen(*_frame(raw), suspect_up_dates=[pd.Timestamp("2020-01-22")])
    assert len(hits) == 1 and dropped == []


def test_suspect_up_dates_only_keeps_economic_up_jumps():
    jumps = pd.DataFrame({
        "date": pd.to_datetime(["2022-03-30", "2020-12-31", "2021-06-01"]),
        "econ": [0.5, -0.2, 0.1],
        "suspect": [True, True, False],
    })
    got = pd.DatetimeIndex(fh._suspect_up_dates(jumps))
    assert list(got) == [pd.Timestamp("2022-03-30")]


def test_short_series_is_skipped():
    raw = np.full(24, 10.0)
    assert _screen(*_frame(raw)) == ([], [])


def test_events_before_2020_are_ignored():
    raw = np.full(40, 10.0)
    raw[20:] = 30.0
    hits, dropped = _screen(*_frame(raw, start="2019-11-01"))
    assert hits == [] and dropped == []


def test_window_starting_before_2020_is_ignored():
    """起点在 2019 的窗口不算：2020+ 切片后首根已在 30 元，没有 2× 起点。"""
    raw = np.concatenate([np.full(31, 10.0), np.full(20, 30.0)])
    hits, dropped = _screen(*_frame(raw, start="2019-12-01"))
    assert hits == [] and dropped == []


def test_limit_up_count_uses_raw_returns_not_hfq():
    n = 45
    raw = np.full(n, 10.0)
    raw[21] = 11.0
    raw[22:] = 11.0 * 1.075 ** np.arange(1, n - 21)
    factor = np.ones(n)
    factor[21:] = 0.95          # 当日除权：hfq 只涨 4.5%，raw 涨 10%
    hits, _ = _screen(*_frame(raw, factor))
    h = next(x for x in hits if x["start_date"] == "2020-01-22")
    assert h["limit_up_count"] == 1
