"""方案 E 回测的出场口径：同一根 K 线双触发取保守一侧，跳空按开盘成交。

`backtest_one` 原来逐日只看 `high >= TP` 再看 `low <= SL`，命中后就按**触发价位本身**
计价，于是同一条 bar 里"先摸到止盈再砸穿止损"被记成止盈离场，跳空低开穿过止损也被
记成理想价位成交。两者都只会把胜率和均收益往好的方向偏，而这份报告是实盘清单的依据。

出场价还必须回灌到逐日收益序列 `daily`：资金曲线原来一路取到出场日的 **收盘价**，
可 `return_pct` 用的是盘中触发价，两个数对不上——TP 当日冲高回落时曲线只认到 +10%，
单笔却记 +30%。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_backtest():
    spec = importlib.util.spec_from_file_location(
        "plan_e_backtest_under_test", ROOT / "scripts" / "plan_e_backtest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pe = _load_backtest()

CODE = "600000"
SIGNAL_DATE = "2026-01-01"


def _seed(bars: list[tuple[float, float, float, float]]) -> None:
    """bars = 每日 (open, high, low, close)，idx0 即 T 日；volume 固定不影响过滤。"""
    n = len(bars)
    df = pd.DataFrame(
        {
            "date": pd.date_range(SIGNAL_DATE, periods=n, freq="D"),
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [1_000_000] * n,
        }
    )
    pe._PARQUET_CACHE[CODE] = df


def _run(**kwargs) -> dict:
    params = {"hold_days": 10, "tp": 0.30, "sl": -0.10, "fee": 0.0, "slippage": 0.0}
    params.update(kwargs)
    result = pe.backtest_one(CODE, SIGNAL_DATE, **params)
    assert result is not None, "样本被过滤掉了，测试构造无效"
    return result


# T 日 +10% 满足默认过滤，T+2 开盘 10.00 = 入场价 → TP 线 13.00 / SL 线 9.00
_PROLOGUE = [(10, 10, 10, 10), (10, 11, 10, 11), (10, 10.5, 10, 10.5)]
# 既不摸 TP 也不破 SL 的填充日；出场在测试自己给的 bar 上，尾部无影响
_FILLER = (10.5, 10.6, 10.0, 10.5)


def _bars(*extra: tuple[float, float, float, float]):
    """出场判定窗口至少 3 天，因此补齐到 8 根 bar。"""
    pad = max(0, 8 - len(_PROLOGUE) - len(extra))
    _seed(_PROLOGUE + list(extra) + [_FILLER] * pad)


def test_same_bar_double_touch_resolves_to_stop_loss():
    """一根 bar 同时够到 13.5 高点和 8.5 低点：无法证明先摸哪个，按保守侧记止损。"""
    _bars((10.5, 13.5, 8.5, 12.0), (12, 12, 12, 12), (12, 12, 12, 12))
    result = _run()
    assert result["exit_reason"] == "SL"
    assert result["exit_price"] == pytest.approx(9.0, abs=0.01)
    assert result["return_pct_gross"] == pytest.approx(-0.10, abs=1e-3)


def test_gap_down_through_stop_fills_at_open():
    """跳空低开 8.00 直接跌破 9.00 止损线：成交在开盘价，不是理想止损价。"""
    _bars((10.5, 10.8, 10.2, 10.5), (8.0, 8.4, 7.6, 8.0), (8, 8, 8, 8))
    result = _run()
    assert result["exit_reason"] == "SL"
    assert result["exit_price"] == pytest.approx(8.0, abs=0.01)
    assert result["return_pct_gross"] == pytest.approx(-0.20, abs=1e-3)


def test_gap_up_through_take_profit_fills_at_open():
    """对称口径：跳空高开 13.80 越过 13.00 止盈线，成交在开盘价（不能只改亏的一侧）。"""
    _bars((10.5, 10.8, 10.2, 10.5), (13.8, 14.5, 13.5, 14.0), (14, 14, 14, 14))
    result = _run()
    assert result["exit_reason"] == "TP"
    assert result["exit_price"] == pytest.approx(13.8, abs=0.01)
    assert result["return_pct_gross"] == pytest.approx(0.38, abs=1e-3)


def test_intra_bar_stop_still_fills_at_level():
    """未跳空时口径不变：开盘 10.50 在止损线上方，按 9.00 成交。"""
    _bars((10.5, 10.6, 8.5, 9.0), (9, 9, 9, 9))
    result = _run()
    assert result["exit_reason"] == "SL"
    assert result["exit_price"] == pytest.approx(9.0, abs=0.01)


def test_daily_series_ends_at_exit_price_not_close():
    """资金曲线与单笔必须同源：出场日那一段按成交价结算。"""
    _bars((10.5, 13.5, 10.4, 11.0), (11, 11, 11, 11))
    result = _run()
    assert result["exit_reason"] == "TP"
    assert result["return_pct_gross"] == pytest.approx(0.30, abs=1e-3)
    # 零成本下逐日复利应精确还原单笔毛收益
    compound = 1.0
    for _, day_ret in result["daily"]:
        compound *= 1 + day_ret
    assert compound - 1 == pytest.approx(result["return_pct_gross"], abs=1e-9)
    assert result["daily"][-1][0] == result["exit_date"]


def test_time_exit_prices_at_close():
    """全程不触发 TP/SL：按窗口最后一日收盘价离场，逐日序列同样自洽。"""
    _bars(*[(10.5, 10.8, 10.2, 10.6) for _ in range(5)])
    result = _run()
    assert result["exit_reason"] == "TIME"
    compound = 1.0
    for _, day_ret in result["daily"]:
        compound *= 1 + day_ret
    assert compound - 1 == pytest.approx(result["return_pct_gross"], abs=1e-9)
