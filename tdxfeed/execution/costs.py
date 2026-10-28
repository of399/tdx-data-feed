"""交易成本：佣金/印花税/滑点。

两个合同不能合并：
  round_trip —— 比例式，用在「以收益率为输出」的回测（plan_e / sim_live / topk / daban）。
                它没有最低佣金概念：一笔收益率的 5 元下限取决于本金，本金不在入参里。
  cash_pnl   —— 金额式，用在有持仓手数与最低佣金的账本（daban/paper）。
                分母是买入金额（未含滑点），与 paper._net_actual 的 base 定义一致。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    price: float      # 含滑点的成交价
    shares: int
    amount: float     # price * shares
    fee: float
    tax: float = 0.0

    @property
    def cash_out(self) -> float:
        return self.amount + self.fee

    @property
    def cash_in(self) -> float:
        return self.amount - self.fee - self.tax


@dataclass(frozen=True)
class CostModel:
    commission: float = 0.0003
    min_commission: float = 0.0
    stamp_sell: float = 0.0
    slippage: float = 0.0010

    def buy(self, price: float, shares: int) -> Fill:
        px = float(price) * (1.0 + self.slippage)
        amount = px * int(shares)
        fee = max(self.min_commission, amount * self.commission)
        return Fill(price=px, shares=int(shares), amount=amount, fee=fee)

    def sell(self, price: float, shares: int) -> Fill:
        px = float(price) * (1.0 - self.slippage)
        amount = px * int(shares)
        fee = max(self.min_commission, amount * self.commission)
        return Fill(price=px, shares=int(shares), amount=amount, fee=fee,
                    tax=amount * self.stamp_sell)

    def round_trip(self, entry: float, exit_price: float) -> float:
        if self.min_commission > 0:
            raise ValueError("round_trip 不支持最低佣金：本金不在入参里，改用 cash_pnl")
        entry_eff = float(entry) * (1.0 + self.slippage)
        exit_eff = float(exit_price) * (1.0 - self.slippage)
        return (exit_eff * (1.0 - self.commission - self.stamp_sell)
                / (entry_eff * (1.0 + self.commission)) - 1.0)

    def cash_pnl(self, entry: float, exit_price: float, shares: int,
                 *, base: float | None = None) -> float:
        b = self.buy(entry, shares)
        s = self.sell(exit_price, shares)
        denom = base if base is not None else float(entry) * int(shares)
        if denom <= 0:
            return float("nan")
        return (s.cash_in - b.cash_out) / denom


# 方案 E 全链路：佣金 0.03% + 滑点 0.10%，双边，无印花税（口径见 AGENTS.md）
PLAN_E_COSTS = CostModel(commission=0.0003, slippage=0.0010)

# 打板研究：佣金 2.5bp + 印花税 5bp（仅卖出）+ 滑点 5bp
DABAN_COSTS = CostModel(commission=0.00025, stamp_sell=0.0005, slippage=0.0005)
