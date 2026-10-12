"""数据校验：真实交易日历连续性 + 缺失率。

关键修正：A 股有法定节假日（春节/五一/十一等），朴素 pd.date_range(freq='B')
会把节假日误判为"缺失数据"。本模块使用 akshare 真实交易日历
tool_trade_date_hist_sina() 计算应在市交易日，仅统计真正缺口。
"""
from __future__ import annotations

from pathlib import Path

import akshare as ak
import pandas as pd

_CORE_COLS = ["open", "high", "low", "close", "volume"]


def trade_calendar() -> set[pd.Timestamp]:
    """返回 A 股真实交易日集合（来自 akshare 交易日历）。"""
    cal = ak.tool_trade_date_hist_sina()
    col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
    return set(pd.to_datetime(cal[col]))


def validate(df: pd.DataFrame, calendar: set[pd.Timestamp] | None = None) -> dict:
    """对标准日线 DataFrame 做完整性校验。

    Args:
        df: 含 [date, open, high, low, close, volume, amount] 的 DataFrame。
        calendar: 真实交易日集合；缺省时实时拉取。
    Returns:
        校验报告字典（行数/字段/区间/应在交易日/缺失交易日/缺失率）。
    """
    if calendar is None:
        calendar = trade_calendar()
    dts = set(pd.to_datetime(df["date"]))
    lo, hi = df["date"].min(), df["date"].max()
    expected = {d for d in calendar if lo <= d <= hi}
    missing = sorted(expected - dts)
    na_rate = float(df[_CORE_COLS].isna().mean().mean())
    return {
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "date_range": (str(lo.date()), str(hi.date())),
        "expected_trading_days": len(expected),
        "missing_trading_days": len(missing),
        "missing_examples": [str(m.date()) for m in missing[:5]],
        "overall_na_rate": round(na_rate, 4),
    }
