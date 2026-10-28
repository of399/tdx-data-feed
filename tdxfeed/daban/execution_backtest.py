"""成交感知回测：触板信号 → 等 N 根 K 挂涨停买单 → 只有开板才成交。

与 `daban_backtest.py --mode limit` 的区别
------------------------------------------
- 不以“T 日收盘封死”选股；信号在触板时点产生。
- 不假设涨停价一定成交；只有触板后开板（low < limit）才算成交。
- 成交样本天然偏向弱板，因此必须同时报告成交率、成交后封板率和净期望，
  不能只看成交样本的收益。

本模块只做模拟与汇总，不连接券商、不启动纸盘。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from tdxfeed.execution.costs import CostModel

__all__ = [
    "ExecutionConfig",
    "aggregate",
    "net_return",
    "run_rule",
    "select_signals",
    "simulate_signals",
]

EPS = 0.005


@dataclass(slots=True)
class ExecutionConfig:
    wait: int = 1
    exit_mode: str = "open"  # open | close
    fee_bps: float = 2.5
    stamp_bps: float = 5.0
    slip_bps: float = 5.0
    top_k_per_day: int | None = None
    min_prob: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def net_return(entry: float, exit_price: float, cfg: ExecutionConfig) -> float:
    """限价买入（无买入滑点）+ 卖出滑点/双边佣金/印花税。

    不套 `round_trip`：它把双边滑点都算上，而这里是触板价成交的限价单，
    买入侧加 5bp 会让每笔收益凭空少 0.05%；`_exit_price` 的跌停判断读事件表
    预计算的 `next_lim_dn` 列（数据，不是规则计算），同样不动。
    """
    try:
        entry = float(entry)
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        return float("nan")
    if not np.isfinite(entry) or not np.isfinite(exit_price) or entry <= 0 or exit_price <= 0:
        return float("nan")
    cm = CostModel(commission=cfg.fee_bps / 1e4, stamp_sell=cfg.stamp_bps / 1e4,
                   slippage=cfg.slip_bps / 1e4)
    sell = cm.sell(exit_price, 1)
    return (1.0 - cm.commission) * sell.cash_in / entry - 1.0


def select_signals(events: pd.DataFrame, cfg: ExecutionConfig) -> pd.DataFrame:
    """按概率阈值或每日 Top-K 选信号；K 是每日最多信号数。"""
    df = events.copy()
    if cfg.min_prob is not None:
        if "seal_prob" not in df.columns:
            raise ValueError("min_prob 需要 seal_prob 列")
        df = df[df["seal_prob"] >= cfg.min_prob].copy()
    if cfg.top_k_per_day is not None:
        if "seal_prob" not in df.columns:
            raise ValueError("top_k_per_day 需要 seal_prob 列")
        df["_rank"] = df.groupby("date", sort=False)["seal_prob"].rank(
            ascending=False, method="first")
        df = df[df["_rank"] <= int(cfg.top_k_per_day)].drop(columns="_rank")
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def _exit_price(row: pd.Series, cfg: ExecutionConfig) -> tuple[float, bool]:
    if cfg.exit_mode == "open":
        px = float(row.get("next_open", np.nan))
        if np.isfinite(px) and px > 0 and px <= float(row.get("next_lim_dn", -np.inf)) + EPS:
            return float(row.get("next2_open", np.nan)), True
        return px, False
    px = float(row.get("next_close", np.nan))
    if np.isfinite(px) and px > 0 and px <= float(row.get("next_lim_dn", -np.inf)) + EPS:
        return float(row.get("next2_open", np.nan)), True
    return px, False


def simulate_signals(signals: pd.DataFrame, cfg: ExecutionConfig) -> pd.DataFrame:
    """逐信号模拟成交与 T+1 卖出；未成交也保留记录，便于计算成交率。"""
    fill_col = f"fill_w{cfg.wait}"
    entry_col = f"entry_w{cfg.wait}"
    gap_col = f"fill_gap_w{cfg.wait}"
    if fill_col not in signals.columns:
        raise ValueError(f"缺少成交列 {fill_col}")
    rows: list[dict[str, Any]] = []
    for _, r in signals.iterrows():
        base = {
            "code": str(r["code"]),
            "signal_date": str(r["date"]),
            "touch_bar": int(r.get("touch_bar", 0)),
            "cb_prev": int(r.get("cb_prev", 0)),
            "seal_prob": float(r.get("seal_prob", np.nan)),
            "y_sealed_close": int(r.get("y_sealed_close", 0)),
            "fill_gap": int(r.get(gap_col, 0)) if pd.notna(r.get(gap_col, np.nan)) else 0,
        }
        if not bool(r.get(fill_col, False)):
            rows.append({**base, "status": "no_fill", "net": np.nan,
                         "gross": np.nan, "entry": np.nan, "exit": np.nan,
                         "deferred": 0})
            continue
        entry = float(r.get(entry_col, np.nan))
        exit_px, deferred = _exit_price(r, cfg)
        gross = (exit_px / entry - 1.0
                 if np.isfinite(entry) and entry > 0
                 and np.isfinite(exit_px) and exit_px > 0 else np.nan)
        net = net_return(entry, exit_px, cfg)
        status = "ok" if np.isfinite(net) else "invalid"
        rows.append({**base, "status": status, "net": net, "gross": gross,
                     "entry": entry, "exit": exit_px,
                     "deferred": int(deferred)})
    return pd.DataFrame(rows)


def aggregate(trades: pd.DataFrame, n_signals: int) -> dict[str, Any]:
    """汇总成交率、净期望、日度 t、回撤与逆向选择诊断。"""
    ok = trades[trades["status"] == "ok"].copy()
    n_fills = len(ok)
    n_days = int(trades["signal_date"].nunique()) if len(trades) else 0
    out: dict[str, Any] = {
        "n_signals": int(n_signals),
        "n_fills": n_fills,
        "n_no_fill": int((trades["status"] == "no_fill").sum()),
        "n_invalid": int((trades["status"] == "invalid").sum()),
        "fill_rate": n_fills / n_signals if n_signals else float("nan"),
        "n_days": n_days,
        "n_days_with_fills": int(ok["signal_date"].nunique()) if len(ok) else 0,
        "seal_rate_fills": float(ok["y_sealed_close"].mean()) if len(ok) else float("nan"),
        "avg_net": float(ok["net"].mean()) if len(ok) else float("nan"),
        "median_net": float(ok["net"].median()) if len(ok) else float("nan"),
        "avg_gross": float(ok["gross"].mean()) if len(ok) else float("nan"),
        "win_rate": float((ok["net"] > 0).mean()) if len(ok) else float("nan"),
        "avg_net_sealed": float(ok.loc[ok["y_sealed_close"] == 1, "net"].mean())
        if (ok["y_sealed_close"] == 1).any() else float("nan"),
        "avg_net_unsealed": float(ok.loc[ok["y_sealed_close"] == 0, "net"].mean())
        if (ok["y_sealed_close"] == 0).any() else float("nan"),
        "profit_factor": None,
        "daily_mean": float("nan"),
        "t": float("nan"),
        "max_drawdown": float("nan"),
        "total_return": float("nan"),
        "deferred_n": int(ok["deferred"].sum()) if len(ok) else 0,
    }
    if not len(ok):
        return out
    wins = ok.loc[ok["net"] > 0, "net"]
    losses = ok.loc[ok["net"] <= 0, "net"]
    if len(losses) and losses.sum() < 0:
        out["profit_factor"] = float(wins.sum() / abs(losses.sum()))
    daily = ok.groupby("signal_date", sort=True)["net"].mean()
    out["daily_mean"] = float(daily.mean())
    if len(daily) > 2 and daily.std(ddof=1) > 0:
        out["t"] = float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))))
    equity = (1.0 + daily).cumprod()
    out["total_return"] = float(equity.iloc[-1] - 1.0)
    out["max_drawdown"] = float((equity / equity.cummax() - 1.0).min())
    return out


def run_rule(
    events: pd.DataFrame,
    name: str,
    cfg: ExecutionConfig,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """选信号 → 模拟 → 汇总，返回 (summary, trades)。"""
    signals = select_signals(events, cfg)
    trades = simulate_signals(signals, cfg)
    summary = aggregate(trades, n_signals=len(signals))
    summary["rule"] = name
    summary["config"] = cfg.to_dict()
    return summary, trades
