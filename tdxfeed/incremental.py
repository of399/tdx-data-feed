"""增量更新日 K（按 v2 schema 写入）。

【数据源优先级】Tencent → Akshare(Eastmoney) → Sina(过滤)
- Tencent 真正支持 start_date / end_date（增量首选）
- Akshare/Eastmoney 支持 start_date（次选）
- Sina 不支持 range，但可拉最近 1023 根（约 4 年）后过滤 start

【v2 schema】见 .codebuddy/rules/tdx-data-schema-v2.md
- daily 7 列：date(date32), open, high, low, close, volume, amount
- 列数严格不变；date 强制 date32

【用法】
    # 单只
    venv/bin/python -m tdxfeed.incremental --symbol sh600000

    # 批量（从文件）
    venv/bin/python -m tdxfeed.incremental --codes-file scripts/etf_bond_codes.txt

    # 全市场（默认扫所有 parquet，按 last_date 判断增量）
    venv/bin/python -m tdxfeed.incremental --limit 100
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tdxfeed.amount_fix import fill_missing_amount
import pyarrow as pa
import pyarrow.parquet as pq

from tdxfeed.sources.akshare_source import AkshareSource
from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.sources.tencent_source import TencentSource
from tdxfeed.writers import Writers
from tdxfeed.paths import daily_dir  # noqa: E402

ROOT = Path(__file__).parent.parent
DAILY_DIR = daily_dir()

log = logging.getLogger("incremental")

V2_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _norm_date(s: pd.Series) -> pd.Series:
    """任意 date 类型 (int / timestamp / object) → datetime64[ns]（无时区）。"""
    if s.dtype.kind in "iu":
        return pd.to_datetime(
            s.astype("Int64").astype(str),
            format="%Y%m%d",
            errors="coerce",
        ).reindex(s.index)
    if s.dtype.kind == "M":
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
        return s
    return pd.to_datetime(s, errors="coerce")


def _bars_to_df(bars: list) -> pd.DataFrame:
    """bars（list of list）→ DataFrame（v2 列）。"""
    df = pd.DataFrame(bars, columns=V2_COLS)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["date"] = _norm_date(df["date"])
    df = df.dropna(subset=["date", "close"])
    df = df.drop_duplicates(subset=["date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)


def _write_v2(fp: Path, df: pd.DataFrame) -> None:
    """v2 写入：date 列强制 date32。"""
    if list(df.columns) != V2_COLS:
        raise ValueError(f"列不符 v2: {list(df.columns)}")
    date_py = df["date"].dt.date
    arr = pa.array(date_py, type=pa.date32())
    table = pa.Table.from_pandas(df, preserve_index=False)
    idx = table.schema.get_field_index("date")
    table = table.set_column(idx, "date", arr)
    fp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, fp, compression="snappy")


def fetch_range(symbol: str, start: date, end: date) -> list | None:
    """多源兜底拉取 [start, end] 区间日 K。返回 bars 或 None。"""
    s, e = start.isoformat(), end.isoformat()
    # 1) Tencent：真正支持 start/end
    try:
        bars = TencentSource().fetch_bars(symbol, start_date=s, end_date=e)
        if bars:
            log.debug("tencent ok %s [%s~%s] %d bars", symbol, s, e, len(bars))
            return bars
    except Exception as ex:
        log.debug("tencent fail %s: %s", symbol, ex)
    # 2) Akshare (Eastmoney 支持 start_date)
    try:
        bars = AkshareSource().fetch_bars(symbol, start_date=s, end_date=e)
        if bars:
            log.debug("akshare ok %s [%s~%s] %d bars", symbol, s, e, len(bars))
            return bars
    except Exception as ex:
        log.debug("akshare fail %s: %s", symbol, ex)
    # 3) Sina 兜底：拉最近 1023 根 → 过滤 start 之后
    try:
        bars = SinaSource().fetch_bars(symbol)
        if bars:
            cutoff = int(start.strftime("%Y%m%d"))
            filtered = [b for b in bars if Writers._norm_date(b[0]) >= cutoff]
            if filtered:
                log.debug("sina ok %s filter %d/%d bars", symbol, len(filtered), len(bars))
                return filtered
    except Exception as ex:
        log.debug("sina fail %s: %s", symbol, ex)
    return None


def incremental_update(symbol: str) -> dict:
    """单只标的增量更新。返回 dict 含 status / rows / added。"""
    fp = DAILY_DIR / f"{symbol}.parquet"
    today = date.today()
    old: pd.DataFrame | None = None
    start: date | None = None

    if fp.exists():
        old = pd.read_parquet(fp)
        old["date"] = _norm_date(old["date"])
        last = old["date"].max()
        if pd.notna(last):
            start = last.date() + timedelta(days=1)

    if start is not None and start > today:
        return {"symbol": symbol, "status": "up_to_date", "rows": len(old) if old is not None else 0, "added": 0}

    bars = fetch_range(symbol, start or date(1990, 1, 1), today)
    if not bars:
        return {"symbol": symbol, "status": "fetch_failed", "rows": len(old) if old is not None else 0, "added": 0}

    new = _bars_to_df(bars)
    if old is not None:
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    # amount 同日 max（防 Sina amount=0 覆盖）
    merged["amount"] = merged.groupby("date")["amount"].transform("max")
    merged, _ = fill_missing_amount(merged)

    _write_v2(fp, merged)
    return {
        "symbol": symbol,
        "status": "ok",
        "rows": len(merged),
        "added": len(merged) - (len(old) if old is not None else 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="增量更新日 K (v2 schema)")
    ap.add_argument("--symbol", help="单只代码 (e.g. sh600000 / sz000001 / bj920000)")
    ap.add_argument("--codes-file", help="代码列表文件 (每行一个 symbol)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 只")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.symbol:
        syms = [args.symbol]
    elif args.codes_file:
        syms = [s.strip() for s in Path(args.codes_file).read_text().splitlines() if s.strip()]
    else:
        syms = sorted([p.stem for p in DAILY_DIR.glob("*.parquet")])

    if args.limit:
        syms = syms[: args.limit]

    log.warning("增量更新 %d 只", len(syms))
    ok = skip = fail = added_total = 0
    t0 = time.time()
    for i, sym in enumerate(syms, 1):
        try:
            r = incremental_update(sym)
        except Exception as ex:
            log.error("  [%d/%d] %s exception: %s", i, len(syms), sym, ex)
            fail += 1
            continue
        st = r["status"]
        if st == "ok":
            ok += 1
            added_total += r["added"]
        else:
            skip += 1
        if i % 100 == 0 or i == len(syms):
            el = time.time() - t0
            log.info(
                "  [%d/%d] ok=%d skip=%d fail=%d added=%d el=%.0fs",
                i, len(syms), ok, skip, fail, added_total, el,
            )
    log.warning("完成 ok=%d skip=%d fail=%d added=%d", ok, skip, fail, added_total)


if __name__ == "__main__":
    main()
