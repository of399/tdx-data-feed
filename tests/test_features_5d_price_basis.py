"""合成除权消融：13 个 hfq 维与无除权序列逐值相同，8 个 raw 维随 raw 变。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from tdxfeed.factors_polars import compute_window_features_extended_polars
from tdxfeed.features_5d import compute_window_features_extended

HFQ_DIMS = [
    "ret_5d", "ret_3d", "max_5d_pct", "pct_t1", "volatility_5d", "close_pos_5d",
    "max_drawdown_5d", "acceleration", "range_efficiency", "vol_norm_ret",
    "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low",
]
RAW_DIMS = ["limit", "prev_close", "limit_up_5d", "limit_up_t1", "is_limit_up_today",
            "consecutive_boards", "board_position", "amt_per_price"]
AMOUNT_DIMS = ["avg_amount_5d", "vol_ratio_t1", "turnover_proxy", "amount_surge_3d"]
ENUM_DIMS = ["is_st", "is_new", "is_bj", "is_gem"]


def _frames(closes, factor):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    raw = pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": closes * 0.995,
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


# 第 5 行是 10 转 3 的除权日：raw −30%，后复权因子 1 → 10/7
CLOSES = [10.0] * 5 + [7.0, 7.1, 7.8, 8.0, 8.2]
FACTOR = [1.0] * 5 + [10 / 7] * 5


def test_hfq_dims_equal_the_no_jump_series():
    raw, hfq = _frames(CLOSES, FACTOR)
    got = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    ref = compute_window_features_extended(hfq, "600000", sig_idx=8)
    for k in HFQ_DIMS:
        assert got[k] == pytest.approx(ref[k], rel=1e-12, abs=1e-12), k


def test_raw_and_amount_dims_follow_raw():
    raw, hfq = _frames(CLOSES, FACTOR)
    got = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    ref = compute_window_features_extended(raw, "600000", sig_idx=8)
    for k in RAW_DIMS + AMOUNT_DIMS + ENUM_DIMS:
        assert got[k] == pytest.approx(ref[k], rel=1e-12, abs=1e-12), k


def test_limit_branch_uses_raw_when_factor_moves_on_a_limit_day():
    # 第 5 行 raw +10%（封板），同日后复权因子 1 → 0.7：hfq 口径下这行是 −23%
    closes = [10.0] * 5 + [11.0, 11.0, 11.0, 11.0, 11.0]
    factor = [1.0] * 5 + [0.7] * 5
    raw, hfq = _frames(closes, factor)
    got = compute_window_features_extended(raw, "600000", sig_idx=5, frame_hfq=hfq)
    assert got["is_limit_up_today"] == 1                  # 涨停必须按 raw 判
    assert got["consecutive_boards"] == 1
    # 价格维按 hfq：窗口 1..5 的 pct 前四项为 0，最后一项 7.7/10−1 = −23%，
    # 落在 max_drawdown_5d 上（max_5d_pct 取最大，仍是首元素 0）。
    assert got["max_drawdown_5d"] == pytest.approx(0.7 * 11.0 / 10.0 - 1.0, rel=1e-9)
    assert got["max_5d_pct"] == pytest.approx(0.0, abs=1e-12)


def test_passing_raw_as_hfq_frame_changes_nothing():
    raw, _ = _frames(CLOSES, FACTOR)
    a = compute_window_features_extended(raw, "600000", sig_idx=8)
    b = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=raw.copy())
    assert a == pytest.approx(b, rel=1e-12, abs=1e-12)


def test_misaligned_frames_raise():
    raw, hfq = _frames(CLOSES, FACTOR)
    with pytest.raises(ValueError):
        compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq.iloc[1:])


def test_polars_matches_pandas_on_both_bases():
    raw, hfq = _frames(CLOSES, FACTOR)
    p = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    q = compute_window_features_extended_polars(
        pl.from_pandas(raw), "600000", sig_idx=8, frame_hfq=pl.from_pandas(hfq))
    for k in HFQ_DIMS + RAW_DIMS + AMOUNT_DIMS + ENUM_DIMS:
        assert q[k] == pytest.approx(p[k], rel=1e-9, abs=1e-12), k
