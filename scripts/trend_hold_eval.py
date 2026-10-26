"""趋势持续增长（价格趋势为主）信号的持有期表现评估。

和 `hengpan_t1_eval.py` 的分工：横盘突破测的是隔日博弈（T+1），这里测的是
"入池后持有 h 个交易日"的中长线口径，所以不看 T+1，看 h=20/60/120。

口径（沿用本仓库既有约定，不另起一套）
--------------------------------------
- **复权**：raw OHLC × `data/adj_factor/{code}.parquet` 的 `adj_factor`（前复权）。
  实测 `qfq_close == raw_close × adj_factor`（误差 ~1e-15），除权跳空被吸收
  （601088 在 2025-07-07 原始 −8.16% → 复权 −2.81%）。
  **不能用 `data/hfq_factor/` 的 `hfq_close`**：那份文件的 hfq_close 与未复权价逐日相等
  （factor 恒 1，实测 5 只票全样本如此），乘上去等于没复权。
  `data/parquet/daily/` 本身是未复权（`_audit_adjust_convention.csv` 判 raw/raw），
  用未复权价算 MA(C,250)/HHV(C,250) 会把除权缺口当成破位，趋势条件必然失真。
  因子表覆盖 2020-01-02~2026-09-24，之后按 1.0 顺延；若 9-24 后又除权，尾部收益略有偏差。
- **成本**：复用 `execution_backtest.net_return` + `ExecutionConfig` 缺省
  （双边佣金 2.5bp + 印花税 5bp + 卖出滑点 5bp ≈ 15bp 一个来回）。
- **可买性**：T 日收盘涨停视为买不进，信号与基线**同样剔除**。
- **持有期终点**：按该股自身行序往后数 h 行（停牌日在日线里本就没有行，所以停牌票的
  "h 个交易日"可能跨更长自然日）；不足 h 行的信号丢弃。
- **独立性**：重叠持有期会把 t 虚高，另给一个每 h 个交易日取一次仓的**不重叠**子集 t。
- **阈值不是拍的**：平滑度 CV=STD(C,120)/MA(C,120) 的切点取全市场股票-日分布的 P25/P50
  （先跑一遍 300 只随机样本标定），避免"0.18"这种无来源的数。

用法::

    venv/bin/python scripts/trend_hold_eval.py
    venv/bin/python scripts/trend_hold_eval.py --calib-sample 300 --start 2021-01-04

产物：``data/daban/trend_hold_eval_YYYYMMDD.json``
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.execution_backtest import ExecutionConfig, net_return  # noqa: E402
from tdxfeed.daban.limit_price import (  # noqa: E402
    exchange_prefix,
    is_st,
    limit_ratio,
    limit_up_price,
)

DAILY_DIR = _ROOT / "data" / "parquet" / "daily"
ADJ_DIR = _ROOT / "data" / "adj_factor"
UNIVERSE_PATH = _ROOT / "data" / "all_a_stocks.json"
OUT_DIR = _ROOT / "data" / "daban"
INDEX_CODE = "sh000001"      # 上证指数，未复权但对指数无除权问题，仅作 beta 参照

MIN_BARS = 260
AMT_FLOOR = 5e7             # 20 日均成交额下限（元）：流动性闸门，替代流通市值条件
HORIZONS = (20, 60, 120)
NEAR_HIGH = 0.95            # 距一年高点不超过 5%
PULL_BACK = 0.03            # 回踩 60 日线 ±3%


def _limit_table(df: pd.DataFrame, code: str, name: str) -> pd.Series:
    """涨停价（未复权口径）。ST 票整只剔除，故本票比例恒定，不需逐日传 on_date。

    按去重后的前收盘查表，保证与 `limit_up_price` 的 ROUND_HALF_UP 到分完全一致
    （np.round 是银行家舍入，会差一分，一分就够把封板判错）。
    """
    ratio = limit_ratio(code, name)
    uniq = df["raw_prev_c"].dropna().unique()
    return df["raw_prev_c"].map({float(v): limit_up_price(float(v), ratio) for v in uniq})


def _load_one(code: str, name: str) -> pd.DataFrame | None:
    """单只票：未复权日线 × 前复权因子 → 复权趋势条件 + 三个持有期的净收益。"""
    fp = DAILY_DIR / f"{exchange_prefix(code)}{code}.parquet"
    if fp.exists() is False or is_st(name):
        return None
    raw = pd.read_parquet(fp, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    if len(raw) < MIN_BARS + max(HORIZONS) + 5:
        return None

    factor = pd.Series(1.0, index=raw.index)
    ap = ADJ_DIR / f"{code}.parquet"
    if ap.exists():
        adj = pd.read_parquet(ap, columns=["date", "adj_factor"])
        factor = raw["date"].map(adj.drop_duplicates("date").set_index("date")["adj_factor"]).astype(float)
        factor = factor.ffill().bfill().fillna(1.0)

    c = raw["close"] * factor
    df = pd.DataFrame({
        "date": pd.to_datetime(raw["date"]),
        "c": c,
        "h": raw["high"] * factor,
        "prev_c": c.shift(1),
        "raw_c": raw["close"],
        "raw_prev_c": raw["close"].shift(1),
        "v": raw["volume"],
        "amt20": raw["amount"].rolling(20).mean(),
    })
    ma60 = c.rolling(60).mean()
    ma120 = c.rolling(120).mean()
    ma250 = c.rolling(250).mean()
    df["ma60"] = ma60
    df["cv"] = c.rolling(120).std() / ma120          # 波动/中枢：越小越"稳步"
    df["up250"] = ma250 > ma250.shift(20)            # 年线自身向上
    df["stack"] = (c > ma60) & (ma60 > ma250)        # 多头排列
    hhv250 = df["h"].rolling(250).max()
    df["near_high"] = c >= hhv250 * NEAR_HIGH
    df["new_high"] = c >= hhv250 - 1e-9
    df["at_ma60"] = (c / ma60 - 1).abs() <= PULL_BACK
    df["up_day"] = c > df["prev_c"]
    df["shrink"] = df["v"] < df["v"].rolling(20).mean()

    lp = _limit_table(df, code, name)
    df["sealed"] = (df["raw_c"] >= lp - 0.004) & (lp > 0)
    df["gate"] = (df["amt20"] >= AMT_FLOOR) & df["cv"].notna() & (df["cv"] < 9)
    df["tradable"] = df["gate"] & ~df["sealed"]

    pos = np.arange(len(df))
    entry = df["c"].to_numpy()
    cfg = ExecutionConfig()
    for h in HORIZONS:
        idx = pos + h
        ok = (idx < len(df)) & np.isfinite(idx)
        exitc = np.full(len(df), np.nan)
        valid_idx = idx[ok]
        exitc[ok] = df["c"].to_numpy()[valid_idx]
        tradable = df["tradable"].to_numpy() & ok & (exitc > 0)
        net = np.full(len(df), np.nan)
        a, b = entry[tradable], exitc[tradable]
        net[tradable] = [net_return(float(x), float(y), cfg) for x, y in zip(a, b, strict=True)]
        df[f"net{h}"] = net
    df["code"] = code
    return df


def _variants(cv_hi: float, cv_mid: float) -> dict[str, str]:
    """条件表达式，和交付的通达信公式一一对应。"""
    lift = "tradable & up250 & stack"
    return {
        "G1_年线向上多头排列": lift,
        "G2_G1+距一年高5%": f"{lift} & near_high",
        "G3_G2+平滑P25": f"{lift} & near_high & (cv <= {cv_hi})",
        "G4_G2+平滑P50": f"{lift} & near_high & (cv <= {cv_mid})",
        "G5_G1+平滑P25(不要求新高)": f"{lift} & (cv <= {cv_hi})",
        "G6_创新高当日+G1": f"{lift} & new_high",
        "G7_回踩60日线企稳": "tradable & up250 & at_ma60 & up_day & shrink",
    }


def _collect(codes_names: list[tuple[str, str]], start: pd.Timestamp, end: pd.Timestamp):
    """逐票计算，累加信号行与基线行（只留窗口内、列尽量窄）。"""
    keep = ["date", "code", "cv", "tradable", "up250", "stack", "near_high", "new_high",
            "at_ma60", "up_day", "shrink"] + [f"net{h}" for h in HORIZONS]
    sig_pool, base_pool, cv_pool = [], [], []
    for i, (code, name) in enumerate(codes_names, 1):
        df = _load_one(code, str(name))
        if df is None:
            continue
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        if not len(df):
            continue
        sub = df[keep]
        base_pool.append(sub[sub["tradable"]])
        cv_pool.append(df.loc[df["tradable"], "cv"].to_numpy())
        sig_pool.append(sub)
        if i % 800 == 0:
            print(f"  已处理 {i}/{len(codes_names)}", flush=True)
    return pd.concat(sig_pool, ignore_index=True), pd.concat(base_pool, ignore_index=True), np.concatenate(cv_pool)


def _summarize(panel: pd.DataFrame, base: pd.DataFrame, exprs: dict[str, str]) -> list[dict]:
    rows = []
    for h in HORIZONS:
        col = f"net{h}"
        b = base.dropna(subset=[col])
        base_daily = b.groupby("date")[col].mean()
        base_mean = float(b[col].mean()) * 100
        days = sorted(base["date"].unique())
        n_window_days = len(days)          # 日均命中数按整个窗口的交易日算，不是按"有票的日子"算
        for vname, expr in exprs.items():
            g = panel.dropna(subset=[col]).query(expr)
            if not len(g):
                rows.append({"variant": vname, "horizon_days": h, "signals": 0})
                continue
            daily = g.groupby("date")[col].mean().rename("sig")
            j = pd.concat([daily, base_daily.rename("base")], axis=1, join="inner")
            ex = j["sig"] - j["base"]
            stride = set(days[::h])
            jn = j.loc[[d for d in j.index if d in stride]]
            xn = jn["sig"] - jn["base"]

            def t_of(s: pd.Series) -> float | None:
                if len(s) <= 2:
                    return None
                sd = float(s.std(ddof=1))
                if not np.isfinite(sd) or sd == 0:
                    return None
                return round(float(s.mean() / (sd / np.sqrt(len(s)))), 2)

            # 分年稳定性：均值正但某一年扛全部，就是没稳定性
            yr = g.assign(year=g["date"].dt.year).groupby("year")[col].mean()
            yrb = base.assign(year=base["date"].dt.year).groupby("year")[col].mean()
            by_year = {int(k): round(float(v - yrb.get(k, np.nan)) * 100, 2)
                       for k, v in yr.items() if np.isfinite(yrb.get(k, np.nan))}
            # 尾部贡献：去掉最好的 5% 之后还剩多少
            cut = float(g[col].quantile(0.95))
            trimmed = float(g.loc[g[col] <= cut, col].mean()) * 100

            rows.append({
                "variant": vname,
                "horizon_days": h,
                "signals": len(g),
                "trading_days": len(j),
                "avg_per_day": round(len(g) / max(n_window_days, 1), 1),
                "sig_mean_pct": round(float(g[col].mean()) * 100, 3),
                "sig_median_pct": round(float(g[col].median()) * 100, 3),
                "sig_win_pct": round(float((g[col] > 0).mean()) * 100, 1),
                "base_mean_pct": round(base_mean, 3),
                "daily_excess_mean_pct": round(float(ex.mean()) * 100, 3),
                "daily_excess_t": t_of(ex),
                "nonoverlap_n": len(xn),
                "nonoverlap_excess_mean_pct": round(float(xn.mean()) * 100, 3) if len(xn) > 2 else None,
                "nonoverlap_excess_t": t_of(xn),
                "win_days_pct": round(float((ex > 0).mean()) * 100, 1),
                "distinct_stocks": int(g["code"].nunique()),
                "mean_excl_top5pct_pct": round(trimmed, 3),
                "excess_by_year_pct": by_year,
            })
    return rows


def _index_bench(start: pd.Timestamp, end: pd.Timestamp) -> dict:
    fp = DAILY_DIR / f"{INDEX_CODE}.parquet"
    if not fp.exists():
        return {}
    d = pd.read_parquet(fp, columns=["date", "close"])
    d["date"] = pd.to_datetime(d["date"])
    c = d["close"].to_numpy()
    dates = d["date"].to_numpy()
    out = {}
    for h in HORIZONS:
        r = (c[h:] / c[:-h] - 1.0) * 100
        m = (dates[h:] >= np.datetime64(start)) & (dates[h:] <= np.datetime64(end))
        out[f"idx_net{h}_pct"] = round(float(r[m].mean()), 3) if m.any() else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="趋势信号持有期评估")
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default="2026-07-15")
    ap.add_argument("--calib-sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261026)
    args = ap.parse_args()

    universe = list(json.loads(UNIVERSE_PATH.read_text()).items())
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    rng = random.Random(args.seed)
    calib = rng.sample(universe, min(args.calib_sample, len(universe)))
    print(f"标定：{len(calib)} 只随机样本，窗口 {args.start}~{args.end}，"
          f"成本 {ExecutionConfig().to_dict()}")
    _, _, cv_all = _collect(calib, start, end)
    cv_hi, cv_mid = float(np.percentile(cv_all, 25)), float(np.percentile(cv_all, 50))
    print(f"平滑度 CV=STD(C,120)/MA(C,120) 分布：P10={np.percentile(cv_all,10):.4f} "
          f"P25={cv_hi:.4f} P50={cv_mid:.4f} P75={np.percentile(cv_all,75):.4f}")

    print("全量：")
    panel, base, cv_all2 = _collect(universe, start, end)
    exprs = _variants(cv_hi, cv_mid)
    print(f"面板 {len(panel)} 行 / 可交易日 {len(base)} 行 / 覆盖 {panel['code'].nunique()} 只")
    rows = _summarize(panel, base, exprs)
    idx = _index_bench(start, end)
    cols = ["variant", "horizon_days", "signals", "avg_per_day", "sig_mean_pct", "sig_median_pct",
            "sig_win_pct", "base_mean_pct", "daily_excess_mean_pct", "daily_excess_t",
            "nonoverlap_n", "nonoverlap_excess_mean_pct", "nonoverlap_excess_t", "win_days_pct"]
    table = pd.DataFrame(rows).reindex(columns=cols)
    print(table.to_string(index=False))
    for vname in ("G6_创新高当日+G1", "G3_G2+平滑P25", "G1_年线向上多头排列"):
        for h in (60, 120):
            r = next((x for x in rows if x["variant"] == vname and x["horizon_days"] == h), None)
            if r:
                print(f"{vname} h={h}：分年超额(%) {r['excess_by_year_pct']} "
                      f"去掉最好5%后均值 {r['mean_excl_top5pct_pct']}% "
                      f"覆盖 {r['distinct_stocks']} 只")
    print(f"\n上证指数同口径均值(%)：{idx}")

    out = OUT_DIR / f"trend_hold_eval_{datetime.now():%Y%m%d}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": [args.start, args.end],
        "adjust": "raw × data/adj_factor.adj_factor（前复权）；hfq_factor 目录的 hfq_close 未复权，不可用",
        "execution": ExecutionConfig().to_dict(),
        "gates": {"min_bars": MIN_BARS, "amt20_floor": AMT_FLOOR, "exclude_st": True,
                  "exclude_sealed_close": True, "near_high_ratio": NEAR_HIGH,
                  "pullback_band": PULL_BACK},
        "cv_calibration": {"sample": len(calib), "p25": cv_hi, "p50": cv_mid,
                           "full_market_p25": float(np.percentile(cv_all2, 25)),
                           "full_market_p50": float(np.percentile(cv_all2, 50))},
        "variant_exprs": exprs,
        "index_bench": idx,
        "panel_rows": len(panel),
        "tradable_rows": len(base),
        "results": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
