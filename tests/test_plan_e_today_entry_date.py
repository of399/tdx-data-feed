"""`--today` 清单的语义：今日开盘入场，不是"过去 7 天里已经错过的信号"。

原实现 `signal_date >= today - 7d` 只有下界、没有上界，也不看入场日，于是清单里可以是
任何入场日早已过去（甚至晚于今天）的行。用真实数据实测：传 today=2025-12-31 时 9 条"今日
信号"里 entry_date == 今天的有 **0 条**，最离谱的一条 signal_date 是 2026-07-30（在未来）。

而真正该买的那条——T+1 是行情里最后一根 bar、今天开盘入场——反而必然被丢掉：
`backtest_one` 要求 T+2 之后有 3 根 bar，盘前跑的时候 T+2 根本还不存在。
所以严格口径必须绕开前向窗口，只按"T+1 过滤成立 + 入场日是今天"出票。
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


def _load_backtest():
    spec = importlib.util.spec_from_file_location(
        "plan_e_backtest_today_under_test", ROOT / "scripts" / "plan_e_backtest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pe = _load_backtest()

BARS = pd.bdate_range("2026-10-12", periods=20)  # 最后一根 = 2026-11-06
# 交易日历取自指数日 K；这里用延伸到 11-10 的工作日序列冒充"日历已覆盖今天"
CALENDAR = pd.bdate_range("2026-10-12", periods=22)  # 至 2026-11-10


def _seed_calendar() -> None:
    n = len(CALENDAR)
    pe._PARQUET_CACHE[getattr(pe, "CALENDAR_CODE", "sh000001")] = pd.DataFrame(
        {
            "date": CALENDAR,
            "open": [3000.0] * n,
            "high": [3000.0] * n,
            "low": [3000.0] * n,
            "close": [3000.0] * n,
            "volume": [1_000_000] * n,
        }
    )


def _seed(code: str, t1_idx: int, t1_gain: float = 0.10) -> None:
    """翻倍触发日 T = t1_idx - 1；T+1 那根 bar 放量 +t1_gain。"""
    close = [10.0] * len(BARS)
    close[t1_idx] = round(10.0 * (1 + t1_gain), 4)
    volume = [1_000_000] * len(BARS)
    volume[t1_idx] = 2_000_000
    pe._PARQUET_CACHE[code] = pd.DataFrame(
        {
            "date": BARS,
            "open": [10.0] * len(BARS),
            "high": [10.2] * len(BARS),
            "low": [9.8] * len(BARS),
            "close": close,
            "volume": volume,
        }
    )


def _double_up(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame({"code": [r[0] for r in rows],
                         "signal_date": [r[1] for r in rows]})


def _signals(today: str, rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pe.generate_today_signals(_double_up(rows), today)


def test_actionable_when_t1_is_the_newest_bar():
    """盘前运行：T+1 就是最后一根 bar，今天开盘才买得到 → 必须出现在清单里。"""
    _seed_calendar()
    _seed("600001", t1_idx=19)  # T = 11-05，T+1 = 11-06（最后一根），入场 = 11-09
    sig = _signals("2026-11-09", [("600001", "2026-11-05")])
    assert len(sig) == 1, sig
    assert sig.iloc[0]["entry_date"] == "2026-11-09"


def test_stale_entry_date_is_not_today_signal():
    """入场日已在几天前的，不是"今日开盘入场"清单，哪怕信号落在 7 日窗口内。"""
    _seed("600002", t1_idx=2)  # T = 10-13，T+1 = 10-14，入场 = 10-15
    sig = _signals("2026-10-20", [("600002", "2026-10-13")])
    assert sig.empty, sig[["code", "signal_date", "entry_date"]].to_string(index=False)


def test_t1_gain_below_threshold_is_dropped():
    """T+1 只涨 3%，达不到 9.5% 的跟随门槛。"""
    _seed("600003", t1_idx=19, t1_gain=0.03)
    sig = _signals("2026-11-09", [("600003", "2026-11-05")])
    assert sig.empty, sig[["code", "signal_date", "t1_pct"]].to_string(index=False)


def test_t1_volume_ratio_below_threshold_is_dropped():
    """T+1 涨幅够但没放量（量比 1.0 < 1.5）不跟。"""
    code = "600004"
    _seed(code, t1_idx=19)
    volume = [1_000_000] * len(BARS)
    volume[19] = 1_000_000
    pe._PARQUET_CACHE[code]["volume"] = volume
    sig = _signals("2026-11-09", [(code, "2026-11-05")])
    assert sig.empty, sig[["code", "signal_date", "t1_vol_ratio"]].to_string(index=False)


def test_listed_rows_keep_the_columns_journal_prints():
    """plan_e_journal.mode_daily 直接按这四列打印，字段不能少。"""
    _seed_calendar()
    _seed("600005", t1_idx=19)
    sig = _signals("2026-11-09", [("600005", "2026-11-05")])
    for col in ("code", "signal_date", "t1_pct", "t1_vol_ratio", "entry_date"):
        assert col in sig.columns, col


def test_not_relisted_after_the_entry_session_is_over():
    """隔天早上再跑不能继续冒充今日信号：11-09 已过，11-10 的开盘不是这个入场点。"""
    _seed_calendar()
    _seed("600006", t1_idx=19)  # 数据仍停在 11-06，T+2 缺失
    sig = _signals("2026-11-10", [("600006", "2026-11-05")])
    assert sig.empty, sig[["code", "signal_date", "entry_date"]].to_string(index=False)


def test_not_listed_when_the_market_is_closed_today():
    """周末没有开盘可等：11-07/11-08 非交易日，清单应为空。"""
    _seed_calendar()
    _seed("600007", t1_idx=19)
    for weekend in ("2026-11-07", "2026-11-08"):
        sig = _signals(weekend, [("600007", "2026-11-05")])
        assert sig.empty, f"{weekend}\n{sig.to_string(index=False)}"
