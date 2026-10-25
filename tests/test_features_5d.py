"""方案 D 18 维特征单一数据源（training + predict 共用）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from tdxfeed.daban.limit_price import limit_ratio
from tdxfeed.features_5d import (
    LIMIT_MAP,
    MAIN_ST_RULE_CHANGE_DATE,
    compute_window_features,
    get_limit,
)


def _make_window(n: int = 10, start_close: float = 10.0, trend: float = 0.05) -> pd.DataFrame:
    """构造 n 行合成日 K（避免依赖真实数据）。"""
    dates = pd.date_range("2026-09-01", periods=n, freq="B")
    close = start_close * (1 + trend) ** np.arange(n)
    open_ = close * (1 + np.random.default_rng(42).uniform(-0.01, 0.01, n))
    high = np.maximum(close, open_) * (1 + np.abs(np.random.default_rng(43).uniform(0, 0.005, n)))
    low = np.minimum(close, open_) * (1 - np.abs(np.random.default_rng(44).uniform(0, 0.005, n)))
    volume = np.random.default_rng(45).uniform(1e6, 1e7, n)
    amount = close * volume
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount}
    )


def test_get_limit_basic():
    assert get_limit("600000") == LIMIT_MAP["main"]
    assert get_limit("300750") == LIMIT_MAP["gem"]
    assert get_limit("688000") == LIMIT_MAP["gem"]
    assert get_limit("bj920001") == LIMIT_MAP["bse"]
    assert get_limit("920001") == LIMIT_MAP["bse"]
    assert get_limit("sz300750") == LIMIT_MAP["gem"]
    assert get_limit("sh688000") == LIMIT_MAP["gem"]


def test_get_limit_main_st_before_rule_change_uses_five_percent():
    """主板 ST/*ST 在 2026-07-06 并轨前是 ±5%，判定阈值须随之降档。"""
    assert get_limit("600001", name="ST康达", on_date="2026-07-03") == pytest.approx(0.045)
    assert get_limit("sz000001", name="*ST新城", on_date="20200102") == pytest.approx(0.045)


def test_get_limit_main_st_on_and_after_rule_change_uses_ten_percent():
    """生效日**含当日**起与主板普通票并轨，不再单列一档。"""
    assert get_limit("600001", name="ST康达", on_date=MAIN_ST_RULE_CHANGE_DATE) == pytest.approx(0.095)
    assert get_limit("600001", name="ST康达", on_date="2026-08-01") == pytest.approx(0.095)


def test_get_limit_st_flag_does_not_change_gem_or_bse():
    """创业板/科创板 ST 维持 20%，北交所 ST 维持 30%，全程与日期无关。"""
    for on_date in ("2020-01-02", "2026-07-05", "2026-07-06", "2026-10-01"):
        assert get_limit("sz300750", name="ST宁德", on_date=on_date) == pytest.approx(0.195)
        assert get_limit("sh688000", name="*ST科创", on_date=on_date) == pytest.approx(0.195)
        assert get_limit("bj920001", name="ST北晶", on_date=on_date) == pytest.approx(0.295)


def test_get_limit_without_name_or_date_keeps_legacy_values():
    """不传 name/on_date 必须逐值等于旧行为：既有调用点与历史模型输入不受影响。"""
    for code, expected in (
        ("600000", LIMIT_MAP["main"]),
        ("sz000001", LIMIT_MAP["main"]),
        ("300750", LIMIT_MAP["gem"]),
        ("688000", LIMIT_MAP["gem"]),
        ("bj920001", LIMIT_MAP["bse"]),
    ):
        assert get_limit(code) == expected
        assert get_limit(code, on_date="2021-06-01") == expected
        assert get_limit(code, name="ST无关") == expected


def test_get_limit_threshold_equals_ratio_minus_buffer():
    """四档阈值恒 = 交易所真实比例 − 0.5% 缓冲：比例独立重述一遍，改动须两处同时。"""
    for code, name, on_date, ratio in (
        ("600000", "", "2021-06-01", 0.10),
        ("600001", "ST康达", "2026-07-03", 0.05),
        ("600001", "ST康达", MAIN_ST_RULE_CHANGE_DATE, 0.10),
        ("sz300750", "ST宁德", "2021-06-01", 0.20),
        ("bj920001", "ST北晶", "2026-07-03", 0.30),
    ):
        assert get_limit(code, name=name, on_date=on_date) == pytest.approx(ratio - 0.005)


def test_get_limit_agrees_with_daban_limit_ratio_across_the_merger():
    """判定侧阈值恒 = 挂单侧真实比例 − 0.5%：同一规则的两处实现不许各自漂。"""
    cases = [
        ("600000", "", "2021-06-01"),
        ("600001", "ST康达", "2026-07-03"),
        ("600001", "ST康达", "2026-07-05"),
        ("600001", "ST康达", "2026-07-06"),
        ("600001", "ST康达", "2026-10-01"),
        ("600001", "ST康达", None),
        ("sz000001", "*ST新城", "20200102"),
        ("sz300750", "ST宁德", "2021-06-01"),
        ("sh688000", "*ST科创", "2026-07-03"),
        ("bj920001", "ST北晶", "2026-07-03"),
    ]
    for code, name, on_date in cases:
        expected = float(limit_ratio(code, name, on_date)) - 0.005
        assert get_limit(code, name=name, on_date=on_date) == pytest.approx(expected), (
            f"{code}/{name}/{on_date}: 判定侧 {get_limit(code, name=name, on_date=on_date)} "
            f"≠ 挂单侧 {limit_ratio(code, name, on_date)} − 0.005"
        )


def test_compute_returns_18_dims():
    df = _make_window(10)
    feats = compute_window_features(df, "600000")
    assert feats is not None
    expected_keys = {
        "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
        "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
        "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
        "is_bj", "is_gem",
    }
    assert set(feats.keys()) == expected_keys
    assert len(feats) == 18


def test_compute_uses_raw_close_not_qfq():
    """核心守护：价格类特征必须基于 raw close，不应用 adj_factor。

    模拟真实 adj_factor 行为：对 open/high/low/close 统一缩放（除权日调整），
    涨幅类特征（无量纲）应当完全一致。
    """
    df = _make_window(10)
    feats_raw = compute_window_features(df, "600000")

    # 模拟 qfq：所有价格列整体 × adj_factor=0.8（真实除权场景）
    df_qfq = df.copy()
    for c in ("open", "high", "low", "close"):
        df_qfq[c] = df_qfq[c] * 0.8
    feats_qfq = compute_window_features(df_qfq, "600000")

    # 涨幅类无量纲，应当完全一致
    assert abs(feats_raw["ret_5d"] - feats_qfq["ret_5d"]) < 1e-9, (
        f"ret_5d 漂移：raw={feats_raw['ret_5d']:.6f} qfq={feats_qfq['ret_5d']:.6f}"
    )
    assert abs(feats_raw["ret_3d"] - feats_qfq["ret_3d"]) < 1e-9
    assert abs(feats_raw["pct_t1"] - feats_qfq["pct_t1"]) < 1e-9


def test_compute_uses_amount_not_volume_for_vol_ratio():
    df = _make_window(10)
    feats = compute_window_features(df, "600000")
    # vol_ratio_t1 应基于 amount，不是 volume（守护 predict 旧 bug）
    expected = df.iloc[-1]["amount"] / df.iloc[-5:]["amount"].mean()
    assert abs(feats["vol_ratio_t1"] - expected) < 1e-9


def test_compute_max_drawdown_uses_pct_min_not_running_max():
    """守护：max_drawdown_5d = pct_change.min()，非 running max - 1。"""
    df = _make_window(10)
    feats = compute_window_features(df, "600000")
    pct = df["close"].iloc[-5:].pct_change().fillna(0)
    assert abs(feats["max_drawdown_5d"] - float(pct.min())) < 1e-9


def test_compute_pct_t1_uses_open_to_close():
    """守护：pct_t1 = sig_day close/open - 1（T-1 当日），不是 close→close。"""
    df = _make_window(10)
    feats = compute_window_features(df, "600000")
    sig_day = df.iloc[-1]
    expected = sig_day["close"] / sig_day["open"] - 1
    assert abs(feats["pct_t1"] - expected) < 1e-9


def test_compute_sig_idx_offset():
    """验证 sig_idx 偏移：取非末尾位置应得到不同的特征。"""
    df = _make_window(10)
    feats_last = compute_window_features(df, "600000", sig_idx=-1)
    feats_mid = compute_window_features(df, "600000", sig_idx=6)
    # 中间行的 prev_close 应该不同
    assert feats_last["prev_close"] != feats_mid["prev_close"]


def test_compute_insufficient_data_returns_none():
    df = _make_window(3)  # < 5 行
    assert compute_window_features(df, "600000") is None


def test_column_order_matches_training():
    """守护：返回 dict 的列顺序与训练时严格一致（LightGBM 模型依赖列序）。"""
    df = _make_window(10)
    feats = compute_window_features(df, "600000")
    expected_order = [
        "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
        "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
        "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
        "is_bj", "is_gem",
    ]
    assert list(feats.keys()) == expected_order


if __name__ == "__main__":
    test_get_limit_basic()
    test_compute_returns_18_dims()
    test_compute_uses_raw_close_not_qfq()
    test_compute_uses_amount_not_volume_for_vol_ratio()
    test_compute_max_drawdown_uses_pct_min_not_running_max()
    test_compute_pct_t1_uses_open_to_close()
    test_compute_sig_idx_offset()
    test_compute_insufficient_data_returns_none()
    test_column_order_matches_training()
    print("ALL features_5d TESTS PASSED")
