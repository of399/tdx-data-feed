"""审计 data/parquet/daily/*.parquet 的 close 实际落盘口径（raw vs qfq）。

判定依据：data/adj_factor/{code}.parquet 同时存了 raw_close 与 qfq_close（按日对齐），
可直接与日K 文件的 stored close 比对，无需联网。

分类（对重叠日期计算中位数相对误差）：
  - err_raw = median(|stored - raw_close| / raw_close)
  - err_qfq = median(|stored - qfq_close| / qfq_close)
  - err_raw < 0.5%  → 'raw'   （含 raw==qfq 的无除权股，等价）
  - 否则 err_qfq < 0.5% → 'qfq'
  - 否则 → 'mixed/other'

分别给出「全历史」与「最近 30 个重叠交易日（predict 窗口）」两个口径，
后者决定 Step 5 选出的特征在线上 predict 时是否潜在漂移。

输出：stdout 汇总 + data/parquet/_audit_adjust_convention.csv（每文件一行）。
"""
from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import polars as pl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audit_adj")

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "data" / "parquet" / "daily"
ADJ = ROOT / "data" / "adj_factor"
OUT = DAILY / "_audit_adjust_convention.csv"
RECENT_N = 30
TOL = 0.005


def _code6(name: str) -> str:
    return "".join(ch for ch in name if ch.isdigit())


def _norm_date(df: pl.DataFrame) -> pl.DataFrame:
    if df["date"].dtype == pl.Date:
        return df
    # 旧文件可能是 int64 YYYYMMDD
    return df.with_columns(
        pl.col("date").cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d").alias("date")
    )


def main() -> int:
    daily_files = sorted(p for p in DAILY.glob("*.parquet") if ".bak" not in p.name)
    log.info("日K 文件数: %d", len(daily_files))

    # 预载 adj_factor（code6 -> df[date, raw_close, qfq_close]）
    adj_map: dict[str, pl.DataFrame] = {}
    for af in sorted(p for p in ADJ.glob("*.parquet") if ".bak" not in p.name):
        c = _code6(af.stem)
        try:
            d = pl.read_parquet(af, columns=["date", "raw_close", "qfq_close"])
            d = _norm_date(d).filter(pl.col("raw_close").is_not_null())
            adj_map[c] = d
        except Exception as e:  # pragma: no cover
            log.debug("adj 读取失败 %s: %s", af.name, e)
    log.info("adj_factor 载入: %d", len(adj_map))

    overall = Counter()
    recent = Counter()
    rows = []
    n_no_ref = 0
    n_skip = 0

    for i, fp in enumerate(daily_files):
        if (i + 1) % 1000 == 0:
            log.info("进度 %d/%d", i + 1, len(daily_files))
        c = _code6(fp.stem)
        adj = adj_map.get(c)
        if adj is None:
            n_no_ref += 1
            rows.append({"code": fp.stem, "has_adj": False, "overall": "no_ref", "recent": "no_ref"})
            continue
        try:
            df = pl.read_parquet(fp, columns=["date", "close"])
            df = _norm_date(df)
        except Exception as e:  # pragma: no cover
            log.debug("daily 读取失败 %s: %s", fp.name, e)
            n_skip += 1
            continue

        m = df.join(adj, on="date", how="inner")
        if m.height < RECENT_N:
            n_skip += 1
            rows.append({"code": fp.stem, "has_adj": True, "overall": "few_overlap", "recent": "few_overlap", "n": m.height})
            continue

        stored = m["close"].to_numpy().astype(float)
        raw = m["raw_close"].to_numpy().astype(float)
        qfq = m["qfq_close"].to_numpy().astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            err_raw = float(np.median(np.abs(stored - raw) / np.where(raw != 0, raw, np.nan)))
            err_qfq = float(np.median(np.abs(stored - qfq) / np.where(qfq != 0, qfq, np.nan)))

        ov = "raw" if err_raw < TOL else ("qfq" if err_qfq < TOL else "mixed/other")

        # 最近 N 个重叠日
        rm = m.tail(RECENT_N)
        rs = rm["close"].to_numpy().astype(float)
        rr = rm["raw_close"].to_numpy().astype(float)
        rq = rm["qfq_close"].to_numpy().astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            e_raw = float(np.median(np.abs(rs - rr) / np.where(rr != 0, rr, np.nan)))
            e_qfq = float(np.median(np.abs(rs - rq) / np.where(rq != 0, rq, np.nan)))
        rc = "raw" if e_raw < TOL else ("qfq" if e_qfq < TOL else "mixed/other")

        overall[ov] += 1
        recent[rc] += 1
        rows.append({
            "code": fp.stem, "has_adj": True, "overall": ov, "recent": rc,
            "n": m.height, "err_raw": round(err_raw, 5), "err_qfq": round(err_qfq, 5),
        })

    out = pl.DataFrame(rows)
    out.write_csv(OUT)
    print("\n==== 审计汇总 ====")
    print(f"日K 文件总数: {len(daily_files)}")
    print(f"无 adj 参照(ETF/指数/债券等): {n_no_ref}")
    print(f"重叠不足 {RECENT_N} 日/读取失败: {n_skip}")
    print(f"有参照文件数: {sum(overall.values())}")
    print("\n— 全历史口径 —")
    for k, v in overall.most_common():
        print(f"  {k:14s} {v}")
    print("\n— 最近 {RECENT_N} 重叠日口径（predict 窗口）—".replace("{RECENT_N}", str(RECENT_N)))
    for k, v in recent.most_common():
        print(f"  {k:14s} {v}")

    # 抽查 600519（已知 2026-10-09 sina raw ≈ 1263.00）
    spot = out.filter(pl.col("code") == "sh600519")
    if spot.height:
        print("\n抽查 sh600519:", spot.to_dicts()[0])
    print(f"\n明细已写出: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
