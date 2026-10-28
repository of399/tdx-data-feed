"""回测结算内核：两套口径（单笔 TP/SL、打板持有期）+ 共用组件。"""
from .limits import (
    LimitRule,
    is_limit_up_ret,
    is_sealed,
    limit_down_price,
    limit_ratio,
    limit_threshold,
    limit_up_price,
    market_of,
    open_is_limit_up,
)
from .single import (
    ExitHit,
    SingleTradePlan,
    check_exit,
    entry_index,
    exit_levels,
    settle_single_trade,
)

__all__ = [
    "LimitRule",
    "is_limit_up_ret",
    "is_sealed",
    "limit_down_price",
    "limit_ratio",
    "limit_threshold",
    "limit_up_price",
    "market_of",
    "open_is_limit_up",
    "ExitHit",
    "SingleTradePlan",
    "check_exit",
    "entry_index",
    "exit_levels",
    "settle_single_trade",
]
