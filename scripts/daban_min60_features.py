#!/usr/bin/env python
"""构建 daban 打板/接力的 min60 盘中特征表（2025-09-05 ~ 2026-09-24）。

每个股票-交易日一行，来自 4 根 60 分钟 K（10:30/11:30/14:00/15:00）：
- 涨停价触板、首封、开板次数、回封情况
- 封板后成交量占比、首小时/尾盘量能、日内动量
- 三种「封板后等待 N 根 K 再挂单」的可成交入场模拟
以及日期级市场宽度（每根 K 收盘时全市场封板家数占比）。

用法::

    venv/bin/python scripts/daban_min60_features.py --workers 16
    venv/bin/python scripts/daban_min60_features.py --limit 50   # 冒烟
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.scanner import daily_path, load_universe  # noqa: E402

MIN60_DIR = ROOT / "data/parquet/min60"
DAILY_DIR = ROOT / "data/parquet/daily"
OUT = ROOT / "data/daban/min60_features_20261022.parquet"
OUT_MKT = ROOT / "data/daban/min60_market_20261022.parquet"
START, END = "20250905", "20260924"
BARS = ("1030", "1130", "1400", "1500")
EPS = 0.005


def _code_prefix(code: str) -> str:
    return "sh" if code[0] in "69" else "sz"


def _main_board(code: str) -> bool:
    return not code.startswith(("300", "301", "688", "689"))


def load_daily(code: str) -> pd.DataFrame | None:
    p = daily_path(code, DAILY_DIR)
    if p is None:
        return None
    try:
        d = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    except Exception:
        return None
    if d is None or d.empty:
        return None
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y%m%d")
    d = d.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)
    d["next_open"] = d["open"].shift(-1)
    d["next_close"] = d["close"].shift(-1)
    d["next2_open"] = d["open"].shift(-2)
    d["next_date"] = d["date"].shift(-1)
    d["avg_amount_5d"] = d["amount"].rolling(5, min_periods=1).mean()
    d["hist_n"] = np.arange(1, len(d) + 1)
    return d


def load_min60(code: str) -> pd.DataFrame | None:
    p = MIN60_DIR / f"{_code_prefix(code)}{code}.parquet"
    if not p.exists():
        return None
    try:
        m = pd.read_parquet(p)
    except Exception:
        return None
    if m is None or m.empty:
        return None
    dt = pd.to_datetime(m["datetime"])
    m = m.assign(date=dt.dt.strftime("%Y%m%d"), bar=dt.dt.strftime("%H%M"))
    m = m[m.date.between(START, END) & m.bar.isin(BARS)]
    if m.empty:
        return None
    return m


def _rowwise_fill(
    first_seal: np.ndarray,
    sealed: np.ndarray,
    low: np.ndarray,
    opens: np.ndarray,
    limit: np.ndarray,
    wait: int,
) -> dict[str, np.ndarray]:
    """封板后等 wait 根 K 再挂涨停买单，模拟能否成交及成交价。

    挂单从第 first_seal-1+wait 根 K 开始有效（first_seal 为 1-based）。
    该 K 开盘低于涨停价 → 以开盘价成交（买入限价单在更低价成交）；
    否则若盘中最低价跌破涨停价 → 以涨停价成交。
    """
    n, nb = low.shape
    idx = np.arange(nb)[None, :]
    start = first_seal - 1 + wait
    eligible = idx >= start[:, None]
    below = eligible & (low < limit[:, None] - EPS)
    has = below.any(axis=1) & (first_seal > 0)
    fi = np.where(has, below.argmax(axis=1), 0)
    rows = np.arange(n)
    fill_open = opens[rows, fi]
    entry = np.where(has, np.minimum(fill_open, limit), np.nan)
    gap = has & (fill_open < limit - EPS)
    cs_sealed = np.cumsum(sealed, axis=1)
    cs_reopen = np.cumsum(below, axis=1)
    prev_sealed = np.where(fi > 0, cs_sealed[rows, np.maximum(fi - 1, 0)], 0)
    prev_reopen = np.where(fi > 0, cs_reopen[rows, np.maximum(fi - 1, 0)], 0)
    return {
        f"fill_w{wait}": has,
        f"fill_bar_w{wait}": np.where(has, fi + 1, 0),
        f"entry_w{wait}": entry,
        f"gap_fill_w{wait}": gap,
        f"prior_sealed_w{wait}": prev_sealed,
        f"prior_reopen_w{wait}": prev_reopen,
    }


def build_one(code: str, name: str) -> pd.DataFrame | None:
    m = load_min60(code)
    d = load_daily(code)
    if m is None or d is None:
        return None

    is_st = int("ST" in str(name).upper() or "退" in str(name))
    main = _main_board(code)
    ratio_val = 0.05 if (is_st and main) else (0.10 if main else 0.20)
    prev_all = d.prev_close.to_numpy()
    limit_all = np.floor(np.where(np.isfinite(prev_all), prev_all, 0.0)
                         * (1.0 + ratio_val) * 100.0 + 0.5 + 1e-7) / 100.0
    hit = (np.isfinite(prev_all) & (prev_all > 0)
           & (d.close.to_numpy() >= limit_all - EPS))
    grp = (~hit).cumsum()
    d["cb"] = pd.Series(hit.astype(int), index=d.index).groupby(grp).cumsum()

    cnt = m.groupby("date")["bar"].agg(["size", "nunique"])
    good = cnt[(cnt["size"] == 4) & (cnt["nunique"] == 4)].index
    m = m[m.date.isin(good)]
    if m.empty:
        return None

    piv = {}
    for col in ("open", "high", "low", "close", "volume", "amount"):
        piv[col] = m.pivot(index="date", columns="bar", values=col).reindex(columns=BARS)
    j = pd.DataFrame(index=piv["open"].index)
    for k in range(4):
        j[f"o{k}"] = piv["open"].iloc[:, k].to_numpy()
        j[f"lo{k}"] = piv["low"].iloc[:, k].to_numpy()
        j[f"c{k}"] = piv["close"].iloc[:, k].to_numpy()
        j[f"v{k}"] = piv["volume"].iloc[:, k].to_numpy()

    dd = d[d.date.between(START, END)].set_index("date")
    j = j.join(dd[["prev_close", "open", "high", "low", "close", "volume", "amount",
                   "avg_amount_5d", "hist_n", "cb",
                   "next_open", "next_close", "next2_open", "next_date"]],
               how="inner")
    j = j.dropna(subset=["prev_close", "open", "high", "low", "close"])
    j = j[j.prev_close > 0]
    if j.empty:
        return None

    j["code"] = code
    j["is_st"] = is_st
    j["is_new"] = (j.hist_n <= 30).astype(int)
    j["consecutive_boards"] = j.cb.astype(int)

    ratio = np.full(len(j), ratio_val)
    prev = j.prev_close.to_numpy()
    limit = np.floor(prev * (1.0 + ratio) * 100.0 + 0.5 + 1e-7) / 100.0
    j["ratio"] = ratio
    j["limit"] = limit

    o = j[[f"o{k}" for k in range(4)]].to_numpy()
    lo = j[[f"lo{k}" for k in range(4)]].to_numpy()
    c = j[[f"c{k}" for k in range(4)]].to_numpy()
    v = j[[f"v{k}" for k in range(4)]].to_numpy()
    # 60min bar 的 high 单独从 pivot 取，用于定位触板发生在哪根 K。
    h = piv["high"].reindex(j.index).to_numpy()

    lim2 = limit[:, None]
    touch_bar = h >= lim2 - EPS
    sealed_bar = c >= lim2 - EPS
    touched = touch_bar.any(axis=1)
    sealed_close = sealed_bar[:, 3]
    open_limit = o[:, 0] >= limit - EPS
    first_touch = np.where(touched, touch_bar.argmax(axis=1) + 1, 0)
    any_seal = sealed_bar.any(axis=1)
    first_seal = np.where(any_seal, sealed_bar.argmax(axis=1) + 1, 0)
    n_sealed = sealed_bar.sum(axis=1)
    idx = np.arange(4)[None, :]
    after = idx >= first_seal[:, None]
    reopen_mask = after & (lo < lim2 - EPS)
    reopen_count = reopen_mask.sum(axis=1)
    first_reopen = np.where(reopen_count > 0, reopen_mask.argmax(axis=1) + 1, 0)

    day_vol = v.sum(axis=1)
    after_vol = np.where(day_vol > 0,
                         np.where(first_seal > 0,
                                  np.where(after, v, 0).sum(axis=1) / day_vol, 0.0), 0.0)
    after_bars = np.maximum(4 - first_seal, 0)
    sealed_after = np.where(first_seal > 0,
                            np.where(after, sealed_bar, 0).sum(axis=1)
                            / np.maximum(after_bars, 1), 1.0)

    j["touched"] = touched
    j["sealed_close"] = sealed_close
    j["open_limit"] = open_limit
    j["first_touch"] = first_touch
    j["first_seal"] = first_seal
    j["n_sealed_bars"] = n_sealed
    j["reopen_count"] = reopen_count
    j["first_reopen"] = first_reopen
    j["vol_after_seal_share"] = after_vol
    j["sealed_share_after"] = sealed_after
    j["last_bar_vol_share"] = np.where(day_vol > 0, v[:, 3] / day_vol, 0.0)
    j["first_hour_vol_share"] = np.where(day_vol > 0, v[:, 0] / day_vol, 0.0)
    j["first_hour_ret"] = c[:, 0] / prev - 1.0
    j["morning_ret"] = c[:, 1] / prev - 1.0
    j["last_hour_ret"] = c[:, 3] / np.maximum(c[:, 2], 1e-9) - 1.0
    j["open_gap"] = o[:, 0] / prev - 1.0
    j["amplitude"] = (h.max(axis=1) - lo.min(axis=1)) / prev
    j["touch_count"] = touch_bar.sum(axis=1)
    j["seal_strength"] = np.where(first_seal > 0,
                                  np.where(after, sealed_bar, 0).sum(axis=1)
                                  / np.maximum(4 - first_seal, 1), 0.0)

    # 日线 vs min60 对齐校验
    j["px_diff"] = np.maximum.reduce([
        np.abs(c[:, 3] - j.close.to_numpy()),
        np.abs(h.max(axis=1) - j.high.to_numpy()),
        np.abs(lo.min(axis=1) - j.low.to_numpy()),
    ])
    j["suspect_ref"] = ((j.open / j.prev_close - 1.0).abs() > j.ratio + 0.05) | \
                       (j.amplitude > 2.5 * j.ratio) | (j.px_diff > 0.02)

    for w in (1, 2, 3):
        out = _rowwise_fill(first_seal, sealed_bar, lo, o, limit, w)
        for k, vv in out.items():
            j[k] = vv

    # 下一日涨跌停价（用于买不进 / 卖不出判定）
    next_lim_up = np.floor(j.close.to_numpy() * (1.0 + ratio) * 100.0 + 0.5 + 1e-7) / 100.0
    next_lim_dn = np.floor(j.close.to_numpy() * (1.0 - ratio) * 100.0 + 0.5 + 1e-7) / 100.0
    j["next_lim_up"] = next_lim_up
    j["next_lim_dn"] = next_lim_dn

    keep = [
        "code", "prev_close", "ratio", "limit", "is_st", "is_new",
        "consecutive_boards", "amount", "avg_amount_5d", "suspect_ref", "px_diff",
        "touched", "sealed_close", "open_limit", "first_touch", "first_seal",
        "n_sealed_bars", "reopen_count", "first_reopen", "vol_after_seal_share",
        "sealed_share_after", "last_bar_vol_share", "first_hour_vol_share",
        "first_hour_ret", "morning_ret", "last_hour_ret", "open_gap", "amplitude",
        "touch_count", "seal_strength",
        "next_open", "next_close", "next2_open", "next_date",
        "next_lim_up", "next_lim_dn",
    ]
    for w in (1, 2, 3):
        keep += [f"fill_w{w}", f"fill_bar_w{w}", f"entry_w{w}", f"gap_fill_w{w}",
                 f"prior_sealed_w{w}", f"prior_reopen_w{w}"]
    keep += [f"o{k}" for k in range(4)]
    keep += [f"c{k}" for k in range(4)]
    out = j[keep].reset_index().rename(columns={"index": "date"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只，冒烟用")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    universe = load_universe()
    codes = sorted(universe.keys())
    if args.limit:
        codes = codes[: args.limit]
    print(f"[info] {len(codes)} 只股票，min60 {START}~{END}")

    parts: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, part in enumerate(
                pool.map(lambda c: build_one(c, universe.get(c, "")), codes), 1):
            if part is not None and not part.empty:
                parts.append(part)
            if i % 500 == 0:
                print(f"  {i}/{len(codes)}  已保留 {len(parts)} 只")
    if not parts:
        print("[error] 无数据")
        return 1
    df = pd.concat(parts, ignore_index=True)
    df.to_parquet(args.out, index=False)
    print(f"[ok] {args.out}  {len(df):,} 行 / {df.code.nunique()} 只 / {df.date.nunique()} 天")

    valid = df[~df.suspect_ref]
    print(f"[check] 价格对齐异常 {df.suspect_ref.mean() * 100:.2f}%  "
          f"（|close diff| 中位 {df.px_diff.median():.4f}，P99 {df.px_diff.quantile(.99):.4f}）")
    print(f"[check] 触板事件 {int(valid.touched.sum()):,}；收盘封板 "
          f"{int(valid.sealed_close.sum()):,}；开盘一字 {int(valid.open_limit.sum()):,}")
    for w in (1, 2, 3):
        print(f"[check] wait{w} 可成交 {int((valid[f'fill_w{w}'] & valid.touched).sum()):,}")

    if not args.limit:
        # 日期级市场宽度：每根 K 收盘时封板家数占比
        rows = []
        v = df[~df.suspect_ref]
        for k in range(4):
            g = v.groupby("date").agg(
                n=("code", "size"),
                sealed=(f"c{k}", lambda s: 0),  # placeholder replaced below
            )
            # sealed at bar k 需要与 limit 比较，单独算
            sealed = (v[f"c{k}"] >= v["limit"] - EPS)
            g = sealed.groupby(v["date"]).agg(["sum", "count"]).rename(
                columns={"sum": "sealed", "count": "n"})
            g["breadth"] = g["sealed"] / g["n"]
            g["bar"] = k + 1
            rows.append(g.reset_index())
        mkt = pd.concat(rows, ignore_index=True)
        mkt.to_parquet(OUT_MKT, index=False)
        print(f"[ok] {OUT_MKT}  {len(mkt):,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
