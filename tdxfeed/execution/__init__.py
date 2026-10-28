"""回测结算内核：两套口径（单笔 TP/SL、打板持有期）+ 共用组件。"""
from .bars import as_bars
from .costs import DABAN_COSTS, PLAN_E_COSTS, CostModel, Fill
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
from .hold import HoldPlan, settle_hold_trade
from .result import TradeResult

__all__ = [
    "as_bars",
    "CostModel",
    "Fill",
    "PLAN_E_COSTS",
    "DABAN_COSTS",
    "TradeResult",
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
    "HoldPlan",
    "settle_hold_trade",
]
