"""方案 D 18 维特征：训练（find_d_signal_5d）与预测（predict）共用单一数据源。

⚠ 关键：所有价格类特征必须使用 **raw close（非前复权）**，
原因：v7 LightGBM 模型是在 raw close 特征上训练的，
预测侧若使用 qfq_close（前复权 × adj_factor）会引入分布漂移，
这是历史 OOS 退化（v7r 续训未提升）的根因之一。

历史口径差异（本模块统一为训练侧口径，作为权威）：

  | 特征         | 训练侧口径（保留）         | 预测侧旧口径（已废弃）        |
  |--------------|----------------------------|-------------------------------|
  | prev_close   | sig_day raw close          | qfq_close × adj_factor       |
  | ret_5d/3d    | prev_5 raw close           | qfq_close                     |
  | limit_up_t1  | open→close (T-1 当日)      | close→close (T-2→T-1)       |
  | pct_t1       | open→close (T-1 当日)      | close→close (T-2→T-1)       |
  | vol_ratio_t1 | amount / mean(amount)      | volume / mean(volume)         |
  | max_dd_5d    | pct_change.min()           | running_max - 1（真最大回撤）|
  | turnover     | amount / volume            | df['turnover'] 或 1.0 占位   |
  | is_new       | sig_idx < 250              | len(df) < 30                  |
  | is_gem       | '300'/'301'/'688' 前缀     | 同（无误判）                  |

18 维特征列顺序（与 LightGBM 模型训练时严格一致）：
  limit, prev_close, ret_5d, ret_3d, max_5d_pct, limit_up_5d, limit_up_t1,
  pct_t1, avg_amount_5d, vol_ratio_t1, volatility_5d, close_pos_5d,
  max_drawdown_5d, turnover_proxy, is_st, is_new, is_bj, is_gem
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["get_limit", "compute_window_features", "compute_window_features_extended", "LIMIT_MAP"]

# 涨停限制常量（与训练脚本一致）
LIMIT_MAP: dict[str, float] = {"main": 0.095, "gem": 0.195, "bse": 0.295}


def get_limit(code: str) -> float:
    """根据代码判断涨停限制。

    支持格式：
      - 'bj920XXX' / '920XXX'     → 北交所 0.30
      - 'sh688XXX' / '688XXX'    → 科创板 0.20
      - 'sz300XXX' / 'sz301XXX' / '300XXX' / '301XXX' → 创业板 0.20
      - 其他                       → 主板 0.10
    """
    code = str(code)
    if code.startswith("bj") or code.startswith("920"):
        return LIMIT_MAP["bse"]
    if (
        code.startswith("300")
        or code.startswith("301")
        or code.startswith("688")
        or code.startswith("sz300")
        or code.startswith("sz301")
        or code.startswith("sh688")
    ):
        return LIMIT_MAP["gem"]
    return LIMIT_MAP["main"]


def compute_window_features(
    df: pd.DataFrame,
    code: str,
    sig_idx: int = -1,
) -> "dict | None":
    """对 df 中 sig_idx 行作为 T-1，计算 18 维特征。

    参数：
      df:     含 open/high/low/close/volume/amount 列的日 K DataFrame，按 date 升序，至少 5 行。
      code:   股票代码（可带或不带 bj/sh/sz 前缀）。
      sig_idx: T-1 在 df 中的索引，默认 -1（最后一行）。

    返回：
      18 维特征 dict；数据不足时返回 None。
    """
    if df is None or len(df) < 5:
        return None

    # 负索引标准化
    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    if sig_idx < 4 or sig_idx >= len(df):
        return None

    # 取末尾 5 行（T-5 ~ T-1）+ T-1 当日
    prev_5 = df.iloc[sig_idx - 4 : sig_idx + 1]
    sig_day = df.iloc[sig_idx]

    limit = get_limit(code)
    pct = prev_5["close"].pct_change().fillna(0)

    feats = {
        # 1. 涨停限制
        "limit": limit,
        # 2. T-1 收盘价（raw close，非前复权）
        "prev_close": float(sig_day["close"]),
        # 3-4. 累计涨幅
        "ret_5d": float(prev_5["close"].iloc[-1] / prev_5["close"].iloc[0] - 1),
        "ret_3d": float(prev_5["close"].iloc[-1] / prev_5["close"].iloc[-4] - 1) if len(prev_5) >= 4 else 0,
        # 5-6. 5 日内单日最大涨幅 / 涨停次数
        "max_5d_pct": float(pct.max()),
        "limit_up_5d": int((pct >= limit - 0.001).sum()),
        # 7-8. T-1 当日是否涨停 / 当日涨幅（open→close）
        "limit_up_t1": int(sig_day["close"] / sig_day["open"] - 1 >= limit - 0.001) if sig_day["open"] > 0 else 0,
        "pct_t1": float(sig_day["close"] / sig_day["open"] - 1) if sig_day["open"] > 0 else 0,
        # 9-10. 5 日均成交额 / T-1 量比（amount-based）
        "avg_amount_5d": float(prev_5["amount"].mean()),
        "vol_ratio_t1": float(sig_day["amount"] / prev_5["amount"].mean()) if prev_5["amount"].mean() > 0 else 1.0,
        # 11. 5 日价格波动率（pct_change 标准差）
        "volatility_5d": float(pct.std()),
        # 12. T-1 收盘价在 5 日 K 线中的位置
        "close_pos_5d": float(
            (sig_day["close"] - prev_5["low"].min()) / (prev_5["high"].max() - prev_5["low"].min())
        )
        if prev_5["high"].max() > prev_5["low"].min()
        else 0.5,
        # 13. 5 日最大单日跌幅（pct_change 最小值，区别于 running max drawdown）
        "max_drawdown_5d": float(pct.min()),
        # 14. T-1 换手代理（amount / volume）
        "turnover_proxy": float(sig_day["amount"] / sig_day["volume"]) if sig_day["volume"] > 0 else 0,
        # 15. 是否 ST（默认 0，由调用方按需通过 name 覆盖）
        "is_st": 0,
        # 16. 是否新股（按 sig_idx < 250，即上市 < 250 个交易日）
        "is_new": int(sig_idx < 250),
        # 17-18. 市场类型（接受 bj/920/sh/sz 前缀或裸 6 位）
        "is_bj": int(code.startswith("bj") or code.startswith("920")),
        "is_gem": int(
            code.startswith("300")
            or code.startswith("301")
            or code.startswith("688")
            or code.startswith("sz300")
            or code.startswith("sz301")
            or code.startswith("sh688")
        ),
    }
    return feats


def compute_window_features_extended(
    df: pd.DataFrame,
    code: str,
    sig_idx: int = -1,
) -> "dict | None":
    """扩展 26 维：18 维基础 + 8 维工程化新特征（v9 模型使用）。

    8 维新特征:
      - acceleration       动量加速度: ret_5d - ret_3d
      - range_efficiency   价格区间效率: mean((close-open)/(high-low))
      - vol_norm_ret       波动率调整收益: ret_5d / volatility_5d
      - amount_surge_3d    3 日成交额突增: amount[-1]/mean(amount[-3:])
      - up_down_ratio      涨跌天数比: up/down
      - dist_to_5d_high    距 5 日高点: (high.max()-close)/close
      - dist_to_5d_low     距 5 日低点: (close-low.min())/close
      - amt_per_price      单位价格成交额: avg_amount_5d/prev_close
    """
    base = compute_window_features(df, code, sig_idx=sig_idx)
    if base is None:
        return None

    # 规范化 sig_idx (与 compute_window_features 内部一致)
    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    prev_5 = df.iloc[sig_idx - 4 : sig_idx + 1]
    close = prev_5["close"]
    pct = close.pct_change().fillna(0)
    amount = prev_5["amount"]
    high = prev_5["high"]
    low = prev_5["low"]
    open_ = prev_5["open"]

    accel = float(base["ret_5d"] - base["ret_3d"])
    daily_range = (high - low).replace(0, np.nan)
    efficiency = float(((close - open_) / daily_range).mean())
    if np.isnan(efficiency):
        efficiency = 0.0
    vol_norm = float(base["ret_5d"] / base["volatility_5d"]) if base["volatility_5d"] > 0 else 0.0
    amount_3d_mean = float(amount.iloc[-3:].mean())
    surge_3d = float(amount.iloc[-1] / amount_3d_mean) if amount_3d_mean > 0 else 1.0
    up_days = int((pct > 0).sum())
    down_days = int((pct < 0).sum())
    up_down = float(up_days / max(down_days, 1))
    high_max = float(high.max())
    dist_high = float((high_max - close.iloc[-1]) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0
    low_min = float(low.min())
    dist_low = float((close.iloc[-1] - low_min) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0
    amt_per_price = float(base["avg_amount_5d"] / base["prev_close"]) if base["prev_close"] > 0 else 0.0

    # P0-2 涨停/连板特征 (2026-10-18, 打板策略升级分析):
    # 现有 limit_up_t1 用 open→close 判日内涨幅, 会漏判"一字板"(开盘即涨停 open=close→0)。
    # 打板核心是连板接力, 故补收盘封板判定 + 连板高度。
    close_full = df["close"]
    if sig_idx >= 1:
        # 收盘封板: 当日 close 相对前日 close >= limit（含一字板）
        limit_up_mask = (close_full.pct_change() >= base["limit"] - 0.001)
        is_limit_today = int(limit_up_mask.iloc[sig_idx])
        # 连板数: 从 sig_idx 往前数连续封板天数
        consec = 0
        i = sig_idx
        while i >= 1 and limit_up_mask.iloc[i]:
            consec += 1
            i -= 1
    else:
        is_limit_today, consec = 0, 0
    board_position = 1 if consec == 1 else (2 if consec == 2 else (3 if consec >= 3 else 0))

    return {
        **base,
        "acceleration": accel,
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
