"""内核 A 的三条硬口径：亏损侧优先、跳空按开盘、超时收官。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import (
    ExitHit,
    SingleTradePlan,
    check_exit,
    entry_index,
    exit_levels,
    settle_single_trade,
)

FREE = CostModel(commission=0.0, slippage=0.0)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows)),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [1_000_000] * len(rows),
    })


def test_exit_levels_from_entry_price():
    tp_lvl, sl_lvl = exit_levels(10.0, SingleTradePlan(tp=0.30, sl=-0.10))
    assert tp_lvl == pytest.approx(13.0)
    assert sl_lvl == pytest.approx(9.0)


def test_exit_levels_allow_disabled_side():
    assert exit_levels(10.0, SingleTradePlan(tp=None, sl=-0.10))[0] is None


def test_double_touch_settles_loss_side():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan())
    assert hit == ExitHit(price=9.0, reason="SL")


def test_double_touch_tp_first_keeps_profit_side():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan(sl_first=False))
    assert hit.reason == "TP"


def test_gap_down_through_stop_fills_at_open():
    hit = check_exit(10.0, open_=8.0, high=8.5, low=7.5, close=8.0, plan=SingleTradePlan())
    assert hit == ExitHit(price=8.0, reason="SL")


def test_gap_up_through_target_fills_at_open():
    hit = check_exit(10.0, open_=14.0, high=14.2, low=13.8, close=14.0, plan=SingleTradePlan())
    assert hit == ExitHit(price=14.0, reason="TP")


def test_close_only_judges_on_close():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan(judge="close_only"))
    assert hit is None
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=13.0,
                     plan=SingleTradePlan(judge="close_only"))
    assert hit == ExitHit(price=13.0, reason="TP")


def test_entry_index_respects_lag_and_bounds():
    df = _bars([(10.0, 10.0, 10.0, 10.0)] * 5)
    assert entry_index(df, 0, lag=1) == 1
    assert entry_index(df, 3, lag=1) == 4
    assert entry_index(df, 4, lag=1) is None


def test_settle_enters_next_bar_and_times_out_at_close_cost_free():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),     # 0 信号日
        (10.0, 10.0, 10.0, 10.0),     # 1 入场日 open=10
        (10.5, 10.6, 10.4, 10.5),     # 2
        (11.0, 11.1, 10.9, 11.0),     # 3 超时（hold_days=3 ⇒ 第 3 根收盘）
    ])
    r = settle_single_trade(df, 1, SingleTradePlan(tp=0.30, sl=-0.10, hold_days=3), FREE)
    assert r.reason == "TIME"
    assert r.exit_idx == 3
    assert r.exit_price == 11.0
    assert r.net_ret == pytest.approx(0.1)


def test_daily_series_has_entry_and_exit_cost_bars():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (13.0, 13.2, 12.9, 13.0),
    ])
    cm = CostModel(commission=0.0003, slippage=0.0010)
    r = settle_single_trade(df, 1, SingleTradePlan(tp=0.30, sl=-0.10, hold_days=5), cm)
    assert [d for d, _ in r.daily] == ["2026-01-02", "2026-01-03"]
    # 入场日只扣一次 side cost，出场日再扣一次
    assert r.daily[0][1] == pytest.approx(-(0.0003 + 0.0010))
    assert r.daily[1][1] == pytest.approx(13.0 / 10.0 - 1 - (0.0003 + 0.0010))


def test_insufficient_window_skips_by_default():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0)])
    assert settle_single_trade(df, 1, SingleTradePlan(), FREE) is None


def test_insufficient_window_can_close_at_last_bar():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.5, 10.6, 10.4, 10.5)])
    r = settle_single_trade(df, 1, SingleTradePlan(hold_days=10), FREE,
                            insufficient="last_close")
    assert r.reason == "END"
    assert r.exit_idx == 2


def test_buyable_callback_can_block_entry():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.5, 10.6, 10.4, 10.5)])
    assert settle_single_trade(df, 1, SingleTradePlan(), FREE,
                               buyable=lambda i: False) is None


# ---- 以下两条锁 plan_e 对齐面（Task 8 的 md5 闸门依赖它们） ----


def test_truncated_window_without_hit_settles_time_at_last_close():
    """数据到头的截断窗口：plan_e 报 TIME 而非 END，skip 不得导致整笔丢失。"""
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.2, 10.3, 10.1, 10.2), (10.3, 10.4, 10.2, 10.3)])
    r = settle_single_trade(df, 1, SingleTradePlan(hold_days=10), FREE)
    assert r is not None
    assert r.reason == "TIME"
    assert r.exit_idx == 3
    assert r.exit_price == pytest.approx(10.3)


def test_daily_feeds_exit_price_back_and_tracks_net_ret():
    """出场价回灌逐日收益；连乘与 net_ret 同源但非恒等（线性扣费 vs 乘法扣费）。"""
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.2, 9.9, 10.1),
        (10.4, 12.1, 10.3, 12.0),
        (13.5, 14.3, 13.4, 13.6),     # 跳空越过 tp=13 → 按开盘 13.5 成交，收盘 13.6
    ])
    cm = CostModel(commission=0.0003, slippage=0.0010)
    r = settle_single_trade(df, 1, SingleTradePlan(tp=0.30, sl=-0.10, hold_days=5), cm)
    assert r.reason == "TP"
    assert r.exit_price == pytest.approx(13.5)
    # 出场日按成交价回灌，不是当日收盘 13.6
    assert r.daily[-1][1] == pytest.approx(13.5 / 12.0 - 1 - (0.0003 + 0.0010))
    compounded = 1.0
    for _, ret in r.daily:
        compounded *= 1.0 + ret
    # 差 = 成本与收益的二阶项（plan_e 的 daily 逐日线性扣 side_cost，net_ret 是乘法扣费）
    assert abs(compounded - 1.0 - r.net_ret) < 1e-3
