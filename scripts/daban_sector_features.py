#!/usr/bin/env python
"""板块特征历史化（M2 里程碑）：把 §A.1 的 Sec 因子重建到历史，供 predict.py 训练使用。

**为什么只做 Sec 不做封单比**：封单量/封单额**没有历史数据源**（只在实时接口
提供），无法进入训练特征；而板块强度完全可以用日线重建 —— 昨日的板块涨幅、
涨停家数、成分数、涨幅排名都是日频可观测量。

输出：
    data/factors/sector_features.csv   date,industry_code,sector_pct,limit_up_n,
                                       member_n,rank_pct,sec
    （另存 parquet 同名 .parquet 便于 predict.py 直接读）

口径与实时 Sec 完全一致（``tdxfeed/daban/sector.py``）：
    sec = 40×rank_pct + 40×min(limit_up_n,10)/10 + 20×min(leader_pct,10)/10
其中 leader_pct = 板块内当日最大涨幅。

用法::

    venv/bin/python scripts/daban_sector_features.py --start 20200101
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from _common import daily_dir  # noqa: E402

from tdxfeed.daban.limit_price import (  # noqa: E402
    exchange_prefix,
    limit_ratio,
    limit_up_price,
)
from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402

DAILY_DIR = daily_dir()
OUT_DIR = _ROOT / "data" / "factors"
COLS = ["date", "open", "high", "low", "close"]


def load_one(code: str) -> tuple[str, pd.DataFrame] | None:
    # 日线文件名带交易所前缀（sh600000.parquet），行业映射里是 6 位代码
    p = DAILY_DIR / f"{exchange_prefix(code)}{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=COLS)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
    return code, df.sort_values("date")


def build(start: str, end: str, workers: int = 12) -> pd.DataFrame:
    industry_map = load_industry_map()
    codes = [c for c in industry_map if industry_map[c]]
    print(f"[info] 股票池 {len(codes)} 只（有行业映射）")

    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(load_one, codes):
            if item is None:
                continue
            code, df = item
            df = df[(df["date"] >= start) & (df["date"] <= end)]
            if len(df) < 2:
                continue
            ratio = limit_ratio(code, "")
            prev = df["close"].shift(1)
            lp = [limit_up_price(float(p), ratio) if p == p and p > 0 else np.nan for p in prev]
            df = df.assign(
                pct=(df["close"] / prev - 1.0) * 100.0,
                is_limit_up=[bool(c >= l - 0.005) if l == l else False for c, l in zip(df["close"], lp, strict=True)],
                industry_code=industry_map[code],
            )
            frames.append(df[["date", "industry_code", "pct", "is_limit_up"]])

    if not frames:
        return pd.DataFrame()
    long = pd.concat(frames, ignore_index=True)
    long["pct"] = long["pct"].astype("float32")
    long["is_limit_up"] = long["is_limit_up"].astype("int8")
    long["industry_code"] = long["industry_code"].astype("category")
    print(f"[info] 长表 {len(long):,} 行 / {long['date'].nunique()} 个交易日")

    grp = long.groupby(["date", "industry_code"], observed=True)
    agg = grp.agg(
        sector_pct=("pct", "median"),
        limit_up_n=("is_limit_up", "sum"),
        member_n=("pct", "size"),
        leader_pct=("pct", "max"),
    ).reset_index()
    agg = agg[agg["member_n"] >= 3].copy()

    # 每日内的板块涨幅排名百分位
    agg["rank_pct"] = agg.groupby("date")["sector_pct"].rank(pct=True, method="average")
    agg["sec"] = (
        40 * agg["rank_pct"]
        + 40 * agg["limit_up_n"].clip(upper=10) / 10
        + 20 * agg["leader_pct"].clip(lower=0, upper=10) / 10
    )
    agg["date"] = agg["date"].astype(str)
    return agg.sort_values(["date", "industry_code"]).reset_index(drop=True)


def main() -> int:
    p = argparse.ArgumentParser(description="板块特征历史化")
    p.add_argument("--start", default="20200101")
    p.add_argument("--end", default="99999999")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--out-dir", default=str(OUT_DIR))
    args = p.parse_args()

    df = build(args.start, args.end, args.workers)
    if df.empty:
        print("[warn] 无数据")
        return 1
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "sector_features.csv"
    pq_path = out / "sector_features.parquet"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_parquet(pq_path, index=False)
    print(f"[ok] {len(df):,} 行 × {df['date'].nunique()} 日 → {csv_path} / {pq_path}")
    print(df.tail(5).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
