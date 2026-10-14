"""全成本模型（补齐现役缺口：印花税 / 最低佣金 / 冲击成本）。

成本项（单边）：
- 佣金：max(notional × commission, min_commission)，双边
- 印花税：sell_notional × stamp_tax（仅卖出）
- 滑点：notional × slippage（主板 0.1% / 北交所 0.3%），双边
- 冲击：notional × gamma × sqrt(participation_rate)，双边
全部返回「成本占成交额比例」，由 engine 累加为组合成本拖累。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class CostConfig:
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage_main: float = 0.001
    slippage_bj: float = 0.003
    impact_gamma: float = 0.1


def board_of(code: str) -> str:
    s = str(code).zfill(6)
    if s.startswith("920") or s.startswith("8"):
        return "bj"
    return "main"


def trade_cost(notional: float, side: str, code: str,
               participation_rate: float, cfg: CostConfig) -> float:
    """单边交易成本（绝对值，元）。side: 'buy' | 'sell'。

    冲击成本采用 sqrt 参与率模型，但对参与率与单笔冲击占成交额比例双重封顶，
    避免流动性差个股的冲击成本爆炸（参与率过大时退化为固定上限）。
    """
    if not np.isfinite(notional) or notional <= 0:
        return 0.0
    slip = cfg.slippage_bj if board_of(code) == "bj" else cfg.slippage_main
    comm = max(notional * cfg.commission, cfg.min_commission)
    slip_cost = notional * slip
    pr = min(max(participation_rate, 0.0), 0.10)        # 参与率封顶 10%
    impact = notional * cfg.impact_gamma * np.sqrt(pr)
    impact = min(impact, notional * 0.005)              # 单笔冲击 ≤ 0.5% 成交额（安全下限）
    stamp = notional * cfg.stamp_tax if side == "sell" else 0.0
    return comm + slip_cost + impact + stamp


def portfolio_turnover_cost(trades: pd.DataFrame, cfg: CostConfig) -> dict:
    """trades 列：code, notional_buy, notional_sell, participation_buy, participation_sell。
    返回各成本项总额（元）与占组合价值比例（需调用方补 portfolio_value）。"""
    total = {"commission": 0.0, "stamp_tax": 0.0, "slippage": 0.0,
             "impact": 0.0, "total": 0.0}
    for _, r in trades.iterrows():
        nb = float(r.get("notional_buy", 0.0))
        ns = float(r.get("notional_sell", 0.0))
        pb = float(r.get("participation_buy", 0.0))
        ps = float(r.get("participation_sell", 0.0))
        code = r["code"]
        slip = cfg.slippage_bj if board_of(code) == "bj" else cfg.slippage_main
        # 分项（与 trade_cost 口径一致：佣金含最低 5 元、滑点、冲击双边，印花税仅卖）
        # 冲击成本同样对参与率（封顶 10%）与单笔占成交额比例（封顶 1%）双重封顶，
        # 防止流动性差个股冲击成本爆炸。
        total["commission"] += max(nb * cfg.commission, cfg.min_commission) \
            + max(ns * cfg.commission, cfg.min_commission)
        total["stamp_tax"] += ns * cfg.stamp_tax
        total["slippage"] += (nb + ns) * slip
        pb_c = min(max(pb, 0.0), 0.10)
        ps_c = min(max(ps, 0.0), 0.10)
        ib = min(nb * cfg.impact_gamma * np.sqrt(pb_c), nb * 0.005)
        is_ = min(ns * cfg.impact_gamma * np.sqrt(ps_c), ns * 0.005)
        total["impact"] += ib + is_
    total["total"] = total["commission"] + total["stamp_tax"] \
        + total["slippage"] + total["impact"]
    return total
