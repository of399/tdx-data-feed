"""方案 D 18 维特征：训练（find_d_signal_5d）与预测（predict）共用单一数据源。

⚠ 价格类特征分两口径（2026-10-30 起）：
  hfq（13 维）：ret_5d/ret_3d/max_5d_pct/pct_t1/volatility_5d/close_pos_5d/
    max_drawdown_5d/acceleration/range_efficiency/vol_norm_ret/up_down_ratio/
    dist_to_5d_high/dist_to_5d_low —— 除权日必须连续，取 frame_hfq；
  raw（8 维）：limit/prev_close/limit_up_5d/limit_up_t1/is_limit_up_today/
    consecutive_boards/board_position/amt_per_price —— 交易所绝对价规则。
  frame_hfq=None 时两口径都取 df，即历史 raw 行为。

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

from decimal import Decimal

import numpy as np
import pandas as pd

from .execution import limits as _limits
from .names import is_st_name as _is_st_name

__all__ = [
    "LIMIT_MAP",
    "MAIN_ST_RULE_CHANGE_DATE",
    "compute_window_features",
    "compute_window_features_extended",
    "get_limit",
    "is_limit_up_ret",
    "is_open_limit_up",
    "is_yizi",
]

# 涨停判定阈值：从内核的精确比例减缓冲派生，不再手抄字面量（P1-12 Phase 3）。
_THRESHOLD_BUFFER = Decimal(str(_limits.DEFAULT_THRESHOLD_BUFFER))
LIMIT_MAP: dict[str, float] = {
    market: float(ratio - _THRESHOLD_BUFFER) for market, ratio in _limits.LIMIT_RATIO.items()
}

# 主板 ST/*ST 在 2026-07-06 与主板普通票并轨（±10%）之前是 ±5%，阈值同样留缓冲。
# 按 R17 保留为规则名（无调用方，判定一律走 get_limit → limits.limit_threshold）。
LIMIT_ST_MAIN_OLD = float(_limits.LIMIT_RATIO_ST_MAIN_OLD - _THRESHOLD_BUFFER)

MAIN_ST_RULE_CHANGE_DATE = _limits.MAIN_ST_RULE_CHANGE_DATE


def get_limit(code: str, name: str = "", on_date=None, is_st: bool | None = None) -> float:
    """涨停判定阈值。委托 `tdxfeed.execution.limits.limit_threshold`（唯一实现）。

    name / on_date 只影响主板 ST/*ST 一档：早于 2026-07-06 取 5% 档，之后与主板
    普通票并轨。北交所按代码前缀（920 / 8 / 4）与 `bj` 前缀判定，不再要求显式前缀。
    做 2020-2026 的历史区间一定要把交易日传进来，否则并轨前的主板 ST 会被误判成 10% 档。

    `is_st` 给定时（面板按日供给的真实状态，见 `tdxfeed/st_supply.py`）以它为准；
    只有 `is_st=None` 才回落到 `_is_st_name(name)`——名称是**当日快照**口径，
    拿它判历史某一天会把「曾经 ST、后来撤销」的票判错，这正是 KI-DATA-008 的病灶之一。
    """
    flag = _is_st_name(name) if is_st is None else bool(is_st)
    return _limits.limit_threshold(code, is_st=flag, on_date=on_date)


def is_limit_up_ret(ret: float, limit: float) -> bool:
    """涨幅是否达到涨停限制（ret 口径）。转发 `tdxfeed.execution.limits.is_limit_up_ret`。

    审计 P1 公共化（2026-10-19）先把散在 5 个文件（train_daban_model /
    diagnose_sample_design / compare_daban_vs_stage1 / verify_daban_hit /
    factors_polars）的同一判定收敛到本模块；P1-12 Phase 3（2026-11-02）再收敛一层
    到内核：容差由 `limits.RET_TOLERANCE` 单一提供（浮点容忍，规避 0.0999999 vs 0.10
    这类误差），本函数不再手抄。标量调用点走本函数，向量化调用点直接减该常量。
    """
    return _limits.is_limit_up_ret(ret, limit)


def is_open_limit_up(open_: float, prev_close: float, limit: float) -> bool:
    """开盘即涨停判定（不可交易口径）。

    当日开盘价已达涨停价 → 一字或准一字板 → 打板者通常买不进。
    T 日（信号日/买入日）命中此条的样本实盘不可执行，应剔除。
    """
    if prev_close <= 0:
        return False
    return is_limit_up_ret(open_ / prev_close - 1.0, limit)


def is_yizi(open_: float, high: float, low: float, close: float) -> bool:
    """一字板判定：四价合一（开=高=低=收）。"""
    return open_ == high == low == close


def _px_frame(df: pd.DataFrame, frame_hfq: pd.DataFrame | None) -> pd.DataFrame:
    """价格口径帧：None 时就是 raw 帧。两帧必须同日同长，错位直接 raise。"""
    if frame_hfq is None:
        return df
    if len(frame_hfq) != len(df):
        raise ValueError(f"hfq 帧 {len(frame_hfq)} 行 != raw 帧 {len(df)} 行")
    a = pd.to_datetime(df["date"]).reset_index(drop=True)
    b = pd.to_datetime(frame_hfq["date"]).reset_index(drop=True)
    if not a.equals(b):
        raise ValueError("hfq 帧与 raw 帧的 date 列不逐值相同")
    return frame_hfq


def compute_window_features(
    df: pd.DataFrame,
    code: str,
    sig_idx: int = -1,
    frame_hfq: pd.DataFrame | None = None,
    *,
    is_st: bool | None = None,
) -> dict | None:
    """对 df 中 sig_idx 行作为 T-1，计算 18 维特征。

    参数：
      df:     含 open/high/low/close/volume/amount 列的日 K DataFrame，按 date 升序，至少 5 行。
      code:   股票代码（可带或不带 bj/sh/sz 前缀）。
      sig_idx: T-1 在 df 中的索引，默认 -1（最后一行）。
      frame_hfq: 同日的后复权帧；价格比率维取它，涨停/成交额仍取 df。
      is_st:  信号日 T 的真实 ST 状态（由 `tdxfeed/st_supply.py` 按日供给）。
              `None` = **未供给**，按现行口径（is_st=False）供给，与改动前逐值相同；
              这一档的存在是为了让研究/基准脚本保持可复现，不是回落借口——
              生产侧（训练与在役推理）必须显式供给，见 KI-DATA-008 与
              `tests/test_limit_on_date_in_production.py`。

    返回：
      18 维特征 dict；数据不足时返回 None。**不新增键**（列集是 29 维契约的一部分，
      由 `test_extended_features_column_set_does_not_grow` 钉住）。
    """
    if df is None or len(df) < 5:
        return None

    # 负索引标准化
    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    if sig_idx < 4 or sig_idx >= len(df):
        return None
    px = _px_frame(df, frame_hfq)

    # 取末尾 5 行（T-5 ~ T-1）+ T-1 当日
    prev_5 = df.iloc[sig_idx - 4 : sig_idx + 1]        # raw：涨停 / 成交额
    px_5 = px.iloc[sig_idx - 4 : sig_idx + 1]          # 价格口径：比率 / 位置
    sig_day = df.iloc[sig_idx]
    px_day = px.iloc[sig_idx]

    # B1（KI-DATA-008）：阈值按**信号日 T** 取档，ST 状态由调用方按日供给。
    # 与 `scripts/audit_daban_st_tier_pollution.py` 的 52,919 同轴（判定挂 T，不是 T+1）：
    # 换成 T+1 会在 ST 实施/撤销边界错开一根 K 线，本批不换轴。
    sig_date = _limits.as_date(sig_day["date"])
    st_flag = False if is_st is None else bool(is_st)
    limit = get_limit(code, on_date=sig_date, is_st=st_flag)
    pct_raw = prev_5["close"].pct_change().fillna(0)
    pct_px = px_5["close"].pct_change().fillna(0)
    hi_max, lo_min = px_5["high"].max(), px_5["low"].min()

    feats = {
        # 1. 涨停限制
        "limit": limit,
        # 2. T-1 收盘价（raw：交易所绝对价）
        "prev_close": float(sig_day["close"]),
        # 3-4. 累计涨幅
        "ret_5d": float(px_5["close"].iloc[-1] / px_5["close"].iloc[0] - 1),
        "ret_3d": float(px_5["close"].iloc[-1] / px_5["close"].iloc[-4] - 1) if len(px_5) >= 4 else 0,
        # 5-6. 5 日内单日最大涨幅 / 涨停次数
        "max_5d_pct": float(pct_px.max()),
        "limit_up_5d": int((pct_raw >= limit - _limits.RET_TOLERANCE).sum()),
        # 7-8. T-1 当日是否涨停 / 当日涨幅（open→close）
        "limit_up_t1": int(is_limit_up_ret(sig_day["close"] / sig_day["open"] - 1, limit)) if sig_day["open"] > 0 else 0,
        "pct_t1": float(px_day["close"] / px_day["open"] - 1) if px_day["open"] > 0 else 0,
        # 9-10. 5 日均成交额 / T-1 量比（amount-based）
        "avg_amount_5d": float(prev_5["amount"].mean()),
        "vol_ratio_t1": float(sig_day["amount"] / prev_5["amount"].mean()) if prev_5["amount"].mean() > 0 else 1.0,
        # 11. 5 日价格波动率（pct_change 标准差）
        "volatility_5d": float(pct_px.std()),
        # 12. T-1 收盘价在 5 日 K 线中的位置
        "close_pos_5d": float((px_day["close"] - lo_min) / (hi_max - lo_min)) if hi_max > lo_min else 0.5,
        # 13. 5 日最大单日跌幅（pct_change 最小值，区别于 running max drawdown）
        "max_drawdown_5d": float(pct_px.min()),
        # 14. T-1 换手代理（amount / volume）
        "turnover_proxy": float(sig_day["amount"] / sig_day["volume"]) if sig_day["volume"] > 0 else 0,
        # 15. 是否 ST：按信号日 T 的真实状态供给（B1 前这行写死 0，见 KI-DATA-008）。
        #     未供给（is_st=None）时仍是 0 ⇒ 与改动前的矩阵逐值相同；面板判「无法判定」
        #     的行由调用方记账（`tdxfeed/st_supply.py::TierTally`），不在这里折成「不是 ST」。
        "is_st": int(st_flag),
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
    frame_hfq: pd.DataFrame | None = None,
    *,
    is_st: bool | None = None,
) -> dict | None:
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
    base = compute_window_features(df, code, sig_idx=sig_idx, frame_hfq=frame_hfq,
                                   is_st=is_st)
    if base is None:
        return None

    # 规范化 sig_idx (与 compute_window_features 内部一致)
    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    px = _px_frame(df, frame_hfq)
    px_5 = px.iloc[sig_idx - 4 : sig_idx + 1]
    raw_5 = df.iloc[sig_idx - 4 : sig_idx + 1]
    close = px_5["close"]
    pct = close.pct_change().fillna(0)
    amount = raw_5["amount"]
    high = px_5["high"]
    low = px_5["low"]
    open_ = px_5["open"]

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

    # 封板 / 连板只认 raw：涨停是交易所绝对价规则，复权序列上的涨幅没有意义。
    close_full = df["close"]
    if sig_idx >= 1:
        limit_up_mask = (close_full.pct_change() >= base["limit"] - _limits.RET_TOLERANCE)
        is_limit_today = int(limit_up_mask.iloc[sig_idx])
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
