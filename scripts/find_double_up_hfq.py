"""用后复权价重筛 2020+ 翻倍样本（样本宇宙的读侧权威切 hfq）。

筛选口径逐项复刻 find_double_up_c_v2.py：起点 2020-01-01、20 个交易日窗口、
峰值/起点 >= 2.0、涨停计数用 raw 涨幅、is_new = 2020+ 切片首日起 < 250 天
（v2 的这个基准是切片首日，老票会被标成新股；这里保留以便两个宇宙逐行对照）。
唯一的差别是价格：close 来自 tdxfeed.adjusted.frame(kind="hfq")，把送转/除权
还原到同一尺度，避免 qfq 旧表里的因子跳变被当成翻倍（603125 在 2023-05-25 的
「翻倍」就是旧 adj_factor 从 0.2179 跳到 0.4668 制造的伪事件）。

窗口 (起点, 起点+window] 内跨「疑似上跳」的事件整体剔除并写审计表：adjusted.factor_jumps
判 suspect 且 econ>0 的才是上跳（302132 / 003038 / 000981 已确认是源侧错误），下跳只会
压缩涨幅、不制造伪事件，只记录不剔除。起点当日的跳变不影响 close[j]/close[i]，不剔除。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.adjusted import MIN_FACTOR_COV, factor_jumps, frame  # noqa: E402
from tdxfeed.execution.limits import normalize_code  # noqa: E402
from tdxfeed.features_5d import get_limit  # noqa: E402
from tdxfeed.paths import daily_files  # noqa: E402

log = logging.getLogger("find_double_up_hfq")

NAME_MAP = ROOT / "data" / "all_a_stocks.json"
OUT_DETAIL = ROOT / "data" / "double_up" / "double_up_hfq_2020_2026_detail.csv"
OUT_EXCLUDED = ROOT / "data" / "double_up" / "double_up_hfq_excluded_2020_2026.csv"

START_DATE = "2020-01-01"
WINDOW = 20
RATIO = 2.0

HIT_COLS = [
    "code", "name", "start_date", "peak_date", "days_to_peak",
    "start_close_hfq", "peak_close_hfq", "pct_change", "limit_up_count",
    "window_amount", "year", "is_st", "is_new", "limit",
]
EXCLUDED_COLS = HIT_COLS + ["suspect_date", "n_suspect", "exclude_reason"]


def _suspect_up_dates(jumps: pd.DataFrame) -> np.ndarray:
    """只保留 econ>0 的 suspect 跳变：上跳会制造假涨幅，下跳只会压缩涨幅。"""
    if jumps.empty:
        return np.array([], dtype="datetime64[ns]")
    sel = jumps[jumps["suspect"].astype(bool) & (jumps["econ"] > 0)]
    return pd.DatetimeIndex(pd.to_datetime(sel["date"])).to_numpy()


def screen_series(
    code: str,
    dates,
    close_hfq,
    close_raw,
    amount,
    *,
    limit: float,
    name: str = "?",
    is_st: bool = False,
    start: str | pd.Timestamp = START_DATE,
    window: int = WINDOW,
    ratio_threshold: float = RATIO,
    suspect_up_dates=(),
) -> tuple[list[dict], list[dict]]:
    """逐窗口判 2×。返回 (命中, 因跨 suspect 上跳日被剔除)，两者同 schema 的前缀。"""
    d = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    hfq = np.asarray(close_hfq, dtype=float)
    raw = np.asarray(close_raw, dtype=float)
    amt = np.asarray(amount, dtype=float)
    keep = d >= pd.Timestamp(start)
    d, hfq, raw, amt = d[keep], hfq[keep], raw[keep], amt[keep]
    n = len(hfq)
    if n < window + 5:
        return [], []

    days_since_first = np.asarray((d - d[0]).days)
    raw_pct = np.full(n, np.nan)
    raw_pct[1:] = raw[1:] / raw[:-1] - 1.0
    sus = np.sort(pd.DatetimeIndex(pd.to_datetime(list(suspect_up_dates))).to_numpy())
    dn = d.to_numpy()

    hits: list[dict] = []
    dropped: list[dict] = []
    for i in range(n - window):
        base = float(hfq[i])
        if not np.isfinite(base) or base <= 0:
            continue
        seg = hfq[i + 1:i + window + 1]
        peak = float(np.max(seg))
        ratio = peak / base
        if not ratio >= ratio_threshold:
            continue
        peak_idx = i + 1 + int(np.argmax(seg))
        rec = {
            "code": code,
            "name": name,
            "start_date": d[i].strftime("%Y-%m-%d"),
            "peak_date": d[peak_idx].strftime("%Y-%m-%d"),
            "days_to_peak": peak_idx - i,
            "start_close_hfq": round(base, 4),
            "peak_close_hfq": round(peak, 4),
            "pct_change": round((ratio - 1.0) * 100.0, 1),
            "limit_up_count": int((raw_pct[i + 1:i + window + 1] >= limit - 0.001).sum()),
            "window_amount": float(np.sum(amt[i + 1:i + window + 1])),
            "year": int(d[peak_idx].year),
            "is_st": bool(is_st),
            "is_new": bool(days_since_first[i] < 250),
            "limit": round(limit * 100.0, 0),
        }
        inside = sus[(sus > dn[i]) & (sus <= dn[i + window])] if len(sus) else sus
        if len(inside):
            dropped.append({
                **rec,
                "suspect_date": pd.Timestamp(inside[0]).strftime("%Y-%m-%d"),
                "n_suspect": int(len(inside)),
                "exclude_reason": "suspect_up_jump",
            })
        else:
            hits.append(rec)
    return hits, dropped


def screen_one(code: str, *, name: str, is_st: bool, start=START_DATE,
               window: int = WINDOW, ratio_threshold: float = RATIO
               ) -> tuple[list[dict], list[dict], str]:
    """单票筛选。缺因子 / 因子对不上 raw_close 的票按 raw 走会把两种口径混进同一个宇宙，
    整只跳过并计入 no_factor。"""
    hfq_frame = frame(code, kind="hfq", strict=False)
    if hfq_frame.attrs.get("factor_cov", 0.0) < MIN_FACTOR_COV:
        return [], [], "no_factor"
    raw_frame = frame(code, kind="raw").sort_values("date")
    hits, dropped = screen_series(
        code, hfq_frame["date"], hfq_frame["close"],
        raw_frame["close"], raw_frame["amount"],
        limit=float(get_limit(code)), name=name, is_st=is_st,
        start=start, window=window, ratio_threshold=ratio_threshold,
        suspect_up_dates=_suspect_up_dates(factor_jumps(code)),
    )
    return hits, dropped, "ok"


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="用 hfq 价重筛 2020+ 翻倍样本")
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--ratio", type=float, default=RATIO)
    ap.add_argument("--sample", type=int, default=0, help="只扫前 N 只（调试）")
    ap.add_argument("--names", default=str(NAME_MAP))
    ap.add_argument("--out", default=str(OUT_DETAIL))
    ap.add_argument("--excluded-out", default=str(OUT_EXCLUDED))
    args = ap.parse_args(argv)

    t0 = time.time()
    names = json.loads(Path(args.names).read_text(encoding="utf-8"))
    files = daily_files()
    if args.sample:
        files = files[:args.sample]
    log.info("日线文件 %d 只（抽样 %s）", len(files), args.sample or "否")

    status: Counter = Counter()
    hits_all: list[dict] = []
    dropped_all: list[dict] = []
    for i, fp in enumerate(files, 1):
        code = fp.stem
        name = names.get(normalize_code(code), "?")
        try:
            hits, dropped, st = screen_one(
                code, name=name, is_st="ST" in name.upper(), start=args.start,
                window=args.window, ratio_threshold=args.ratio)
        except Exception as e:  # noqa: BLE001 —— 单票坏数据不打断全表
            status["error"] += 1
            log.warning("跳过 %s：%s", code, e)
            continue
        status[st] += 1
        hits_all.extend(hits)
        dropped_all.extend(dropped)
        if i % 500 == 0:
            log.info("[%d/%d] %.0fs hits=%d excluded=%d",
                     i, len(files), time.time() - t0, len(hits_all), len(dropped_all))

    out = pd.DataFrame(hits_all, columns=HIT_COLS)
    if len(out):
        out = out.sort_values(["year", "code", "start_date"]).reset_index(drop=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    exc = pd.DataFrame(dropped_all, columns=EXCLUDED_COLS)
    if len(exc):
        exc = exc.sort_values(["year", "code", "start_date"]).reset_index(drop=True)
    exc.to_csv(args.excluded_out, index=False, encoding="utf-8-sig")

    elapsed = time.time() - t0
    print(f"\n命中: {len(out)} 笔 / {out['code'].nunique() if len(out) else 0} 只"
          f"  （剔除跨 suspect 上跳: {len(exc)} 笔 / {exc['code'].nunique() if len(exc) else 0} 只）")
    print(f"口径: hfq  close | start={args.start} window={args.window} ratio>={args.ratio}")
    print(f"逐票状态: ok={status['ok']} no_factor={status['no_factor']} error={status['error']}")
    print(f"耗时: {elapsed:.0f}s")
    print(f"明细: {args.out}")
    print(f"剔除: {args.excluded_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
