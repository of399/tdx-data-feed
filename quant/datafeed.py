"""统一数据接口层：在 tdxfeed.sources 之上提供 DataFrame 化 fetch。

设计边界（复用既有抽象，不重建）：
- 数据归一化契约来自 tdxfeed.sources.base.DataSource（bars = [date, open, high, low, close, volume(手), amount]）。
- 拉数由 tdxfeed 各源负责（SinaSource 直连 / AkshareSource 新浪主+东财回退）。
- 本模块只做"源选择 + 标准化为 DataFrame + 日期过滤"，不做网络细节。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from tdxfeed.sources.akshare_source import AkshareSource
from tdxfeed.sources.base import DataSource
from tdxfeed.sources.sina_source import SinaSource

# 阶段一固定新浪后端；'akshare' 仅作为别名（其东财回退在本环境会被阻断）。
_DEFAULT_SOURCE = "sina"


def _normalize_symbol(symbol: str) -> str:
    """6 位代码补 sh/sz/bj 前缀，已带前缀则原样返回。"""
    s = symbol.strip().lower()
    if s[:2] in ("sh", "sz", "bj"):
        return s
    head = s[0]
    if head in "69":
        return "sh" + s
    if head in "48":
        return "bj" + s
    return "sz" + s


class DataFeed:
    """统一行情接口：fetch 返回标准日线 DataFrame。

    Args:
        source: 'sina'（默认，最稳）或 'akshare'（新浪主+东财回退，东财会被阻断）。
    """

    def __init__(self, source: str = _DEFAULT_SOURCE) -> None:
        self.source_name = source
        self._src: DataSource = SinaSource() if source == "sina" else AkshareSource()

    def fetch(
        self,
        symbol: str,
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """拉取日线并转为标准 DataFrame。

        Args:
            symbol: 6 位代码或 sh/sz/bj 前缀，如 '600519' 或 'sh600519'。
            start/end: 'YYYYMMDD' 或 'YYYY-MM-DD'。
            adjust: 复权方式（'qfq'/'hfq'/''）。
        Returns:
            列 [date, open, high, low, close, volume(手), amount(元)]，按日期升序。
        """
        sym = _normalize_symbol(symbol)
        bars = self._src.fetch_bars(sym, start, end)
        df = pd.DataFrame(
            bars,
            columns=["date", "open", "high", "low", "close", "volume", "amount"],
        )
        lo = pd.Timestamp(start)
        hi = pd.Timestamp(end)
        df = df[(df["date"] >= lo) & (df["date"] <= hi)]
        return df.sort_values("date").reset_index(drop=True)
