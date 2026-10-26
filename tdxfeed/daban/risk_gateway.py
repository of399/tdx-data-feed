"""交易前风险闸门：仓位、单笔风险、日/周亏损、连亏暂停、流动性上限。

只做决策，不下单、不连券商。所有状态可落盘 JSON，便于纸盘/实盘复用。

计划书口径：
- 单笔风险 0.5%~1% 总资金
- 单日亏损 -2%、单周 -5% 停止开新仓
- 连续 3 笔亏损暂停
- 单票/总仓位受市场环境（phase）约束
- 订单金额不超过近 5 日均成交额的 1%
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tdxfeed.daban.phase import phase_params

__all__ = [
    "Order",
    "RiskConfig",
    "RiskDecision",
    "RiskState",
    "check_order",
    "load_state",
    "parse_cap",
    "record_entry",
    "record_result",
    "roll_state",
    "save_state",
    "week_key",
]


@dataclass(slots=True)
class RiskConfig:
    capital: float = 100_000.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.20
    max_total_exposure_pct: float = 0.70
    daily_loss_limit: float = -0.02
    weekly_loss_limit: float = -0.05
    max_consecutive_losses: int = 3
    pause_days: int = 1
    max_new_per_day: int = 3
    min_price: float = 2.0
    max_price: float = 100.0
    min_avg_amount_5d: float = 5e7
    max_order_pct_adv: float = 0.01
    lot_size: int = 100
    min_commission: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Order:
    code: str
    side: str
    price: float
    shares: int
    stop_price: float = 0.0
    avg_amount_5d: float = 0.0
    is_st: bool = False
    is_new: bool = False
    is_open_limit: bool = False
    name: str = ""


@dataclass(slots=True)
class RiskState:
    equity: float
    day: str = ""
    day_start_equity: float = 0.0
    week: str = ""
    week_start_equity: float = 0.0
    consecutive_losses: int = 0
    paused_until: str = ""
    trades_today: int = 0
    exposure: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    code: str
    side: str
    requested_shares: int
    approved_shares: int
    price: float
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    reasons: list[str] = field(default_factory=list)
    caps: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def week_key(date_str: str) -> str:
    y, w, _ = date.fromisoformat(date_str).isocalendar()
    return f"{y}-{w:02d}"


def parse_cap(text: Any) -> float | None:
    """解析计划书仓位字符串："0-10%"→0.10，"≤30%"→0.30，"0%"→0.0，"—"→None。"""
    if text is None:
        return None
    s = str(text).strip().replace("≤", "").replace("%", "").replace(" ", "")
    if s in ("", "—", "-", "none", "None"):
        return None
    if "-" in s:
        s = s.split("-")[-1]
    try:
        v = float(s) / 100.0
    except (TypeError, ValueError):
        return None
    return max(0.0, min(v, 1.0))


def roll_state(state: RiskState, date_str: str) -> RiskState:
    """跨日/跨周重置基准；暂停期结束后自动恢复。"""
    if state.day != date_str:
        state.day = date_str
        state.day_start_equity = state.equity
        state.trades_today = 0
    wk = week_key(date_str)
    if state.week != wk:
        state.week = wk
        state.week_start_equity = state.equity
    if state.paused_until and date_str > state.paused_until:
        state.paused_until = ""
    if state.day_start_equity <= 0:
        state.day_start_equity = state.equity
    if state.week_start_equity <= 0:
        state.week_start_equity = state.equity
    return state


def _reject(decision: RiskDecision, reason: str) -> RiskDecision:
    decision.approved = False
    decision.approved_shares = 0
    decision.reasons.append(reason)
    return decision


def check_order(
    order: Order,
    state: RiskState,
    cfg: RiskConfig,
    *,
    phase: str = "震荡市",
    date_str: str | None = None,
) -> RiskDecision:
    """检查一笔订单；返回可成交股数与风险预算。"""
    date_str = date_str or state.day or date.today().isoformat()
    roll_state(state, date_str)
    decision = RiskDecision(
        approved=True,
        code=order.code,
        side=order.side,
        requested_shares=int(order.shares),
        approved_shares=0,
        price=float(order.price),
    )
    if order.side not in ("buy", "sell"):
        return _reject(decision, f"未知方向: {order.side}")
    if order.side == "sell":
        decision.approved_shares = int(order.shares)
        decision.reasons.append("卖出不受开仓限制")
        return decision

    if state.paused_until and date_str <= state.paused_until:
        return _reject(decision, f"连亏暂停至 {state.paused_until}")
    if state.day_start_equity > 0:
        day_ret = state.equity / state.day_start_equity - 1.0
        if day_ret <= cfg.daily_loss_limit:
            return _reject(decision, f"触及单日亏损上限 {day_ret:.2%}")
    if state.week_start_equity > 0:
        week_ret = state.equity / state.week_start_equity - 1.0
        if week_ret <= cfg.weekly_loss_limit:
            return _reject(decision, f"触及单周亏损上限 {week_ret:.2%}")
    if state.trades_today >= cfg.max_new_per_day:
        return _reject(decision, f"当日新开仓已达 {cfg.max_new_per_day} 笔")
    if order.is_st:
        return _reject(decision, "ST/退市风险")
    if order.is_new:
        return _reject(decision, "新股")
    if order.is_open_limit:
        return _reject(decision, "一字板无法成交")
    if not (cfg.min_price <= order.price <= cfg.max_price):
        return _reject(decision, f"价格 {order.price:.2f} 不在区间")
    if order.avg_amount_5d < cfg.min_avg_amount_5d:
        return _reject(decision, f"近5日均额 {order.avg_amount_5d:.0f} 低于下限")
    if order.stop_price <= 0 or order.stop_price >= order.price:
        return _reject(decision, "止损价必须低于买入价")

    lot = max(int(cfg.lot_size), 1)
    equity = max(float(state.equity), 0.0)
    price = float(order.price)
    risk_per_share = price - float(order.stop_price)
    risk_budget = equity * cfg.risk_per_trade

    def lots(value: float) -> int:
        return int(math.floor(max(value, 0.0) / price / lot) * lot)

    shares_risk = lots(risk_budget / risk_per_share * price)
    shares_position = lots(equity * cfg.max_position_pct)
    shares_exposure = lots(max(0.0, equity * cfg.max_total_exposure_pct - state.exposure))
    shares_liquidity = lots(order.avg_amount_5d * cfg.max_order_pct_adv)

    params = phase_params(phase)
    total_cap = parse_cap(params.get("total_pos"))
    single_cap = parse_cap(params.get("single_cap"))
    if total_cap is not None:
        shares_phase_total = lots(max(0.0, equity * total_cap - state.exposure))
    else:
        shares_phase_total = shares_exposure
    shares_phase_single = (lots(equity * single_cap)
                           if single_cap is not None else shares_position)

    caps = {
        "risk": max(shares_risk, 0),
        "position": max(shares_position, 0),
        "exposure": max(shares_exposure, 0),
        "liquidity": max(shares_liquidity, 0),
        "phase_total": max(shares_phase_total, 0),
        "phase_single": max(shares_phase_single, 0),
    }
    decision.caps = caps
    approved = min([int(order.shares)] + [v for v in caps.values() if v >= 0])
    approved = int(math.floor(approved / lot) * lot)
    if approved < lot:
        return _reject(decision, "按风险/仓位/流动性约束可买股数为 0")
    decision.approved_shares = approved
    decision.risk_amount = approved * risk_per_share
    decision.risk_pct = decision.risk_amount / equity if equity > 0 else float("inf")
    if decision.risk_pct > cfg.risk_per_trade + 1e-9:
        return _reject(decision, f"单笔风险 {decision.risk_pct:.2%} 超预算")
    decision.reasons.append("通过")
    return decision


def record_entry(state: RiskState, date_str: str) -> None:
    roll_state(state, date_str)
    state.trades_today += 1


def record_result(
    state: RiskState,
    pnl: float,
    date_str: str,
    cfg: RiskConfig,
) -> None:
    """更新权益与连亏计数；连亏达到上限则设置暂停期。"""
    state.equity += float(pnl)
    if pnl < 0:
        state.consecutive_losses += 1
        if state.consecutive_losses >= cfg.max_consecutive_losses:
            state.paused_until = (
                date.fromisoformat(date_str) + timedelta(days=cfg.pause_days)
            ).isoformat()
    else:
        state.consecutive_losses = 0


def save_state(state: RiskState, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def load_state(path: str | Path, *, capital: float = 100_000.0) -> RiskState:
    p = Path(path)
    if not p.exists():
        return RiskState(equity=capital, day_start_equity=capital,
                         week_start_equity=capital)
    payload = json.loads(p.read_text(encoding="utf-8"))
    allowed = {f for f in RiskState.__slots__}
    return RiskState(**{k: v for k, v in payload.items() if k in allowed})
