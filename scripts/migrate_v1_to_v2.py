#!/usr/bin/env python3
"""migrate_v1_to_v2.py — 将 daily parquet 的 date 列统一为 date32[day]（tdx-data-schema-v2 提案步骤 3）。

用法:
    venv/bin/python scripts/migrate_v1_to_v2.py              # dry-run（仅统计）
    venv/bin/python scripts/migrate_v1_to_v2.py --apply       # 真实迁移
    venv/bin/python scripts/migrate_v1_to_v2.py --apply --limit 10   # 仅迁移前 10 个（调试）

约束（不可破坏原则）:
- 仅改变 date 列类型，不动字段名 / 字段顺序 / 行内容（v2 对 daily.date 的例外放行）。
- 原文件备份为 <code>.v1.bak.parquet，不删除历史数据。
- 仅迁移 date 类型非 date32[day] 的文件；已合规者跳过。

注意：按 v2 流程，真实切流前需经历 30 天双写共存期；本脚本仅完成类型归一，
配合 full_sync 的 date→date32 写入实现"新数据合规 + 旧数据按需迁移"。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
EXPECTED_DATE_TYPE = "date32[day]"


def _norm_date(s: pd.Series) -> pd.Series:
    """将三态 date（int64 YYYYMMDD / timestamp / date32）统一为 date。"""
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype(str), format="%Y%m%d").dt.date
    return pd.to_datetime(s).dt.date


def main() -> None:
    ap = argparse.ArgumentParser(description="daily parquet date 列统一为 date32[day]")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个异常文件（调试）")
    args = ap.parse_args()

    files = sorted(DAILY_DIR.glob("*.parquet"))
    to_migrate: list[Path] = []
    for p in files:
        try:
            schema = pq.read_schema(p)
        except Exception:
            continue
        dt = next((f.type for f in schema if f.name == "date"), None)
        if str(dt) != EXPECTED_DATE_TYPE:
            to_migrate.append(p)

    print(f"扫描 {len(files)} 个文件，需迁移（date≠{EXPECTED_DATE_TYPE}）: {len(to_migrate)}")
    if not to_migrate:
        print("✓ 无需迁移")
        return
    if args.limit:
        to_migrate = to_migrate[: args.limit]

    for p in to_migrate:
        df = pd.read_parquet(p)
        df["date"] = _norm_date(df["date"])
        if not args.apply:
            print(f"  [DRY] 将迁移 {p.name} -> date32 ({len(df)} rows)")
            continue
        bak = p.with_suffix(".v1.bak.parquet")
        if not bak.exists():
            p.rename(bak)
        df.to_parquet(p, index=False, engine="pyarrow")
        print(f"  [OK] 迁移 {p.name} -> date32 ({len(df)} rows, 备份 {bak.name})")

    if not args.apply:
        print("（dry-run 完成，未写入。加 --apply 执行真实迁移）")


if __name__ == "__main__":
    main()
