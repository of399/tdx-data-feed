"""阶段 1 — Polars 因子引擎（复刻 tdxfeed/features_5d.py 语义，保逐位一致）。

设计约束（见 docs/stage1_feature_migration_plan.md）：
  - 现有 18 维 / 扩展 8 维（共 26 维）必须与 features_5d.py 数值一致（容差 1e-9）；
    价格口径分派与 features_5d 同源（13 hfq / 8 raw），frame_hfq=None 时两者都取 df。
  - Polars 负责：数据加载、逐股窗口装配、现有特征算术复刻、宽表装配。
  - akquant 负责：新增候选因子（Alpha101 / talib），见后续 Step 2。

输入：Polars DataFrame，含 date, open, high, low, close, volume, amount，按 date 升序。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from .features_5d import get_limit, is_limit_up_ret

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


def _px_frame_polars(df: pl.DataFrame, frame_hfq: pl.DataFrame | None) -> pl.DataFrame:
    """价格口径帧：None 时就是 raw 帧。两帧必须同日同长，错位直接 raise。"""
    if frame_hfq is None:
        return df
    if frame_hfq.height != df.height:
        raise ValueError(f"hfq 帧 {frame_hfq.height} 行 != raw 帧 {df.height} 行")
    if not df["date"].equals(frame_hfq["date"]):
        raise ValueError("hfq 帧与 raw 帧的 date 列不逐值相同")
    return frame_hfq


def compute_window_features_polars(
    df: pl.DataFrame, code: str, sig_idx: int = -1,
    frame_hfq: pl.DataFrame | None = None,
) -> dict | None:
    """Polars 版 18 维特征，语义与 features_5d.compute_window_features 一致。"""
    n = df.height
    if n < 5:
        return None
    if sig_idx < 0:
        sig_idx = n + sig_idx
    if sig_idx < 4 or sig_idx >= n:
        return None
    px = _px_frame_polars(df, frame_hfq)
    w = _window_scalars(df, sig_idx)
    wx = _window_scalars(px, sig_idx) if frame_hfq is not None else w
    if w is None or wx is None:
        return None
    c, a = w["c"], w["a"]
    cx, hx, lx = wx["c"], wx["h"], wx["l"]
    sig_close, sig_open = w["sig_close"], w["sig_open"]
    sig_vol, sig_amt = w["sig_vol"], w["sig_amt"]

    def _pct(xs):
        # 首元素 0（对齐 pandas .fillna(0)）；ddof=1 对齐 pandas .std()
        return [0.0] + [(xs[i] / xs[i - 1] - 1.0) if xs[i - 1] else 0.0 for i in range(1, 5)]

    pct_raw = _pct(c)
    pct_px = _pct(cx)
    limit = float(get_limit(code))
    amt_mean = sum(a) / len(a)
    hi_max, lo_min = max(hx), min(lx)

    feats = {
        "limit": limit,
        "prev_close": float(sig_close),
        "ret_5d": float(cx[-1] / cx[0] - 1.0),
        "ret_3d": float(cx[-1] / cx[-4] - 1.0) if len(cx) >= 4 else 0.0,
        "max_5d_pct": float(max(pct_px)),
        "limit_up_5d": int(sum(1 for x in pct_raw if is_limit_up_ret(x, limit))),
        "limit_up_t1": (int(is_limit_up_ret(sig_close / sig_open - 1.0, limit))
                        if sig_open > 0 else 0),
        "pct_t1": float(wx["sig_close"] / wx["sig_open"] - 1.0) if wx["sig_open"] > 0 else 0.0,
        "avg_amount_5d": float(amt_mean),
        "vol_ratio_t1": float(a[-1] / amt_mean) if amt_mean > 0 else 1.0,
        "volatility_5d": float(np.std(pct_px, ddof=1)) if len(pct_px) > 1 else 0.0,
        "close_pos_5d": float((wx["sig_close"] - lo_min) / (hi_max - lo_min)) if hi_max > lo_min else 0.5,
        "max_drawdown_5d": float(min(pct_px)),
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
    df: pl.DataFrame, code: str, sig_idx: int = -1,
    frame_hfq: pl.DataFrame | None = None,
) -> dict | None:
    """Polars 版 26 维（18 基础 + 8 扩展），语义与 features_5d 扩展版一致。"""
    base = compute_window_features_polars(df, code, sig_idx=sig_idx, frame_hfq=frame_hfq)
    if base is None:
        return None
    n = df.height
    if sig_idx < 0:
        sig_idx = n + sig_idx
    px = _px_frame_polars(df, frame_hfq)
    p5 = px.slice(sig_idx - 4, 5)
    a = df.slice(sig_idx - 4, 5)["amount"].to_list()
    c = p5["close"].to_list()
    o = p5["open"].to_list()
    h = p5["high"].to_list()
    l = p5["low"].to_list()
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
    # 封板 / 连板只认 raw（与 features_5d 同一条判据）。
    close_full = df["close"].to_list()
    limit_val = float(base["limit"])
    if sig_idx >= 1:
        limit_up_mask = [
            i >= 1
            and is_limit_up_ret(close_full[i] / close_full[i - 1] - 1.0, limit_val)
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
