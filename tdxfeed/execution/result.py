"""结算结果：两个内核共用同一形状，避免各调用点再拼 dict。"""
from __future__ import annotations

from dataclasses import dataclass, field

OK = "ok"


@dataclass(frozen=True)
class TradeResult:
    entry_idx: int
    entry_date: str
    entry_price: float
    exit_idx: int
    exit_date: str
    exit_price: float
    reason: str                     # SL | TP | TIME | END | <卖出时点>
    gross_ret: float
    net_ret: float
    max_gain: float = 0.0
    max_loss: float = 0.0
    daily: tuple[tuple[str, float], ...] = ()
    status: str = OK                # ok | unbuyable_open_limit | suspended | no_exit | deferred_unsold
    deferred: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
