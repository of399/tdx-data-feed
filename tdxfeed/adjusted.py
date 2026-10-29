"""复权读侧咽喉：价格派生只从这里取，因子表只有 data/hfq_factor 一张。

kind="raw"  原样返回日线（不排序、不改列，比照盘上逐值一致）；
kind="hfq"  按日左连 hfq_factor 后 ffill，只乘 OHLC，volume / amount / date 不动。

strict=True（训练 / 建表）在四处 raise：
  - 因子表 raw_close 与日线 close 的中位相对偏差 > 1%（同码双文件：sh000001 指
    数与 000001 平安银行共用 000001.parquet，实测中位偏差 99.65%）；
  - 覆盖率 factor_cov < 0.999；
  - 因子首行晚于日线首行超过 5 个交易日；
  - 因子末日早于日线末日超过 1 个交易日。
strict=False（预测）只记 attrs 与日志——新股 / 缺因子的票不能让整轮预测打挂，
但必须在 attrs 里看得见，不能静默变 raw；raw_close 对不上时**不乘因子**，
宁可让这只票按 raw 走（与它原本的现状一致），也不能乘上别只票的因子。
"""
from __future__ import annotations

import logging

import pandas as pd

from .paths import daily_path, hfq_factor_path

log = logging.getLogger("adjusted")

MIN_FACTOR_COV = 0.999
FACTOR_RAW_TOL = 0.01
HEAD_LAG_MAX = 5
TAIL_LAG_MAX = 1
PRICE_COLS = ("open", "high", "low", "close")


class AdjustmentError(RuntimeError):
    """复权口径门禁未通过。"""


def _raw_close_dev(daily: pd.DataFrame, dates: pd.Series,
                   fac: pd.DataFrame) -> float | None:
    """重叠区 |raw_close / close - 1| 的中位数。

    正常个股是 0.0（同源同参）；sh000001 读 000001.parquet 时实测 99.65%。
    用中位数而不是最大值：个别行的源修订不该把整只票判成错配。
    """
    left = pd.DataFrame({"date": dates, "close": daily["close"].astype(float).to_numpy()})
    ov = left.merge(fac[["date", "raw_close"]], on="date")
    ov = ov[(ov["close"] > 0) & (ov["raw_close"] > 0)]
    if ov.empty:
        return None
    return float((ov["raw_close"] / ov["close"] - 1.0).abs().median())


def frame(symbol: str, *, kind: str = "raw", strict: bool = False) -> pd.DataFrame:
    if kind not in ("raw", "hfq"):
        raise ValueError(f"kind 只接受 raw / hfq：{kind!r}")
    daily = pd.read_parquet(daily_path(symbol))
    if kind == "raw":
        daily.attrs["kind"] = "raw"
        return daily

    daily = daily.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(daily["date"])
    fac_path = hfq_factor_path(symbol)
    fac = None
    if fac_path.exists():
        fac = pd.read_parquet(fac_path, columns=["date", "hfq_factor", "raw_close"])
        fac = fac.assign(date=pd.to_datetime(fac["date"]))
        fac = fac.dropna(subset=["hfq_factor"]).sort_values("date")

    attrs = {"kind": "hfq", "factor_cov": 0.0, "factor_first": None,
             "factor_last": None, "factor_missing": True,
             "factor_head_lag": None, "factor_tail_lag": None,
             "factor_mismatch": False, "factor_raw_dev": None}
    if fac is not None and not fac.empty:
        first, last = fac["date"].iloc[0], fac["date"].iloc[-1]
        head_lag = int((dates < first).sum())
        tail_lag = int((dates > last).sum())
        cov = float(len(daily) - head_lag) / len(daily) if len(daily) else 0.0
        raw_dev = _raw_close_dev(daily, dates, fac)
        attrs.update(factor_cov=cov, factor_first=str(first.date()),
                     factor_last=str(last.date()),
                     factor_missing=cov < MIN_FACTOR_COV,
                     factor_head_lag=head_lag, factor_tail_lag=tail_lag,
                     factor_raw_dev=raw_dev)
        if raw_dev is not None and raw_dev > FACTOR_RAW_TOL:
            attrs.update(factor_cov=0.0, factor_missing=True, factor_mismatch=True)
            msg = (f"因子表 raw_close 与日线 close 中位偏差 {raw_dev:.2%}"
                   f"（> {FACTOR_RAW_TOL:.0%}）：疑似同码双文件，拒绝乘因子")
            if strict:
                raise AdjustmentError(f"{symbol}: {msg}")
            log.warning("%s: %s（按 raw 返回）", symbol, msg)
            out = daily.copy()
            out.attrs.update(attrs)
            return out
        problem = None
        if cov < MIN_FACTOR_COV:
            problem = f"factor_cov={cov:.4f} < {MIN_FACTOR_COV}"
        elif head_lag > HEAD_LAG_MAX:
            problem = f"factor_first={first.date()} 晚于日线首行 {head_lag} 个交易日"
        elif tail_lag > TAIL_LAG_MAX:
            problem = f"factor_last={last.date()} 早于日线末日 {tail_lag} 个交易日"
        if problem:
            if strict:
                raise AdjustmentError(f"{symbol}: {problem}")
            log.warning("%s: %s（按缺失处理，不回落 adj_factor）", symbol, problem)
        aligned = fac.set_index("date")["hfq_factor"].reindex(dates).ffill()
        factor = pd.Series(aligned.fillna(1.0).to_numpy(), index=daily.index)
    elif strict:
        raise AdjustmentError(f"{symbol}: 缺 {fac_path.name}，不能按 hfq 口径建帧")
    else:
        factor = pd.Series(1.0, index=daily.index)
        log.warning("%s: 缺后复权因子文件 %s，按 factor=1.0 处理", symbol, fac_path)

    out = daily.copy()
    for c in PRICE_COLS:
        if c in out.columns:
            out[c] = out[c].astype(float) * factor
    out.attrs.update(attrs)
    return out
