"""阶段 1 — Step 3 宽表装配。

把训练宇宙（data/double_up/d_signal_5d_features_vpnl.csv，含 code/signal_date/
is_positive/hypothetical_pnl + 26 维）扩展为 Optuna 可用的宽表：
  - 26 维基础特征：用 factors_polars 逐位复刻 features_5d（与 CSV 列数值一致，可校验 parity）。
  - 9 维候选因子：factors_candidates（RSI/MACD/ret_skew/vol_term/gap_t1/vp_corr/
    amihud/rsrs/intraday_rev），由 Optuna 决策纳入/排除。
  - 标签：is_positive（分类对照）、hypothetical_pnl（回归主目标，对齐 vpnl_huber）。

输出：data/double_up/stage1_wide_table.parquet（每 (code, signal_date) 一行）。

复权口径：price_basis=raw（默认）时特征与标签都是 raw；price_basis=hfq 时
26 维按 features_5d 的逐维分派（13 hfq / 8 raw / 4 量纲 / 4 枚举）从 hfq 帧算，
标签来自 src CSV（即 d_signal_5d_features_vpnl_hfq.csv 的 hfq 标签）。
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import polars as pl

from .adjusted import MIN_FACTOR_COV, frame as adj_frame
from .alpha158.eval import evaluate_frame, load_payload
from .factors_candidates import CANDIDATE_NAMES, compute_candidate_features
from .factors_polars import compute_window_features_extended_polars
from .paths import daily_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stage1_wide_table")

ROOT = Path(__file__).resolve().parent.parent
DAILY = daily_dir()
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


def resolve_alpha158_cols(spec: str) -> list[str]:
    """把「要哪些 Alpha158 列」这句话解析成列名清单。

    三种写法，一个入口：`"all"` 取工件全集；逗号串取显式名单；指到 union 工件
    （`config/alpha158_union.json`）取它的 `alpha158_net_new`。之所以不许调用方自己
    `json.load(...)[\"union\"]`：判据视图的名单是导出来的，抄一次就会在代码里留下第二套
    口径，而面板重跑后它不会报错，只会悄悄不是同一批列。守卫
    `tests/test_stage1_wide_table_alpha158.py::test_reader_takes_the_column_list_from_the_union_artifact`。
    """
    names = [f["name"] for f in load_payload()["features"]]
    known = set(names)
    spec = spec.strip()
    if spec.lower() == "all":
        return names
    if spec.endswith(".json"):
        path = Path(spec)
        if not path.exists():
            raise ValueError(f"名单工件不存在: {path}")
        body = json.loads(path.read_text(encoding="utf-8"))
        cols = list(body.get("alpha158_net_new", []))
        if not cols:
            raise ValueError(f"{path} 里没有 alpha158_net_new 字段")
        stray = [c for c in cols if c not in known]
        if stray:
            raise ValueError(f"{path.name} 里有不在 158 清单工件的维: {stray}")
        return cols
    cols = [c.strip() for c in spec.split(",") if c.strip()]
    stray = [c for c in cols if c not in known]
    if stray:
        raise ValueError(f"这些名字不在 158 清单工件里，拒绝静默丢列: {stray}")
    return cols


def _alpha158_frame(raw: pl.DataFrame, hfq: pl.DataFrame):  # -> pandas.DataFrame
    """按面板同一套装配把 158 维算出来：价格取 hfq，vwap 由 raw amount/volume 推再 rebase。

    三处口径都写过缺陷或踩过坑，都写在 `scripts/alpha158_ic_panel.py::build_code_panel`
    的同一段里，这里与它**逐字同形**——宽表与面板对同一只票同一根 bar 必须给同一个数，
    否则「IC 判过的那一维」和「模型吃到的那一列」是两回事：

    ① 日线 `volume` 的单位是**手**（÷100 才是每股价，KI-DATA-005）；
    ② `$vwap` 必须与 `$close` 同口径（再 ×当日因子，同上）；
    ③ 停牌日（volume=0）用 `np.nan` 而不是 `pd.NA`：后者把列变成 object 可空类型，
       下游任何一次 `float()` / `to_numpy(dtype=float)` 都对 `NAType` 抛 TypeError，
       症状不是「这一维缺值」而是**整只票**被失败记账丢掉——2026-11-04 全量装配
       第一次跑就丢了 10 只，而行数、列名、其余值全都正常。守卫
       `tests/test_stage1_wide_table_alpha158.py::test_suspended_day_yields_nan_not_a_dropped_code`。
    """
    import numpy as np  # 只在这里要：停牌日的 NaN 语义要落在 float 列上

    raw_pd = raw.to_pandas()
    hfq_pd = hfq.to_pandas()
    px = hfq_pd[["date", "open", "high", "low", "close", "volume"]].copy()
    for col in ("open", "high", "low", "close", "volume"):
        px[col] = px[col].astype(float)
    per_share = raw_pd["amount"].astype(float) / (raw_pd["volume"].astype(float).replace(0, np.nan) * 100.0)
    factor = hfq_pd["close"].astype(float) / raw_pd["close"].astype(float).replace(0, np.nan)
    px["vwap"] = per_share * factor
    return evaluate_frame(px.reset_index(drop=True))


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
    price_basis: str = "raw",
    alpha158_cols: Sequence[str] | str | None = None,
) -> dict:
    """装配宽表。按 code 分组，每股票读一次 parquet，对其每个 signal_date 计算特征。

    `alpha158_cols` 给了就在**同一根锚点 bar**（`sig_idx-1`）上附 158 维的若干列；
    这条路要求 `price_basis="hfq"`——158 条表达式吃的价格列必须是读侧权威那一帧，
    raw 帧上算出来的是一整套没复权的价格比率，形状正常但除权处全错。
    """
    # str 也是 Sequence，所以必须先判 str——否则 "all" 会被拆成 ['a','l','l']
    if not alpha158_cols:
        cols_a158 = []
    elif isinstance(alpha158_cols, str):
        cols_a158 = resolve_alpha158_cols(alpha158_cols)
    else:
        cols_a158 = list(alpha158_cols)
    if cols_a158 and price_basis != "hfq":
        raise ValueError(
            f"alpha158_cols 要配 price_basis='hfq'（当前 {price_basis!r}）："
            "158 维的 $close/$open/$high/$low 必须走 hfq 读侧权威，工件口径 ③")
    if price_basis != "raw" and verify_parity:
        log.info("price_basis=%s：CSV 里是 raw 特征，parity 检查自动关闭", price_basis)
        verify_parity = False
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
    n_hfq_skip = 0
    n_alpha158_fail = 0

    for code in codes:
        fp = DAILY / f"{code}.parquet"
        if not fp.exists():
            n_missing_file += int((base.get_column("code") == code).sum())
            continue
        # raw 路径一字不动：仍按 fp 直读，不绕咽喉（保持与既有宽表逐值一致）
        df = _norm_date(pl.read_parquet(fp))
        frame_hfq = None
        if price_basis == "hfq":
            f_hfq = adj_frame(code, kind="hfq", strict=False)
            if f_hfq.attrs.get("factor_cov", 0.0) < MIN_FACTOR_COV:
                n_hfq_skip += int((base.get_column("code") == code).sum())
                continue
            frame_hfq = _norm_date(pl.from_pandas(f_hfq))
            # 同长还不够：两帧必须同日同序，否则 sig_idx 会错位
            if (frame_hfq.height != df.height
                    or frame_hfq["date"].to_list() != df["date"].to_list()):
                n_hfq_skip += int((base.get_column("code") == code).sum())
                continue
        a158 = None
        if cols_a158:
            # 一票一次算完 158 条（表达式有共享子树，求值面内部已带缓存），下面按锚点取行。
            # 失败要连这只票的行数一起记账：静默 continue 会让「这一维没进宽表」长得
            # 和「这一维全是 0」一模一样。
            try:
                a158 = _alpha158_frame(df, frame_hfq)  # type: ignore[arg-type]
            except Exception as e:  # SEM002：计数并跳过整只，不静默少列
                n_alpha158_fail += int((base.get_column("code") == code).sum())
                log.warning("Alpha158 装配失败 %s: %s: %s", code, type(e).__name__, str(e)[:80])
                continue
            missing_a = [c for c in cols_a158 if c not in a158.columns]
            if missing_a:
                n_alpha158_fail += int((base.get_column("code") == code).sum())
                log.warning("Alpha158 缺列 %s: %s", code, missing_a[:5])
                continue
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
                f26 = compute_window_features_extended_polars(
                    df, code, sig_idx=anchor, frame_hfq=frame_hfq)
                if f26 is None:
                    n_missing_date += 1
                    continue
                cands = compute_candidate_features(
                    frame_hfq if frame_hfq is not None else df,
                    code, sig_idx=anchor, lookback=60)
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
            if a158 is not None:
                # 与 26 维同一根锚点 bar（sig_idx-1）：错位一根就是把信号日当天的收盘喂进特征
                row = a158.iloc[anchor]
                for k in cols_a158:
                    rec[k] = float(row[k])

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
        "完成: n_ok=%d 缺失文件=%d 缺失日期=%d 计算失败=%d 缺因子跳过=%d "
        "Alpha158失败=%d parity_max=%.3e",
        n_ok, n_missing_file, n_missing_date, n_compute_fail, n_hfq_skip,
        n_alpha158_fail, parity_max,
    )
    print(f"\n宽表写出: {out_parquet}  rows={out.height}  cols={out.width}")
    print(f"  26维 parity_max(有限值对) = {parity_max:.3e}")
    print(f"  缺失文件={n_missing_file}  缺失日期={n_missing_date}  计算失败(数据异常跳过)={n_compute_fail}")
    print(f"  缺因子整只跳过={n_hfq_skip}")
    if cols_a158:
        nan_cells = sum(1 for r in records for c in cols_a158 if not math.isfinite(r[c]))
        print(f"  Alpha158 附加列={len(cols_a158)}  装配失败整只跳过={n_alpha158_fail}  "
              f"非有限单元={nan_cells}")
    print(f"  非有限单元={n_nonfinite}  其中与CSV不一致(疑似bug)={n_asym_nonfinite}")
    print(f"  特征漂移: 绝对差>1 的单元={n_diff_big}  相对差>1e-3 的单元={n_diff_rel}")
    return {
        "n_ok": n_ok, "n_missing_file": n_missing_file,
        "n_missing_date": n_missing_date, "n_compute_fail": n_compute_fail,
        "n_nonfinite": n_nonfinite, "n_asym_nonfinite": n_asym_nonfinite,
        "n_diff_big": n_diff_big, "n_diff_rel": n_diff_rel,
        "n_hfq_skip": n_hfq_skip,
        "n_alpha158_fail": n_alpha158_fail,
        "alpha158_cols": list(cols_a158),
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
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ap.add_argument("--alpha158", type=str, default=None,
                    help="附 Alpha158 列：'all' | 逗号名单 | union 工件路径（要配 --price-basis hfq）")
    a = ap.parse_args()
    build_wide_table(
        src_csv=Path(a.src), out_parquet=Path(a.out),
        sample_codes=a.sample, verify_parity=not a.no_parity,
        price_basis=a.price_basis, alpha158_cols=a.alpha158,
    )
