"""阶段 1 — Step 2 候选因子（新增 5~10 个，喂给 Optuna 选择）。

设计（见 docs/stage1_feature_migration_plan.md §2）：动量 / 波动率 / 量价背离方向。
- akquant 负责：RSI / MACD（akquant.talib 双后端指标）。
- Polars + numpy 负责：窗口装配与其余自定义因子。

注意：这些因子在 sig_idx（默认末行 T-1）处取值，但时序类（RSI/MACD/滚动相关/偏度）
需要更长历史，故取 sig_idx 前 `lookback` 根（默认 60）做窗口，取末位值。
所有因子与 features_5d 现有 18/26 维共用同一输入口径（raw/qfq 混合现状原样继承，
不在本模块修正——修正见数据层统一 ingest 口径）。

候选因子（9 个，turnover_rate 因缺流通股数据暂缓）：
  rsi_14, macd_hist, ret_skew_20, vol_term, gap_t1, vp_corr_20,
  amihud_20, rsrs_20, intraday_rev_t1
"""
from __future__ import annotations

import numpy as np
import polars as pl
from scipy import stats

try:
    import akquant.talib as tb
except Exception:  # pragma: no cover - akquant 未装时降级（理论不应发生）
    tb = None

__all__ = ["CANDIDATE_NAMES", "compute_candidate_features"]

CANDIDATE_NAMES = [
    "rsi_14", "macd_hist", "ret_skew_20", "vol_term", "gap_t1",
    "vp_corr_20", "amihud_20", "rsrs_20", "intraday_rev_t1",
]


def compute_candidate_features(
    df: pl.DataFrame, code: str, sig_idx: int = -1, lookback: int = 60
) -> dict | None:
    """计算 sig_idx 处的 9 个候选因子。数据不足返回 None。"""
    n = df.height
    if n < 5:
        return None
    if sig_idx < 0:
        sig_idx = n + sig_idx
    if sig_idx < 4 or sig_idx >= n:
        return None
    lookback = min(lookback, n)

    win = df.slice(max(0, sig_idx - lookback + 1), lookback)
    close = win["close"].to_numpy().astype(float)
    open_ = win["open"].to_numpy().astype(float)
    high = win["high"].to_numpy().astype(float)
    low = win["low"].to_numpy().astype(float)
    amount = win["amount"].to_numpy().astype(float)
    L = len(close)
    si = L - 1  # sig 在窗口内位置（末位）

    pct = np.zeros(L)
    pct[1:] = np.diff(close) / close[:-1]

    # RSI（akquant.talib）
    rsi = 0.0
    macd_hist = 0.0
    if tb is not None:
        rsi = float(np.nan_to_num(tb.RSI(close, timeperiod=14)[si], nan=0.0))
        _, _, mh = tb.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        macd_hist = float(np.nan_to_num(mh[si], nan=0.0))

    # 收益偏度（20 日）
    ret_skew = float(np.nan_to_num(stats.skew(pct[-20:]), nan=0.0)) if L >= 20 else 0.0

    # 波动率期限结构：vol_5d / vol_20d
    v5 = float(np.std(pct[-5:], ddof=1)) if L >= 5 else 0.0
    v20 = float(np.std(pct[-20:], ddof=1)) if L >= 20 else v5
    vol_term = float(v20 / v5) if v5 > 0 else 0.0

    # 跳空：T-1 开盘 / T-2 收盘 - 1
    prev_close = float(close[si - 1]) if si > 0 else float(close[si])
    gap_t1 = float(open_[si] / prev_close - 1.0) if prev_close > 0 else 0.0

    # 量价相关（20 日滚动）
    if L >= 20 and amount[-20:].std() > 0 and close[-20:].std() > 0:
        vp_corr = float(np.corrcoef(amount[-20:], close[-20:])[0, 1])
    else:
        vp_corr = 0.0

    # Amihud 非流动性：|ret| / amount（20 日均值），×1e8 归一到「每亿元成交」量级，
    # 避免 ~1e-11 淹没浮点精度；amount 全 0/缺失时归 0
    amt20 = amount[-20:] if L >= 20 else amount
    pc20 = np.abs(pct[-20:]) if L >= 20 else np.abs(pct)
    denom = np.where(amt20 > 0, amt20, np.nan)
    _ratio = pc20 / denom
    _amihud_mean = float(np.nanmean(_ratio)) * 1e8 if np.any(np.isfinite(_ratio)) else 0.0
    amihud = _amihud_mean

    # RSRS 阻力支撑比：high 对 low 回归斜率（20 日）
    if L >= 20:
        lv = low[-20:]
        hv = high[-20:]
        rsrs = float(np.cov(hv, lv)[0, 1] / lv.var()) if lv.std() > 0 else 0.0
    else:
        rsrs = 0.0

    # 日内反转：(open - close) / |close - prev_close|
    denom2 = abs(float(close[si] - prev_close))
    intraday_rev = float((open_[si] - close[si]) / denom2) if denom2 > 0 else 0.0

    return {
        "rsi_14": rsi,
        "macd_hist": macd_hist,
        "ret_skew_20": ret_skew,
        "vol_term": vol_term,
        "gap_t1": gap_t1,
        "vp_corr_20": vp_corr,
        "amihud_20": amihud,
        "rsrs_20": rsrs,
        "intraday_rev_t1": intraday_rev,
    }
