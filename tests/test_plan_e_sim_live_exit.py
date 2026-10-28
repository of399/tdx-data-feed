"""`plan_e_sim_live.simulate_trade` 的出场口径必须和回测一致（2026-10-24）。

回测侧 P0-3 已经把 `plan_e_backtest.py` 改成保守成交：
  - 同一根 K 线同时够到 TP 和 SL ⇒ 日内高低点无法证明先后，取亏损一侧；
  - 跳空穿越阈值 ⇒ 按**当日开盘价**成交，不是按阈值价。
`plan_e_sim_live.py` 是这条口径的第二份副本，还留着乐观写法：先判 TP、成交恒等于阈值价。
它跑出来的是"实盘跟踪"的收益，比回测更乐观就等于给自己抬分。

三种情形各自的正确答案（entry=10.00 ⇒ TP 13.00 / SL 9.00）：
  双触发      ⇒ SL 9.00（旧代码给 TP 13.00）
  低开穿 SL    ⇒ 开盘 8.00（旧代码给 9.00，少算一个跌停的缺口）
  高开穿 TP    ⇒ 开盘 14.00（旧代码给 13.00，把能多拿的也抹平了）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load():
    spec = importlib.util.spec_from_file_location(
        "plan_e_sim_live_under_test", ROOT / "scripts" / "plan_e_sim_live.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


sim = _load()

SIGNAL_DATE = pd.Timestamp("2024-01-01")


def _bars(*rows: tuple[float, float, float, float]) -> pd.DataFrame:
    """第 0 行是信号日，第 1 行是 T+1 入场日（open 即入场价），其后是持有期。"""
    all_rows = [(10.0, 10.4, 9.8, 10.0), *rows]   # 信号日随便给一根不影响口径
    dates = pd.date_range("2024-01-01", periods=len(all_rows), freq="D")
    o, h, l, c = zip(*all_rows, strict=True)
    df = pd.DataFrame({"date": dates, "open": o, "high": h, "low": l, "close": c})
    df["volume"] = 1_000_000.0
    df["amount"] = df["close"] * df["volume"]
    return df


def _exit(row: tuple[float, float, float, float]) -> dict:
    """入场日 open=10.00，第二日就是判决日。"""
    df = _bars((10.0, 10.2, 9.9, 10.1), row)     # T+1 入场 bar + 判决 bar
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res is not None
    return res


def test_same_bar_double_touch_settles_the_loss_side():
    r = _exit((10.0, 13.5, 8.5, 12.0))
    assert r["reason"] == "SL", f"双触发判成了 TP = 自我抬分: {r}"
    assert r["exit_price"] == 9.0, r


def test_gap_down_through_stop_fills_at_the_open():
    r = _exit((8.0, 8.5, 7.5, 8.0))
    assert r["reason"] == "SL", r
    assert r["exit_price"] == 8.0, f"缺口被抹平成阈值价 9.0，少算了一截: {r}"


def test_gap_up_through_target_fills_at_the_open():
    r = _exit((14.0, 14.5, 13.5, 14.0))
    assert r["reason"] == "TP", r
    assert r["exit_price"] == 14.0, f"高开越限应按开盘成交: {r}"


def test_no_touch_runs_to_the_clock():
    r = _exit((10.0, 10.5, 9.5, 10.2))
    assert r["reason"] in ("TIME", "END"), r
    assert r["exit_price"] == 10.2, f"没触阈值就该按收盘结算: {r}"


def test_time_exit_after_nine_bars_matches_backtest_window():
    """入场后第 9 根收盘超时（与 plan_e 的 hold_days=10 含入场根一致）。"""
    rows = [(10.0, 10.1, 9.9, 10.0)] * 9           # rows[0] 是入场根（_bars 已前置信号日），rows[0..8] 是入场后 9 根平价
    df = _bars(*rows, (10.0, 10.1, 9.9, 10.0), (99.0, 99.0, 99.0, 99.0))
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res["reason"] == "TIME"
    assert res["exit_price"] == 10.0


def test_end_reason_when_window_truncated():
    rows = [(10.0, 10.1, 9.9, 10.0)] * 2
    df = _bars(*rows)
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res["reason"] == "END"


def test_full_window_exactly_at_data_end_reports_time():
    """满窗口恰好触及数据端点报 TIME（价不变）——旧实现报 END，内核按 complete 判 TIME。

    future 恰好 10 根：入场根 + 9 根判定根都齐，只是没有额外数据。END→TIME 是有意对齐
    plan_e（它同样报 TIME），价格仍是第 9 根收盘。
    """
    rows = [(10.0, 10.1, 9.9, 10.0)] * 9 + [(10.0, 10.1, 9.9, 10.0)]
    df = _bars(*rows)
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res["reason"] == "TIME"
    assert res["exit_price"] == 10.0
