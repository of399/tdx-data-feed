"""阶段 1 — Polars 因子引擎（复刻 tdxfeed/features_5d.py 语义，保逐位一致）。

设计约束（见 docs/stage1_feature_migration_plan.md）：
  - 现有 18 维 / 扩展 8 维（共 26 维）必须与 features_5d.py 数值一致（容差 1e-9），
    切换前不得改变任何语义（尤其 raw close 口径）。
  - Polars 负责：数据加载、逐股窗口装配、现有特征算术复刻、宽表装配。
  - akquant 负责：新增候选因子（Alpha101 / talib），见后续 Step 2。

输入：Polars DataFrame，含 date, open, high, low, close, volume, amount，按 date 升序。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from .features_5d import get_limit

__all__ = [
    "compute_window_features_extended_polars",
    "compute_window_features_polars",
]


def _window_scalars(df: pl.DataFrame, sig_idx: int) -> dict | None:
    """取 sig_idx 处窗口 T-5..T-1 的标量，复刻 features_5d 的 iloc 切片。

    返回 None 表示数据不足或索引非法（与 features_5d 同口径）。
    """
    n = df.height
    if n < 5:
        return None
    if sig_idx < 0:
        sig_idx = n + sig_idx
    if sig_idx < 4 or sig_idx >= n:
        return None
    p5 = df.slice(sig_idx - 4, 5)
    c = p5["close"].to_list()
    o = p5["open"].to_list()
    h = p5["high"].to_list()
    l = p5["low"].to_list()
    v = p5["volume"].to_list()
    a = p5["amount"].to_list()
    return {
        "c": c, "o": o, "h": h, "l": l, "v": v, "a": a,
        "sig_close": c[-1], "sig_open": o[-1],
        "sig_vol": v[-1], "sig_amt": a[-1],
    }


def compute_window_features_polars(
    df: pl.DataFrame, code: str, sig_idx: int = -1
) -> dict | None:
    """Polars 版 18 维特征，语义与 features_5d.compute_window_features 一致。"""
    n = df.height
    if n < 5:
        return None
    if sig_idx < 0:
        sig_idx = n + sig_idx
    if sig_idx < 4 or sig_idx >= n:
        return None
    w = _window_scalars(df, sig_idx)
    if w is None:
        return None
    c, h, l, a = w["c"], w["h"], w["l"], w["a"]
    sig_close, sig_open = w["sig_close"], w["sig_open"]
    sig_vol, sig_amt = w["sig_vol"], w["sig_amt"]

    # pct_change：首元素 0（对齐 pandas .fillna(0)）；ddof=1 对齐 pandas .std()
    pct = [0.0] + [(c[i] / c[i - 1] - 1.0) if c[i - 1] else 0.0 for i in range(1, 5)]
    limit = float(get_limit(code))
    amt_mean = sum(a) / len(a)
    hi_max, lo_min = max(h), min(l)

    feats = {
        "limit": limit,
        "prev_close": float(sig_close),
        "ret_5d": float(c[-1] / c[0] - 1.0),
        "ret_3d": float(c[-1] / c[-4] - 1.0) if len(c) >= 4 else 0.0,
        "max_5d_pct": float(max(pct)),
        "limit_up_5d": int(sum(1 for x in pct if x >= limit - 0.001)),
        "limit_up_t1": int(sig_close / sig_open - 1.0 >= limit - 0.001) if sig_open > 0 else 0,
        "pct_t1": float(sig_close / sig_open - 1.0) if sig_open > 0 else 0.0,
        "avg_amount_5d": float(amt_mean),
        "vol_ratio_t1": float(a[-1] / amt_mean) if amt_mean > 0 else 1.0,
        "volatility_5d": float(np.std(pct, ddof=1)) if len(pct) > 1 else 0.0,
        "close_pos_5d": float((sig_close - lo_min) / (hi_max - lo_min)) if hi_max > lo_min else 0.5,
        "max_drawdown_5d": float(min(pct)),
        "turnover_proxy": float(sig_amt / sig_vol) if sig_vol > 0 else 0.0,
        "is_st": 0,
        "is_new": int(sig_idx < 250),
        "is_bj": int(str(code).startswith("bj") or str(code).startswith("920")),
        "is_gem": int(
            str(code).startswith("300") or str(code).startswith("301")
            or str(code).startswith("688") or str(code).startswith("sz300")
            or str(code).startswith("sz301") or str(code).startswith("sh688")
        ),
    }
    return feats


def compute_window_features_extended_polars(
    df: pl.DataFrame, code: str, sig_idx: int = -1
) -> dict | None:
    """Polars 版 26 维（18 基础 + 8 扩展），语义与 features_5d 扩展版一致。"""
    base = compute_window_features_polars(df, code, sig_idx=sig_idx)
    if base is None:
        return None
    n = df.height
    if sig_idx < 0:
        sig_idx = n + sig_idx
    p5 = df.slice(sig_idx - 4, 5)
    c = p5["close"].to_list()
    o = p5["open"].to_list()
    h = p5["high"].to_list()
    l = p5["low"].to_list()
    a = p5["amount"].to_list()
    close = c
    pct = [0.0] + [(c[i] / c[i - 1] - 1.0) if c[i - 1] else 0.0 for i in range(1, 5)]
    open_ = o
    daily_range = [(h[i] - l[i]) if (h[i] - l[i]) != 0 else np.nan for i in range(len(h))]
    eff_vals = [(close[i] - open_[i]) / r for i, r in enumerate(daily_range)]
    efficiency = float(np.nanmean(eff_vals)) if eff_vals else 0.0
    if np.isnan(efficiency):
        efficiency = 0.0
    vol_norm = float(base["ret_5d"] / base["volatility_5d"]) if base["volatility_5d"] > 0 else 0.0
    amt_3d_mean = float(sum(a[-3:]) / 3) if len(a) >= 3 else float(sum(a) / len(a))
    surge_3d = float(a[-1] / amt_3d_mean) if amt_3d_mean > 0 else 1.0
    up_days = int(sum(1 for x in pct if x > 0))
    down_days = int(sum(1 for x in pct if x < 0))
    up_down = float(up_days / max(down_days, 1))
    hi_max = float(max(h))
    lo_min = float(min(l))
    dist_high = float((hi_max - close[-1]) / close[-1]) if close[-1] > 0 else 0.0
    dist_low = float((close[-1] - lo_min) / close[-1]) if close[-1] > 0 else 0.0
    amt_per_price = float(base["avg_amount_5d"] / base["prev_close"]) if base["prev_close"] > 0 else 0.0

    # P0-2 涨停/连板特征 (2026-10-18, 与 features_5d 扩展版语义一致):
    # 补"收盘封板"(含一字板) + 连板高度, 修正 limit_up_t1 一字板漏判。
    close_full = df["close"].to_list()
    limit_val = float(base["limit"])
    if sig_idx >= 1:
        limit_up_mask = [
            i >= 1 and (close_full[i] / close_full[i - 1] - 1.0) >= limit_val - 0.001
            if close_full[i - 1] else False
            for i in range(len(close_full))
        ]
        is_limit_today = int(limit_up_mask[sig_idx]) if sig_idx < len(close_full) else 0
        consec = 0
        i = sig_idx
        while i >= 1 and i < len(limit_up_mask) and limit_up_mask[i]:
            consec += 1
            i -= 1
    else:
        is_limit_today, consec = 0, 0
    board_position = 1 if consec == 1 else (2 if consec == 2 else (3 if consec >= 3 else 0))

    return {
        **base,
        "acceleration": float(base["ret_5d"] - base["ret_3d"]),
        "range_efficiency": efficiency,
        "vol_norm_ret": vol_norm,
        "amount_surge_3d": surge_3d,
        "up_down_ratio": up_down,
        "dist_to_5d_high": dist_high,
        "dist_to_5d_low": dist_low,
        "amt_per_price": amt_per_price,
        "is_limit_up_today": is_limit_today,
        "consecutive_boards": consec,
        "board_position": board_position,
    }
