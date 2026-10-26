"""横盘突破 T+1 评估脚本的纯函数测试。

钉住四件事：
1. 用户原稿的 ``C > HHV(H,N)``（窗口含当日）**永远不成立**，修正式才可能命中；
2. 停牌造成的"下一根 K 线在很久以后"必须被剔除，不能当成 T+1；
3. 一字/长期停牌时箱体宽度为 0，不能产生 inf 或伪信号；
4. 胜率/期望/盈亏比的算式。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hengpan_t1_eval", ROOT / "scripts" / "hengpan_t1_eval.py")
hp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hp)


def box_frame(n_box: int = 24, hp_days: int = 20, breakout: bool = True,
              flat: bool = False) -> pd.DataFrame:
    """横盘 n_box 根（高点 10.0 / 低点 9.9），最后一根可选突破。"""
    dates = [f"2026-01-{i:02d}" if i <= 31 else f"2026-02-{i-31:02d}" for i in range(1, n_box + 2)]
    rows = []
    for i, d in enumerate(dates):
        last = i == len(dates) - 1
        if last and breakout:
            o, h, l, c, v = 10.0, 10.30, 9.95, 10.20, 2_000.0
        elif flat:
            o = h = l = c = 10.0
            v = 100.0
        else:
            o, h, l, c, v = 9.95, 10.00, 9.90, 9.95, 1_000.0
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 突破判定


def test_corrected_formula_fires_once_on_breakout():
    df = box_frame()
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5, use_ref=True)
    assert int(flags.sum()) == 1, "30 根里只允许最后一根突破成立"
    assert bool(flags.iloc[-1]), "命中必须落在突破当根"


def test_user_original_condition_can_never_fire():
    """C<=H<=HHV(H,N) 恒成立，含当日的窗口下突破条件永远为假——原稿的致命伤。"""
    df = box_frame()
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5, use_ref=False)
    assert int(flags.sum()) == 0


def test_narrow_box_wider_than_band_does_not_fire():
    df = box_frame()
    df.loc[df["date"] != df["date"].iloc[-1], "low"] = 8.0  # 箱体振幅 25%
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5)
    assert int(flags.sum()) == 0


def test_flat_box_no_volume_spike_does_not_fire():
    df = box_frame(breakout=False)
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5)
    assert int(flags.sum()) == 0


def test_zero_width_box_is_false_not_nan():
    """一字/停牌序列：箱体宽度 0，横盘判定必须是 False 而不是 inf/NaN 漏出。"""
    df = box_frame(flat=True)
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5)
    assert not flags.isna().any(), "不许把 NaN 漏给调用方"
    assert int(flags.sum()) == 0


def test_needs_quiet_volume_before_breakout():
    """量能不足 1.5 倍 → 不算突破。"""
    df = box_frame()
    df.loc[df.index[-1], "volume"] = 1_100.0
    flags = hp.breakout_flags(df, hp_days=20, band_pct=2.0, vol_ratio=1.5)
    assert int(flags.sum()) == 0


# ---------------------------------------------------------------- T+1 价格拼接


def test_attach_forward_uses_next_calendar_day():
    calendar = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    df = pd.DataFrame({
        "date": calendar,
        "open": [10.0, 10.2, 10.4, 10.6, 10.8],
        "high": [10.0] * 5,
        "low": [10.0] * 5,
        "close": [10.0, 10.5, 11.0, 11.5, 12.0],
        "volume": [100.0] * 5,
    })
    out = hp.attach_forward(df, calendar)
    row = out[out["date"] == "2026-01-02"].iloc[0]
    assert row["next_date"] == "2026-01-03"
    assert row["next_open"] == 10.4 and row["next_close"] == 11.0
    assert row["next2_open"] == 10.6
    assert bool(row["forward_ok"])


def test_attach_forward_rejects_halt_gap():
    """停牌：T 之后的下一根本地 K 线隔了 20 个交易日，不能当成 T+1。"""
    calendar = ["2026-01-01", "2026-01-02", "2026-01-03"]
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02", "2026-03-20"],
        "open": [10.0, 10.0, 10.0],
        "high": [10.0, 10.0, 10.0],
        "low": [10.0, 10.0, 10.0],
        "close": [10.0, 10.5, 13.0],
        "volume": [100, 100, 100],
    })
    out = hp.attach_forward(df, calendar)
    row = out[out["date"] == "2026-01-02"].iloc[0]
    assert not bool(row["forward_ok"]), "长停复牌的第一根不是 T+1"
    assert bool(out[out["date"] == "2026-01-02"]["next_date"].isna().iloc[0])


def test_attach_forward_last_bar_is_not_ok():
    calendar = ["2026-01-01", "2026-01-02"]
    df = pd.DataFrame({"date": calendar, "open": [10.0, 10.0], "high": [10.0, 10.0],
                       "low": [10.0, 10.0], "close": [10.0, 10.0], "volume": [100, 100]})
    out = hp.attach_forward(df, calendar)
    assert not bool(out.iloc[-1]["forward_ok"])


# ---------------------------------------------------------------- 统计


def test_summary_known_numbers():
    s = hp.summary(np.array([0.02, -0.01, 0.03]))
    assert s["n"] == 3
    assert math.isclose(s["win_rate"], 2 / 3)
    assert math.isclose(s["mean"], 0.04 / 3)
    assert math.isclose(s["median"], 0.02)
    assert math.isclose(s["profit_factor"], 0.05 / 0.01)


def test_summary_t_statistic_on_known_sample():
    rets = np.array([0.01, 0.01, 0.01, 0.01])
    s = hp.summary(rets)
    assert math.isclose(s["mean"], 0.01)
    assert math.isinf(s["t"]), "零方差但均值>0 → t 为无穷，表示确定性的正期望"


def test_summary_empty():
    s = hp.summary(np.array([]))
    assert s["n"] == 0
    assert s["win_rate"] is None or (isinstance(s["win_rate"], float) and math.isnan(s["win_rate"]))


# ---------------------------------------------------------------- 面板切片


def _write_daily(tmp_path: Path, stem: str, n: int, start_day: int = 1) -> None:
    pytest.importorskip("pyarrow")
    import datetime as dt

    rows = []
    d0 = dt.date(2026, 1, 1) + dt.timedelta(days=start_day - 1)
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        rows.append({"date": dt.date.fromisoformat(d), "open": 10.0, "high": 10.0,
                     "low": 10.0, "close": 10.0, "volume": 100.0, "amount": 1000.0})
    pd.DataFrame(rows).to_parquet(tmp_path / f"{stem}.parquet")


def test_build_panel_slices_before_window(tmp_path: Path):
    """本地日线是全历史（单只 6000 行），build_panel 必须按 date_from 先切片。"""
    d = tmp_path / "daily"
    d.mkdir()
    _write_daily(d, "sz000001", 80)
    _write_daily(d, "sh600000", 80, start_day=5)
    panel, cal = hp.build_panel(d, {"000001": "测试A", "600000": "测试B"},
                               [(20, 8.0, 1.5)], date_from="2026-02-20")
    assert panel["date"].min() >= "2026-02-20"
    assert len(cal) == len(sorted(set(panel["date"])))
    for col in ("forward_ok", "sealed_today", "limit_price", "sig_20_8_1.5",
                hp.ORIGINAL_COL):
        assert col in panel.columns


def test_build_panel_keeps_box_history_before_window(tmp_path: Path):
    """起点前一天必须有数据，否则起点当天算不出 20 日箱体（信号会被系统性低估）。"""
    d = tmp_path / "daily"
    d.mkdir()
    _write_daily(d, "sz000001", 60)  # 2026-01-01 ~ 2026-03-01
    panel, _ = hp.build_panel(d, {"000001": "测试A"}, [(20, 8.0, 1.5)],
                              date_from="2026-01-15")
    assert panel["date"].min() == "2026-01-15"
    assert len(panel) >= 20, "缓冲区里的 K 线要保留，用于滚动窗口"
