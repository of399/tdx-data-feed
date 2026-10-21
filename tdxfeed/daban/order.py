"""下单接口适配层（计划书 §C.4）。

本模块只负责把**信号转成订单意图**，真正的成交由券商通道执行。

三种落地方式：

- ``DryRunBroker``（默认，零依赖）：把订单意图落盘为信号单 CSV/JSON，
  人工复核后手工在通达信/券商端下单。**推荐先跑这个。**
- ``QmtBroker``：miniQMT / QMT（``xtquant``），需 Windows + 券商客户端；
  未安装 xtquant 时构造即报错并给出安装提示。
- ``condition_order_text``：生成通达信条件单/预警表达式，粘进券商条件单。

⚠ 风控：打板单笔亏损可达 15%~20%（§0.2），本模块不做任何自动风控，
所有参数（仓位上限、止损）必须由调用方按 §5 显式传入。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

__all__ = [
    "BrokerAdapter",
    "DryRunBroker",
    "OrderIntent",
    "QmtBroker",
    "build_intents",
    "condition_order_text",
]


@dataclass(slots=True)
class OrderIntent:
    """一条下单意图（尚未提交）。"""

    code: str
    name: str
    side: str = "buy"  # buy / sell
    price: float = 0.0  # 委托价（打板 = 涨停价）
    shares: int = 0
    amount: float = 0.0  # 委托金额（元）
    stop_loss: float | None = None
    take_profit: float | None = None
    score: float = 0.0
    sector: str = ""
    reason: str = ""
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict:
        return asdict(self)


class BrokerAdapter(Protocol):
    """下单通道协议。"""

    def place(self, intent: OrderIntent) -> dict:  # pragma: no cover - 协议
        ...


class DryRunBroker:
    """落盘模式：不真实下单，只产出信号单（默认通道）。"""

    def __init__(self, out_dir: Path | str = "data/daban/orders") -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sent: list[OrderIntent] = []

    def place(self, intent: OrderIntent) -> dict:
        self.sent.append(intent)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        csv_path = self.out_dir / f"orders_{stamp}.csv"
        json_path = self.out_dir / f"orders_{stamp}.json"
        rows = [asdict(i) for i in self.sent]
        _write_csv(csv_path, rows)
        json_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "status": "dry_run",
            "code": intent.code,
            "price": intent.price,
            "shares": intent.shares,
            "csv": str(csv_path),
            "json": str(json_path),
        }


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class QmtBroker:
    """miniQMT / QMT（xtquant）下单通道。

    需在 Windows 券商环境运行；本机为 Linux 时构造会直接报错。
    """

    def __init__(
        self,
        user_data_path: str,
        session_id: int = 123456789,
        account_id: str = "",
        account_type: str = "STOCK",
    ) -> None:
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as e:
            raise RuntimeError(
                "未安装 xtquant。安装：pip install xtquant；"
                "且 xtquant 仅支持 Windows + 券商 miniQMT 客户端环境。"
                "当前可用 --broker dryrun 走落盘信号单。"
            ) from e

        self._xtconstant = xtconstant
        self.account = StockAccount(account_id, account_type)
        self.trader = XtQuantTrader(user_data_path, session_id)
        self.trader.start()
        self.trader.connect()
        if self.trader.connect_result != 0:
            raise RuntimeError(f"QMT 连接失败，错误码 {self.trader.connect_result}")

    def place(self, intent: OrderIntent) -> dict:
        xtconstant = self._xtconstant
        if intent.side != "buy":
            raise NotImplementedError("当前仅实现买入（下单打板）")
        order_id = self.trader.order_stock(
            self.account,
            intent.code,
            xtconstant.STOCK_BUY,
            intent.shares,
            intent.price,
            0,
            "daban",
            f"daban_{intent.code}",
        )
        return {"status": "submitted", "order_id": order_id, "code": intent.code}


def build_intents(
    candidates,
    cash: float,
    max_single_pct: float = 0.15,
    max_total_pct: float = 0.50,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.10,
    lot: int = 100,
    min_amount: float = 0.0,
) -> list[OrderIntent]:
    """按仓位规则把候选转成订单意图（§5.1/§5.2/§6.2）。

    参数:
        cash:           可用资金（元）
        max_single_pct: 单票上限（占总资金）
        max_total_pct:  总仓上限（占总资金）
        stop_loss_pct:  止损线（相对买入价）
        take_profit_pct:止盈线（相对买入价）
        lot:            最小交易单位（股）
        min_amount:     单笔最小金额（元），低于则跳过（避免尾差碎单）
    """
    single_cap = cash * max_single_pct
    total_cap = cash * max_total_pct
    intents: list[OrderIntent] = []
    used = 0.0
    for c in candidates:
        if used >= total_cap:
            break
        price = float(getattr(c, "limit_price", 0) or 0)
        if price <= 0:
            continue
        budget = min(single_cap, total_cap - used)
        shares = int(budget // price // lot) * lot
        if shares <= 0:
            continue
        amount = shares * price
        if min_amount and amount < min_amount:
            continue
        used += amount
        intents.append(
            OrderIntent(
                code=c.code,
                name=c.name,
                side="buy",
                price=price,
                shares=shares,
                amount=round(amount, 2),
                stop_loss=round(price * (1 - stop_loss_pct), 2),
                take_profit=round(price * (1 + take_profit_pct), 2),
                score=float(getattr(c, "score", 0.0)),
                sector=str(getattr(c, "sector_name", "")),
                reason="; ".join(list(getattr(c, "reasons", []))[:3]),
            )
        )
    return intents


def condition_order_text(intent: OrderIntent) -> str:
    """生成通达信条件单/预警表达式（§C.2）。

    涨停价触发买入 + 跌破止损价触发卖出，可直接粘进券商条件单或预警公式。
    """
    return (
        f"{intent.code} {intent.name} | "
        f"买入: 现价>={intent.price:.2f} 且 现价={intent.price:.2f}（打板价） | "
        f"数量: {intent.shares}股 金额: {intent.amount:.0f}元 | "
        f"止损: 现价<={intent.stop_loss:.2f} 止盈: 现价>={intent.take_profit:.2f} | "
        f"理由: {intent.reason}"
    )
