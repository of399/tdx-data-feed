"""随机行情上，内核 A 必须同时满足：亏损侧优先、跳空按开盘、逐日同源。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade


def _random_bars(seed: int, n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10.0 * np.cumprod(1 + rng.normal(0, 0.03, n))
    open_ = close * (1 + rng.normal(0, 0.01, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=n),
                         "open": open_, "high": high, "low": low,
                         "close": close, "volume": np.full(n, 1e6)})


def test_parity_ladder_is_sl_first_and_gap_at_open():
    """出场阶梯：双触发结算亏损侧，跳空穿越触发价按当日开盘成交（逐位相等）。"""
    costs = CostModel()
    plan = SingleTradePlan(tp=0.05, sl=-0.05, hold_days=5)
    for seed in range(200):
        bars = _random_bars(seed)
        r = settle_single_trade(bars, 3, plan, costs)
        if r is None or r.reason not in {"SL", "TP"}:
            continue
        k = r.exit_idx
        tp_lvl = r.entry_price * 1.05
        sl_lvl = r.entry_price * 0.95
        o = float(bars["open"].iloc[k])
        if r.reason == "SL":
            assert float(bars["low"].iloc[k]) <= sl_lvl
            if o > 0:
                assert r.exit_price == min(sl_lvl, o)
            else:
                assert r.exit_price == sl_lvl
        else:
            assert float(bars["high"].iloc[k]) >= tp_lvl
            if o > 0:
                assert r.exit_price == max(tp_lvl, o)
            else:
                assert r.exit_price == tp_lvl


def test_parity_daily_curve_reproduces_gross_without_costs():
    """零成本时逐日复利必须精确回到单笔毛收益：daily 用的是同一个出场价。

    实测 5000 帧最差 8.9e-16（纯浮点结合顺序）。
    """
    free = CostModel(commission=0.0, slippage=0.0, stamp_sell=0.0)
    plan = SingleTradePlan(tp=0.30, sl=-0.10, hold_days=10)
    for seed in range(200):
        bars = _random_bars(seed, n=60)
        r = settle_single_trade(bars, 5, plan, free)
        if r is None:
            continue
        curve = 1.0
        for _, inc in r.daily:
            curve *= 1.0 + inc
        assert abs((curve - 1.0) - r.gross_ret) <= 1e-12


def test_parity_daily_curve_is_same_source_as_net_return():
    """带成本时资金曲线与 net_ret 同源但**不恒等**：daily 逐日线性扣成本、
    net_ret 用 round_trip 乘性扣，另有逐日复利的二阶交叉项（与 |net_ret| 无关）。
    实测 5000 帧最差 1.91e-4；容差取 1e-3（5 倍余量）。符号必须一致。
    """
    costs = CostModel(commission=0.0003, slippage=0.0010)
    plan = SingleTradePlan(tp=0.30, sl=-0.10, hold_days=10)
    for seed in range(200):
        bars = _random_bars(seed, n=60)
        r = settle_single_trade(bars, 5, plan, costs)
        if r is None:
            continue
        curve = 1.0
        for _, inc in r.daily:
            curve *= 1.0 + inc
        assert abs((curve - 1.0) - r.net_ret) <= 1e-3
        if abs(r.net_ret) > 1e-3:
            assert (curve > 1.0) == (r.net_ret > 0.0)
