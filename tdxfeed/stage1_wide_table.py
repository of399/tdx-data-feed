"""阶段 1 — Step 3 宽表装配。

把训练宇宙（data/double_up/d_signal_5d_features_vpnl.csv，含 code/signal_date/
is_positive/hypothetical_pnl + 26 维）扩展为 Optuna 可用的宽表：
  - 26 维基础特征：用 factors_polars 逐位复刻 features_5d（与 CSV 列数值一致，可校验 parity）。
  - 9 维候选因子：factors_candidates（RSI/MACD/ret_skew/vol_term/gap_t1/vp_corr/
    amihud/rsrs/intraday_rev），由 Optuna 决策纳入/排除。
  - 标签：is_positive（分类对照）、hypothetical_pnl（回归主目标，对齐 vpnl_huber）。

输出：data/double_up/stage1_wide_table.parquet（每 (code, signal_date) 一行）。

复权口径：day parquet 的 close 已审计为未复权（raw），与 features_5d/训练一致，零漂移。
"""
from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

import polars as pl

from .factors_candidates import CANDIDATE_NAMES, compute_candidate_features
from .factors_polars import compute_window_features_extended_polars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stage1_wide_table")

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "data" / "parquet" / "daily"
SRC_CSV = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
OUT_PARQUET = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"

FEAT26_ORDER = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem", "acceleration", "range_efficiency", "vol_norm_ret",
    "amount_surge_3d", "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low",
    "amt_per_price",
]


def _norm_date(df: pl.DataFrame) -> pl.DataFrame:
    if df["date"].dtype == pl.Date:
        return df
    return df.with_columns(
        pl.col("date").cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d").alias("date")
    )


def _str_to_date(s: str) -> date:
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def build_wide_table(
    src_csv: Path = SRC_CSV,
    out_parquet: Path = OUT_PARQUET,
    sample_codes: int | None = None,
    verify_parity: bool = True,
) -> dict:
    """装配宽表。按 code 分组，每股票读一次 parquet，对其每个 signal_date 计算特征。"""
    base = pl.read_csv(src_csv, infer_schema_length=10000)
    # signal_date 统一为字符串，避免被推断为 Date 导致后续 == 比较/解析歧义
    base = base.with_columns(pl.col("signal_date").cast(pl.Utf8))
    codes = base.get_column("code").unique().to_list()
    if sample_codes:
        codes = codes[:sample_codes]
    log.info("宇宙 code 数: %d（采样 %s）", len(codes), sample_codes)

    records: list[dict] = []
    parity_max = 0.0
    worst = (0.0, "", "", "")
    n_ok = 0
    n_missing_file = 0
    n_missing_date = 0
    n_compute_fail = 0
    n_nonfinite = 0
    n_asym_nonfinite = 0
    n_diff_big = 0
    n_diff_rel = 0

    for code in codes:
        fp = DAILY / f"{code}.parquet"
        if not fp.exists():
            n_missing_file += int((base.get_column("code") == code).sum())
            continue
        df = _norm_date(pl.read_parquet(fp))
        dates = df["date"].to_list()
        sub = base.filter(pl.col("code") == code)
        for sd, is_pos, pnl in zip(
            sub["signal_date"].to_list(),
            sub["is_positive"].to_list(),
            sub["hypothetical_pnl"].to_list(),
            strict=True,
        ):
            d = _str_to_date(sd)
            try:
                sig_idx = dates.index(d)
            except ValueError:
                n_missing_date += 1
                continue
            # 训练约定：find_d_signal_5d.extract_features 在 sig_i-1（信号日前一天=T-1）算特征，
            # 5 日窗口 = [sig_i-5, sig_i-1]。故锚点取 sig_idx-1；需 >=4 以满足 5 日窗口。
            anchor = sig_idx - 1
            if anchor < 4:
                n_missing_date += 1
                continue
            try:
                f26 = compute_window_features_extended_polars(df, code, sig_idx=anchor)
                if f26 is None:
                    n_missing_date += 1
                    continue
                cands = compute_candidate_features(df, code, sig_idx=anchor, lookback=60)
                if cands is None:  # 极端短历史
                    cands = {k: 0.0 for k in CANDIDATE_NAMES}
            except (ZeroDivisionError, ValueError, IndexError) as e:
                # 数据异常（如历史窗口含 0 close）导致复刻公式除零；原 features_5d 生成时
                # 数据正常故未触发，现 parquet 该窗口已损坏 → 跳过该行（不影响 Optuna 主体）
                n_compute_fail += 1
                log.debug("跳过异常行 %s %s: %s", code, sd, e)
                continue

            rec = {
                "code": code,
                "signal_date": sd,
                "is_positive": is_pos,
                "hypothetical_pnl": pnl,
            }
            for k in FEAT26_ORDER:
                rec[k] = float(f26[k])
            for k in CANDIDATE_NAMES:
                rec[k] = float(cands[k])

            if verify_parity:
                for k in FEAT26_ORDER:
                    row = sub.filter(pl.col("signal_date") == sd)
                    if not row.height:
                        continue
                    csv_v = row[k][0]
                    rv = rec[k]
                    if csv_v is None or rv is None:
                        n_nonfinite += 1
                        continue
                    csv_f, rv_f = float(csv_v), float(rv)
                    if not (math.isfinite(csv_f) and math.isfinite(rv_f)):
                        n_nonfinite += 1
                        if math.isfinite(csv_f) != math.isfinite(rv_f):
                            n_asym_nonfinite += 1
                        continue
                    parity_max = max(parity_max, abs(rv_f - csv_f))
                    if abs(rv_f - csv_f) > worst[0]:
                        worst = (abs(rv_f - csv_f), k, code, sd)
                    if abs(rv_f - csv_f) > 1.0:
                        n_diff_big += 1
                    if abs(rv_f - csv_f) / (abs(csv_f) + 1.0) > 1e-3:
                        n_diff_rel += 1

            records.append(rec)
            n_ok += 1

    out = pl.DataFrame(records)
    out.write_parquet(out_parquet)
    log.warning("最差 parity 项: diff=%.4e feature=%s code=%s signal_date=%s", *worst)
    log.info(
        "完成: n_ok=%d 缺失文件=%d 缺失日期=%d 计算失败=%d parity_max=%.3e",
        n_ok, n_missing_file, n_missing_date, n_compute_fail, parity_max,
    )
    print(f"\n宽表写出: {out_parquet}  rows={out.height}")
    print(f"  26维 parity_max(有限值对) = {parity_max:.3e}")
    print(f"  缺失文件={n_missing_file}  缺失日期={n_missing_date}  计算失败(数据异常跳过)={n_compute_fail}")
    print(f"  非有限单元={n_nonfinite}  其中与CSV不一致(疑似bug)={n_asym_nonfinite}")
    print(f"  特征漂移: 绝对差>1 的单元={n_diff_big}  相对差>1e-3 的单元={n_diff_rel}")
    return {
        "n_ok": n_ok, "n_missing_file": n_missing_file,
        "n_missing_date": n_missing_date, "n_compute_fail": n_compute_fail,
        "n_nonfinite": n_nonfinite, "n_asym_nonfinite": n_asym_nonfinite,
        "n_diff_big": n_diff_big, "n_diff_rel": n_diff_rel,
        "parity_max": parity_max, "out": str(out_parquet),
    }


if __name__ == "__main__":
    # 默认小采样快速验证；全量去掉 --sample
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--src", type=str, default=str(SRC_CSV))
    ap.add_argument("--out", type=str, default=str(OUT_PARQUET))
    ap.add_argument("--no-parity", action="store_true")
    a = ap.parse_args()
    build_wide_table(
        src_csv=Path(a.src), out_parquet=Path(a.out),
        sample_codes=a.sample, verify_parity=not a.no_parity,
    )
