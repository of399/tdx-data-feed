#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 daily.parquet 从 v1（date 三态）迁移到 v2（date date32，列数不变）。

v2 规则见 `.codebuddy/rules/tdx-data-schema-v2.md`。

【重要】
- **默认 dry-run**：仅打印计划，不修改文件；传 `--apply` 才真正写入
- **强制备份**：写入前把原文件 rename 为 `.v1.bak.parquet`，保留 30 天
- **列数严格不变**：daily 保持 7 列（不补 adj_factor）；adj_factor 保持 5 列
- **数据完整**：迁移只换 date 列类型为 date32，不动 row 内容

用法:
    # 1. 扫描现状（推荐先看 check_schema.py 输出）
    venv/bin/python scripts/check_schema.py

    # 2. dry-run（默认；仅打印计划）
    venv/bin/python scripts/migrate_v1_to_v2.py
    venv/bin/python scripts/migrate_v1_to_v2.py --limit 10

    # 3. 实际执行（不可逆，先备份）
    venv/bin/python scripts/migrate_v1_to_v2.py --apply --limit 100

    # 4. 验证
    venv/bin/python scripts/check_schema.py
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
ADJ_DIR = ROOT / "data" / "adj_factor"

DAILY_V2_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]
ADJ_V2_COLS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_v2")


def _normalize_date_series(s: pd.Series) -> pd.Series:
    """任意 date 类型 (int / timestamp / object) → datetime64[ns]（无时区）。"""
    if s.dtype.kind in "iu":
        return pd.to_datetime(
            s.dropna().astype("Int64").astype(str),
            format="%Y%m%d",
            errors="coerce",
        ).reindex(s.index)
    if s.dtype.kind == "M":
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
        return s
    return pd.to_datetime(s, errors="coerce")


def migrate_daily_one(fp: Path, *, apply: bool) -> tuple[str, int]:
    """迁移单个 daily parquet 到 v2。

    返回 (status, n_rows)；status ∈ {noop / migrated / failed / skipped}.
    """
    try:
        schema = pq.read_schema(fp)
    except Exception as e:
        return f"failed:{type(e).__name__}", 0

    col_set = {f.name for f in schema}
    if col_set != set(DAILY_V2_COLS):
        return f"failed:cols={sorted(col_set)}", 0

    date_field = next(f for f in schema if f.name == "date")
    if str(date_field.type) == "date32[day]":
        return "noop", pq.read_metadata(fp).num_rows

    if not apply:
        return "would-migrate", pq.read_metadata(fp).num_rows

    df = pq.read_table(fp).to_pandas()
    n_rows = len(df)
    df["date"] = _normalize_date_series(df["date"])
    df = df.dropna(subset=["date", "close"])
    df = df.sort_values("date").reset_index(drop=True)

    if list(df.columns) != DAILY_V2_COLS:
        return f"failed:post_cols={list(df.columns)}", 0

    date_py = df["date"].dt.date
    date32_arr = pa.array(date_py, type=pa.date32())
    table = pa.Table.from_pandas(df, preserve_index=False)
    date_idx = table.schema.get_field_index("date")
    table = table.set_column(date_idx, "date", date32_arr)

    backup = fp.with_suffix(".v1.bak.parquet")
    if not backup.exists():
        shutil.move(str(fp), str(backup))
    else:
        fp.unlink()

    pq.write_table(table, fp, compression="snappy")
    return "migrated", n_rows


def migrate_adj_one(fp: Path, *, apply: bool) -> tuple[str, int]:
    """迁移单个 adj_factor parquet 到 v2（date date32；列数/顺序不变）。"""
    try:
        schema = pq.read_schema(fp)
    except Exception as e:
        return f"failed:{type(e).__name__}", 0

    col_set = {f.name for f in schema}
    if col_set != set(ADJ_V2_COLS):
        return f"failed:cols={sorted(col_set)}", 0

    date_field = next(f for f in schema if f.name == "date")
    if str(date_field.type) == "date32[day]":
        return "noop", pq.read_metadata(fp).num_rows

    if not apply:
        return "would-migrate", pq.read_metadata(fp).num_rows

    df = pq.read_table(fp).to_pandas()
    n_rows = len(df)
    df["date"] = _normalize_date_series(df["date"])
    df = df.dropna(subset=["date", "adj_factor"])
    df = df.sort_values("date").reset_index(drop=True)

    if list(df.columns) != ADJ_V2_COLS:
        return f"failed:post_cols={list(df.columns)}", 0

    date_py = df["date"].dt.date
    date32_arr = pa.array(date_py, type=pa.date32())
    table = pa.Table.from_pandas(df, preserve_index=False)
    date_idx = table.schema.get_field_index("date")
    table = table.set_column(date_idx, "date", date32_arr)

    backup = fp.with_suffix(".v1.bak.parquet")
    if not backup.exists():
        shutil.move(str(fp), str(backup))
    else:
        fp.unlink()

    pq.write_table(table, fp, compression="snappy")
    return "migrated", n_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="v1 → v2 schema 迁移 (daily.date / adj_factor.date → date32)")
    parser.add_argument("--apply", action="store_true", help="实际写入；不加则 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试点用）")
    parser.add_argument("--target", choices=["daily", "adj", "both"], default="both")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    log.warning("=" * 60)
    log.warning(f"v1 → v2 schema 迁移  模式: {mode}  target: {args.target}")
    log.warning("=" * 60)

    counters: dict[str, int] = {}

    if args.target in ("daily", "both"):
        files = sorted(DAILY_DIR.glob("*.parquet"))
        if args.limit:
            files = files[: args.limit]
        log.info(f"daily 候选: {len(files)} files")
        t0 = time.time()
        for i, fp in enumerate(files, 1):
            status, n = migrate_daily_one(fp, apply=args.apply)
            counters[status] = counters.get(status, 0) + 1
            if i % 500 == 0 or i == len(files):
                elapsed = time.time() - t0
                log.info(f"  daily [{i}/{len(files)}] elapsed={elapsed:.0f}s status={counters}")
        log.info(f"daily 完成 counters={counters}")

    if args.target in ("adj", "both"):
        files = sorted(ADJ_DIR.glob("*.parquet"))
        if args.limit:
            files = files[: args.limit]
        log.info(f"adj_factor 候选: {len(files)} files")
        t0 = time.time()
        adj_counters: dict[str, int] = {}
        for i, fp in enumerate(files, 1):
            status, n = migrate_adj_one(fp, apply=args.apply)
            adj_counters[status] = adj_counters.get(status, 0) + 1
            if i % 500 == 0 or i == len(files):
                elapsed = time.time() - t0
                log.info(f"  adj [{i}/{len(files)}] elapsed={elapsed:.0f}s status={adj_counters}")
        log.info(f"adj 完成 counters={adj_counters}")

    log.warning("=" * 60)
    log.warning(f"{'实际写入完成' if args.apply else 'dry-run 完成（未修改任何文件）'}")
    log.warning("=" * 60)


if __name__ == "__main__":
    main()