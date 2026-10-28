"""Step 2.5 — 训练数据一致性校验闸门（不盲目重写）。

背景：阶段 1 宽表装配曾误用 signal_date 锚点（应为 T-1=sig_idx-1），把"特征偏移一天"误判成
"训练 CSV 陈旧/60% 漂移"。本脚本在**正确锚点 T-1** 下，用当前 parquet 重算 26 维特征，
与 data/double_up/d_signal_5d_features_vpnl.csv 逐单元比对，输出：
  - 总体 parity_max（有限值对）
  - 逐特征相对差>1e-3 的单元数（定位是否真有漂移）
  - 非有限不一致单元数（疑似 bug 信号）
  - 结论：漂移是否超过阈值（默认 0.5%）→ 决定是否需真正重生成训练数据

用法：
    venv/bin/python scripts/verify_training_data_parity.py [--src CSV] [--tol 0.005] [--sample N]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _common import daily_dir  # noqa: E402

DAILY = daily_dir()
SRC = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
FEAT26 = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem", "acceleration", "range_efficiency", "vol_norm_ret",
    "amount_surge_3d", "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low",
    "amt_per_price",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify_training_parity")


def _norm_date(df: pl.DataFrame) -> pl.DataFrame:
    if df["date"].dtype == pl.Date:
        return df
    return df.with_columns(pl.col("date").cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d").alias("date"))


def _str_to_date(s: str) -> date:
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--tol", type=float, default=0.005, help="相对漂移阈值，超过则判定需重生成")
    ap.add_argument("--sample", type=int, default=0, help="0=全量")
    a = ap.parse_args()

    from tdxfeed.factors_polars import compute_window_features_extended_polars as fp

    base = pl.read_csv(a.src, infer_schema_length=10000).with_columns(
        pl.col("signal_date").cast(pl.Utf8)
    )
    codes = base.get_column("code").unique().to_list()
    if a.sample:
        codes = codes[: a.sample]

    feats_total = {k: 0 for k in FEAT26}          # 有限值对总数
    feats_drift = {k: 0 for k in FEAT26}          # 相对差 > tol 的单元
    feats_abs_big = {k: 0 for k in FEAT26}        # 绝对差 > 1 的单元
    asym_nonfinite = 0
    parity_max = 0.0
    n_ok = 0

    for code in codes:
        fp_parquet = DAILY / f"{code}.parquet"
        if not fp_parquet.exists():
            continue
        df = _norm_date(pl.read_parquet(fp_parquet))
        dates = df["date"].to_list()
        sub = base.filter(pl.col("code") == code)
        for sd, in zip(sub["signal_date"].to_list(), strict=True):
            d = _str_to_date(sd)
            try:
                sig_idx = dates.index(d)
            except ValueError:
                continue
            anchor = sig_idx - 1  # T-1 锚点（训练约定）
            if anchor < 4:
                continue
            try:
                f = fp(df, code, sig_idx=anchor)
            except Exception:
                continue
            if f is None:
                continue
            row = sub.filter(pl.col("signal_date") == sd)
            if not row.height:
                continue
            for k in FEAT26:
                csv_v = row[k][0]
                rv = f[k]
                if csv_v is None or rv is None:
                    asym_nonfinite += 1
                    continue
                csv_f, rv_f = float(csv_v), float(rv)
                if not (math.isfinite(csv_f) and math.isfinite(rv_f)):
                    if math.isfinite(csv_f) != math.isfinite(rv_f):
                        asym_nonfinite += 1
                    continue
                feats_total[k] += 1
                diff = abs(rv_f - csv_f)
                parity_max = max(parity_max, diff)
                if diff / (abs(csv_f) + 1.0) > a.tol:
                    feats_drift[k] += 1
                if diff > 1.0:
                    feats_abs_big[k] += 1
            n_ok += 1

    total_cells = sum(feats_total.values())
    total_drift = sum(feats_drift.values())
    print("\n========== 训练数据一致性校验（锚点 T-1）==========")
    print(f"比对样本(行): {n_ok}   有限值特征单元总数: {total_cells}")
    print(f"parity_max(有限值对): {parity_max:.4e}")
    print(f"非有限不一致单元(疑似bug): {asym_nonfinite}")
    print(f"相对漂移> {a.tol:.1%} 单元: {total_drift} ({total_drift / max(total_cells,1):.3%})")
    print("\n--- 逐特征漂移分解（相对>{tol:.1%} 单元数 / 绝对>1 单元数 / 有限单元数）---".replace("{tol:.1%}", f"{a.tol:.1%}"))
    for k in FEAT26:
        print(f"  {k:14s} drift={feats_drift[k]:5d}  abs>1={feats_abs_big[k]:4d}  n={feats_total[k]}")

    effective = total_drift + asym_nonfinite
    frac = effective / max(total_cells, 1)
    verdict = frac <= a.tol
    print("\n========== 结论 ==========")
    if verdict:
        print(f"✅ 训练 CSV 与当前 parquet 一致（有效漂移 {frac:.3%} ≤ 阈值 {a.tol:.1%}；"
              f"其中相对>{a.tol:.1%} {total_drift} 单元 + 非有限不一致 {asym_nonfinite} 单元），无需重生成。")
        return 0
    print(f"⚠️ 检测到超出阈值的漂移（{frac:.3%} > {a.tol:.1%}）→ 需执行真正重生成（见 scripts/regenerate_training_data.py）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
