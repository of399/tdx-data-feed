"""回测结算内核：两套口径（单笔 TP/SL、打板持有期）+ 共用组件。"""
from .bars import as_bars
from .costs import DABAN_COSTS, PLAN_E_COSTS, CostModel, Fill
from .hold import HoldPlan, settle_hold_trade
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
from .result import TradeResult
from .single import (
    ExitHit,
    SingleTradePlan,
    check_exit,
    entry_index,
    exit_levels,
    settle_single_trade,
)

__all__ = [
    "DABAN_COSTS",
    "PLAN_E_COSTS",
    "CostModel",
    "ExitHit",
    "Fill",
    "HoldPlan",
    "LimitRule",
    "SingleTradePlan",
    "TradeResult",
    "as_bars",
    "check_exit",
    "entry_index",
    "exit_levels",
    "is_limit_up_ret",
    "is_sealed",
    "limit_down_price",
    "limit_ratio",
    "limit_threshold",
    "limit_up_price",
    "market_of",
    "open_is_limit_up",
    "settle_hold_trade",
    "settle_single_trade",
]
