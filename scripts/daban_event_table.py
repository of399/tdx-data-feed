#!/usr/bin/env python
"""Stage 0：构建打板触板事件表（触板时点可得特征 + 事后标签分离）。

数据源
------
- ``data/daban/min60_features_YYYYMMDD.parquet``：股票-日触板/封板与下一日价格
- ``data/parquet/min60``：4 根 60 分钟 K，用于重建触板前量价与“触板后开板成交”
- ``data/parquet/daily``：触板前 5/20 日流动性、动量、位置
- ``data/industry/sw_stock_industry.csv``：申万行业映射（当前版本，历史归属有偏差）

时点纪律
--------
- 特征只用到触板前已经收盘的 60 分钟 K；触板所在 K 只用其开盘价。
- 板块/市场宽度用“触板前一根已收盘 K”的横截面；首根 K 触板时退到前一交易日 15:00。
- ``sealed_close``、``first_seal``、``reopen_count``、成交标签、T+1 收益全部是
  事后标签，统一加 ``y_`` / ``ret_`` / ``fill_`` 前缀，分类器不得使用。

已知缺口
--------
- 历史数据没有逐笔委托队列、封单额、实时板块涨停家数；这些只能前向采集。
- min60 无法区分 10:00 前首封，只能用首触 K=1（09:30-10:30）近似。
- 行业映射是当前版本，存在历史归属/幸存者偏差。
- 本表不含流通市值，市值过滤只能在前向或另接历史股本数据后执行。

用法::

    venv/bin/python scripts/daban_event_table.py
    venv/bin/python scripts/daban_event_table.py --limit 200   # 冒烟
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.scanner import daily_path  # noqa: E402
from tdxfeed.daban.sector import load_industry_map  # noqa: E402

FEATURES = ROOT / "data/daban/min60_features_20261022.parquet"
OUT = ROOT / "data/daban/event_table_min60_20261022.parquet"
META = ROOT / "data/daban/event_table_min60_20261022.meta.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import get_limit, minute_dir  # noqa: E402

MIN60_DIR = minute_dir("min60")
BARS = ("1030", "1130", "1400", "1500")
EPS = 0.005

TOUCH_TIME = {
    1: "09:30-10:30",
    2: "10:30-11:30",
    3: "13:00-14:00",
    4: "14:00-15:00",
}

FEATURE_COLUMNS = [
    "touch_bar",
    "open_gap",
    "pre_ret",
    "pre_high_to_limit",
    "pre_range_pct",
    "pre_vol_ratio",
    "pre_amount_ratio",
    "touch_open_gap",
    "cb_prev",
    "is_st",
    "is_new",
    "ratio",
    "log_prior_amount",
    "amount_ratio_5d",
    "amount_ratio_20d",
    "log_price",
    "ret_5d",
    "ret_20d",
    "vol_20d",
    "dist_high_20d",
    "dist_ma5",
    "dist_ma10",
    "dist_ma20",
    "big_up_20d",
    "max_ret_20d",
    "prior_day_ret",
    "sec_n",
    "sec_limitup_n",
    "sec_median_ret",
    "sec_leader_pct",
    "sec_rank_pct",
    "sec_breadth",
    "sec_score",
    "mkt_n",
    "mkt_limitup_n",
    "mkt_median_ret",
    "mkt_breadth",
]

LABEL_COLUMNS = [
    "y_sealed_close",
    "y_first_seal_bar",
    "y_n_sealed_bars",
    "y_reopen_count",
    "y_seal_strength",
]


def _as_date_str(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype("int64").astype(str)
    return pd.to_datetime(s).dt.strftime("%Y%m%d")


def _raw_min60(code: str) -> dict[str, dict[str, np.ndarray]] | None:
    p = MIN60_DIR / f"{exchange_prefix(code)}{code}.parquet"
    if not p.exists():
        return None
    try:
        m = pd.read_parquet(p, columns=["datetime", "open", "high", "low", "close",
                                        "volume", "amount"])
    except Exception:
        return None
    if m.empty:
        return None
    dt = pd.to_datetime(m["datetime"])
    m = m.assign(date=dt.dt.strftime("%Y%m%d"), bar=dt.dt.strftime("%H%M"))
    m = m[m["bar"].isin(BARS)]
    if m.empty:
        return None
    out: dict[str, dict[str, np.ndarray]] = {}
    for date, g in m.groupby("date", sort=False):
        g = g.set_index("bar").reindex(BARS)
        if g["open"].isna().any():
            continue
        out[str(date)] = {
            "o": g["open"].to_numpy(dtype=float),
            "h": g["high"].to_numpy(dtype=float),
            "l": g["low"].to_numpy(dtype=float),
            "c": g["close"].to_numpy(dtype=float),
            "v": g["volume"].to_numpy(dtype=float),
            "a": g["amount"].to_numpy(dtype=float),
        }
    return out or None


def _daily_history(code: str) -> pd.DataFrame | None:
    p = daily_path(code)
    if p is None:
        return None
    try:
        d = pd.read_parquet(p, columns=["date", "high", "low", "close", "volume", "amount"])
    except Exception:
        return None
    if d is None or d.empty:
        return None
    d["date"] = _as_date_str(d["date"])
    d = d.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    d["prior_close"] = d["close"].shift(1)
    d["prior_amount"] = d["amount"].shift(1)
    # 日线 volume 单位是手，min60 volume 是股；统一成股再比较。
    d["prior_vol_5d"] = d["volume"].shift(1).rolling(5, min_periods=3).mean() * 100.0
    d["prior_amount_5d"] = d["amount"].shift(1).rolling(5, min_periods=3).mean()
    d["prior_amount_20d"] = d["amount"].shift(1).rolling(20, min_periods=10).mean()
    d["ret_5d"] = d["close"].shift(1) / d["close"].shift(6) - 1.0
    d["ret_20d"] = d["close"].shift(1) / d["close"].shift(21) - 1.0
    d["vol_20d"] = d["close"].pct_change().shift(1).rolling(20, min_periods=10).std()
    d["high_20d"] = d["high"].shift(1).rolling(20, min_periods=10).max()
    d["dist_high_20d"] = d["prior_close"] / d["high_20d"] - 1.0
    d["prior_day_ret"] = d["close"].shift(1) / d["close"].shift(2) - 1.0
    d["amount_ratio_5d"] = d["prior_amount"] / d["prior_amount_5d"]
    d["amount_ratio_20d"] = d["prior_amount"] / d["prior_amount_20d"]
    d["log_price"] = np.log(d["prior_close"].where(d["prior_close"] > 0))
    for n in (5, 10, 20):
        ma = d["close"].shift(1).rolling(n, min_periods=max(3, n // 2)).mean()
        d[f"dist_ma{n}"] = d["prior_close"] / ma - 1.0
    # 近 20 日“大涨”次数（主板 9.5%，双创 19.5%，北交所 29.5%）；ST 已从可执行池剔除。
    threshold = get_limit(code)
    ret = d["close"].pct_change()
    d["big_up_20d"] = (ret >= threshold).astype(int).shift(1).rolling(
        20, min_periods=10).sum()
    d["max_ret_20d"] = ret.shift(1).rolling(20, min_periods=10).max()
    keep = ["date", "prior_close", "prior_amount", "prior_vol_5d",
            "prior_amount_5d", "prior_amount_20d", "ret_5d", "ret_20d",
            "vol_20d", "dist_high_20d", "prior_day_ret", "amount_ratio_5d",
            "amount_ratio_20d", "log_price", "dist_ma5", "dist_ma10",
            "dist_ma20", "big_up_20d", "max_ret_20d"]
    return d[keep]


def _bar_stats(features: pd.DataFrame, industry_map: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按日期/行业/已收盘 K 计算板块与市场宽度（0~3 对应 4 根 K）。"""
    f = features[~features["suspect_ref"].astype(bool)].copy()
    f["code"] = f["code"].astype(str).str.zfill(6)
    f["industry"] = f["code"].map(industry_map)
    sector_parts: list[pd.DataFrame] = []
    market_parts: list[pd.DataFrame] = []
    prev = f["prev_close"].to_numpy(dtype=float)
    lim = f["limit"].to_numpy(dtype=float)
    base_ok = np.isfinite(prev) & (prev > 0) & np.isfinite(lim) & (lim > 0)
    for j in range(4):
        close = f[f"c{j}"].to_numpy(dtype=float)
        ok = base_ok & np.isfinite(close)
        pct = np.where(ok, close / np.where(prev > 0, prev, np.nan) - 1.0, np.nan)
        tmp = pd.DataFrame({
            "date": f["date"].to_numpy()[ok],
            "industry": f["industry"].to_numpy()[ok],
            "pct": pct[ok],
            "sealed": (close[ok] >= lim[ok] - EPS),
        })
        tmp = tmp[np.isfinite(tmp["pct"].to_numpy(dtype=float))]
        if tmp.empty:
            continue

        mkt = tmp.groupby("date", sort=False).agg(
            mkt_n=("pct", "size"),
            mkt_limitup_n=("sealed", "sum"),
            mkt_median_ret=("pct", "median"),
        )
        mkt["mkt_breadth"] = (tmp["pct"] > 0).groupby(tmp["date"]).mean()
        mkt["bar"] = j + 1
        market_parts.append(mkt.reset_index())

        s = tmp.dropna(subset=["industry"])
        if s.empty:
            continue
        g = s.groupby(["date", "industry"], sort=False)
        agg = g.agg(
            sec_n=("pct", "size"),
            sec_limitup_n=("sealed", "sum"),
            sec_median_ret=("pct", "median"),
            sec_leader_pct=("pct", "max"),
        )
        agg["sec_breadth"] = (s["pct"] > 0).groupby([s["date"], s["industry"]]).mean()
        agg = agg[agg["sec_n"] >= 3].copy()
        if agg.empty:
            continue
        agg["sec_rank_pct"] = agg.groupby(level="date")["sec_median_ret"].rank(pct=True)
        agg["sec_score"] = (
            40.0 * agg["sec_rank_pct"]
            + 40.0 * agg["sec_limitup_n"].clip(upper=10) / 10.0
            + 20.0 * agg["sec_leader_pct"].clip(lower=0, upper=0.10) / 0.10
        )
        agg["bar"] = j + 1
        sector_parts.append(agg.reset_index())

    sector = pd.concat(sector_parts, ignore_index=True) if sector_parts else pd.DataFrame()
    market = pd.concat(market_parts, ignore_index=True) if market_parts else pd.DataFrame()
    return sector, market


def _exit_price(row: pd.Series, mode: str) -> tuple[float, bool]:
    if mode == "open":
        px = float(row.get("next_open", np.nan))
        if not np.isfinite(px) or px <= 0:
            return np.nan, False
        if px <= float(row.get("next_lim_dn", -np.inf)) + EPS:
            return float(row.get("next2_open", np.nan)), True
        return px, False
    px = float(row.get("next_close", np.nan))
    if not np.isfinite(px) or px <= 0:
        return np.nan, False
    if px <= float(row.get("next_lim_dn", -np.inf)) + EPS:
        return float(row.get("next2_open", np.nan)), True
    return px, False


def build(features_path: Path, limit: int = 0) -> tuple[pd.DataFrame, dict]:
    raw_features = pd.read_parquet(features_path)
    raw_features["date"] = raw_features["date"].astype(str)
    raw_features["code"] = raw_features["code"].astype(str).str.zfill(6)
    raw_features = raw_features.sort_values(["code", "date"]).reset_index(drop=True)
    raw_features["cb_prev"] = (
        raw_features.groupby("code", sort=False)["consecutive_boards"]
        .shift(1).fillna(0).astype(int)
    )

    valid = raw_features[
        (~raw_features["suspect_ref"].astype(bool))
        & raw_features["touched"].astype(bool)
        & np.isfinite(raw_features["prev_close"].to_numpy(dtype=float))
        & (raw_features["prev_close"].to_numpy(dtype=float) > 0)
    ].copy()
    if limit:
        codes = sorted(valid["code"].unique())[:limit]
        valid = valid[valid["code"].isin(codes)].copy()
    print(f"[info] 触板事件 {len(valid):,} / {valid['code'].nunique():,} 只 / "
          f"{valid['date'].nunique()} 天")

    industry_map = load_industry_map()
    print(f"[info] 行业映射 {len(industry_map):,} 只")
    sector, market = _bar_stats(raw_features, industry_map)
    print(f"[info] 板块-日-K 统计 {len(sector):,} 行；市场 {len(market):,} 行")

    dates = np.array(sorted(raw_features["date"].unique()))
    prev_date = dict(zip(dates[1:], dates[:-1], strict=True))
    valid["prior_date"] = np.where(
        valid["first_touch"].to_numpy(dtype=int) > 1,
        valid["date"].to_numpy(),
        valid["date"].map(prev_date).to_numpy(),
    )
    valid["prior_bar"] = np.where(
        valid["first_touch"].to_numpy(dtype=int) > 1,
        valid["first_touch"].to_numpy(dtype=int) - 1,
        4,
    )
    valid["industry"] = valid["code"].map(industry_map)

    if not sector.empty:
        sector = sector.rename(columns={"date": "prior_date", "bar": "prior_bar"})
        valid = valid.merge(
            sector[["prior_date", "industry", "prior_bar", "sec_n", "sec_limitup_n",
                    "sec_median_ret", "sec_leader_pct", "sec_rank_pct", "sec_breadth",
                    "sec_score"]],
            on=["prior_date", "industry", "prior_bar"], how="left",
        )
    if not market.empty:
        market = market.rename(columns={"date": "prior_date", "bar": "prior_bar"})
        valid = valid.merge(
            market[["prior_date", "prior_bar", "mkt_n", "mkt_limitup_n",
                    "mkt_median_ret", "mkt_breadth"]],
            on=["prior_date", "prior_bar"], how="left",
        )

    parts: list[pd.DataFrame] = []
    for i, (code, ev) in enumerate(valid.groupby("code", sort=False), 1):
        bars = _raw_min60(code)
        hist = _daily_history(code)
        if bars is None or hist is None:
            continue
        ev = ev.merge(hist, on="date", how="left")
        rows = []
        for _, r in ev.iterrows():
            day = bars.get(str(r["date"]))
            if day is None:
                continue
            k = int(r["first_touch"])
            if k < 1 or k > 4:
                continue
            o, h, l, c, v, a = day["o"], day["h"], day["l"], day["c"], day["v"], day["a"]
            prev_close = float(r["prev_close"])
            limit = float(r["limit"])
            open_gap = o[0] / prev_close - 1.0
            if k > 1:
                pre_h = float(np.nanmax(h[: k - 1]))
                pre_l = float(np.nanmin(l[: k - 1]))
                pre_close = float(c[k - 2])
                pre_vol = float(np.nansum(v[: k - 1]))
                pre_amt = float(np.nansum(a[: k - 1]))
                pre_ret = pre_close / prev_close - 1.0
                pre_high_to_limit = pre_h / limit - 1.0
                pre_range = (pre_h - pre_l) / prev_close
            else:
                pre_ret = open_gap
                pre_high_to_limit = np.nan
                pre_range = np.nan
                pre_vol = np.nan
                pre_amt = np.nan
            touch_open_gap = o[k - 1] / prev_close - 1.0

            base = {
                "code": str(r["code"]),
                "date": str(r["date"]),
                "industry": r.get("industry", ""),
                "first_touch": k,
                "touch_bar": k,
                "touch_time": TOUCH_TIME[k],
                "open_limit": int(bool(r["open_limit"])),
                "is_st": int(r["is_st"]),
                "is_new": int(r["is_new"]),
                "ratio": float(r["ratio"]),
                "cb_prev": int(r["cb_prev"]),
                "prev_close": prev_close,
                "limit": limit,
                "open_gap": open_gap,
                "pre_ret": pre_ret,
                "pre_high_to_limit": pre_high_to_limit,
                "pre_range_pct": pre_range,
                "pre_vol_ratio": (float(np.clip(pre_vol / float(r["prior_vol_5d"]), 0.0, 5.0))
                                  if np.isfinite(pre_vol) and r.get("prior_vol_5d", 0) > 0
                                  else np.nan),
                "pre_amount_ratio": (float(np.clip(pre_amt / float(r["prior_amount_5d"]), 0.0, 5.0))
                                     if np.isfinite(pre_amt) and r.get("prior_amount_5d", 0) > 0
                                     else np.nan),
                "touch_open_gap": touch_open_gap,
                "log_prior_amount": (np.log1p(float(r["prior_amount"]))
                                     if np.isfinite(r.get("prior_amount", np.nan))
                                     and r.get("prior_amount", 0) > 0 else np.nan),
                "amount_ratio_5d": float(r.get("amount_ratio_5d", np.nan)),
                "amount_ratio_20d": float(np.clip(r.get("amount_ratio_20d", np.nan), 0.0, 10.0)),
                "log_price": float(r.get("log_price", np.nan)),
                "ret_5d": float(np.clip(r.get("ret_5d", np.nan), -0.8, 2.0)),
                "ret_20d": float(np.clip(r.get("ret_20d", np.nan), -0.9, 4.0)),
                "vol_20d": float(r.get("vol_20d", np.nan)),
                "dist_high_20d": float(np.clip(r.get("dist_high_20d", np.nan), -0.9, 0.5)),
                "dist_ma5": float(np.clip(r.get("dist_ma5", np.nan), -0.5, 0.5)),
                "dist_ma10": float(np.clip(r.get("dist_ma10", np.nan), -0.6, 0.6)),
                "dist_ma20": float(np.clip(r.get("dist_ma20", np.nan), -0.7, 0.7)),
                "big_up_20d": float(r.get("big_up_20d", np.nan)),
                "max_ret_20d": float(np.clip(r.get("max_ret_20d", np.nan), -0.3, 0.5)),
                "prior_day_ret": float(np.clip(r.get("prior_day_ret", np.nan), -0.3, 0.3)),
                "y_sealed_close": int(bool(r["sealed_close"])),
                "y_first_seal_bar": int(r["first_seal"]),
                "y_n_sealed_bars": int(r["n_sealed_bars"]),
                "y_reopen_count": int(r["reopen_count"]),
                "y_seal_strength": float(r["seal_strength"]),
                "next_open": float(r.get("next_open", np.nan)),
                "next_close": float(r.get("next_close", np.nan)),
                "next2_open": float(r.get("next2_open", np.nan)),
                "next_lim_dn": float(r.get("next_lim_dn", np.nan)),
                "next_date": str(r.get("next_date", "")),
            }
            for col in ("sec_n", "sec_limitup_n", "sec_median_ret", "sec_leader_pct",
                        "sec_rank_pct", "sec_breadth", "sec_score", "mkt_n",
                        "mkt_limitup_n", "mkt_median_ret", "mkt_breadth"):
                base[col] = float(r[col]) if col in r and pd.notna(r[col]) else np.nan

            # 触板即买（不可成交，仅作事后上界对照）
            for mode in ("open", "close"):
                px, defer = _exit_price(r, mode)
                base[f"ret_touch_{mode}_gross"] = (
                    px / limit - 1.0 if np.isfinite(px) and px > 0 and limit > 0 else np.nan
                )
                base[f"touch_defer_{mode}"] = int(defer)

            # 触板后等 wait 根 K 挂涨停买单；只有开板才成交
            for w in (1, 2, 3):
                start = k - 1 + w
                fill = False
                entry = np.nan
                fill_bar = 0
                gap_fill = False
                if start < 4:
                    below = l[start:] < limit - EPS
                    if below.any():
                        j = start + int(np.argmax(below))
                        entry = min(float(o[j]), limit)
                        fill_bar = j + 1
                        gap_fill = bool(o[j] < limit - EPS)
                        fill = True
                base[f"fill_w{w}"] = int(fill)
                base[f"fill_bar_w{w}"] = fill_bar
                base[f"fill_delay_w{w}"] = (fill_bar - k) if fill else np.nan
                base[f"fill_gap_w{w}"] = int(gap_fill)
                base[f"entry_w{w}"] = entry
                for mode in ("open", "close"):
                    px, defer = _exit_price(r, mode)
                    ret = (px / entry - 1.0
                           if fill and np.isfinite(px) and px > 0 and entry > 0 else np.nan)
                    base[f"ret_fill_{mode}_gross_w{w}"] = ret
                    base[f"fill_defer_{mode}_w{w}"] = int(defer) if fill else 0
            rows.append(base)
        if rows:
            parts.append(pd.DataFrame(rows))
        if i % 500 == 0:
            print(f"  {i}/{valid['code'].nunique()} 只，已保留 {sum(len(x) for x in parts):,} 行")

    if not parts:
        raise SystemExit("[error] 没有可构建的事件")
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["date", "code"]).reset_index(drop=True)
    meta = {
        "source_features": str(features_path),
        "n_events": len(out),
        "n_codes": int(out["code"].nunique()),
        "n_days": int(out["date"].nunique()),
        "date_min": str(out["date"].min()),
        "date_max": str(out["date"].max()),
        "feature_columns": FEATURE_COLUMNS,
        "label_columns": LABEL_COLUMNS,
        "notes": [
            "features use only bars closed before the first touch",
            "sector/market breadth uses the prior closed 60-minute bar",
            "first_touch=1 falls back to previous trading day 15:00 breadth",
            "seal amount, L2 queue, and intraday sector limit-up count are not in history",
            "min60 cannot verify a 10:00 first-seal rule",
            "industry map is current-version and may differ historically",
        ],
    }
    return out, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", type=Path, default=FEATURES)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--meta", type=Path, default=META)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只，冒烟用")
    args = ap.parse_args()

    if not args.features.exists():
        print(f"[error] 缺少 {args.features}")
        return 1
    out, meta = build(args.features, limit=args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out, index=False)
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {args.out}  {len(out):,} 行 / {out['code'].nunique():,} 只 / "
          f"{out['date'].nunique()} 天")
    print(f"[ok] {args.meta}")
    exe = out[(out["open_limit"] == 0) & (out["is_st"] == 0) & (out["is_new"] == 0)]
    print(f"[check] 可执行触板 {len(exe):,}；收盘封板率 "
          f"{exe['y_sealed_close'].mean() * 100:.1f}%；"
          f"首触K=1 {int((exe['touch_bar'] == 1).sum()):,}；"
          f"wait1 可成交 {int(exe['fill_w1'].sum()):,} "
          f"({exe['fill_w1'].mean() * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
