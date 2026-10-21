"""打板（涨停板）量化工具集。

模块划分::

    limit_price  涨停价四舍五入 + 分板块涨跌幅 + ST 规则
    realtime     新浪 L1 五档实时行情（封单/量比来源）
    scanner      全市场扫描：封板/上板中/炸板/回封识别 + 封单比 + 打分
    phase        市场环境判定（计划书 §6.1）与仓位参数（§6.2）

配套脚本（仓库根 scripts/）::

    daban_scan.py      盘中扫描 → CSV + 通达信 .blk
    daban_backtest.py  打板回测（T+1 开盘买入，NET 口径）

对齐文档：《A股打板交易策略执行计划书》v1.1.1。
"""
from __future__ import annotations

from tdxfeed.daban.limit_price import (
    board_label,
    exchange_prefix,
    is_sealed,
    is_st,
    limit_down_price,
    limit_ratio,
    limit_up_price,
    market_of,
    normalize_code,
    pct_change,
)
from tdxfeed.daban.phase import (
    PHASE_PARAMS,
    PhaseStats,
    detect_market_phase,
    phase_params,
    read_phase,
    write_phase,
)
from tdxfeed.daban.realtime import Quote, fetch_quote_map, fetch_quotes
from tdxfeed.daban.scanner import Candidate, Scanner, ScanResult, StockState

__all__ = [
    "PHASE_PARAMS",
    "Candidate",
    "PhaseStats",
    "Quote",
    "ScanResult",
    "Scanner",
    "StockState",
    "board_label",
    "detect_market_phase",
    "exchange_prefix",
    "fetch_quote_map",
    "fetch_quotes",
    "is_sealed",
    "is_st",
    "limit_down_price",
    "limit_ratio",
    "limit_up_price",
    "market_of",
    "normalize_code",
    "pct_change",
    "phase_params",
    "read_phase",
    "write_phase",
]
