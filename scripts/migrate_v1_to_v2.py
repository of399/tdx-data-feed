#!/usr/bin/env python3
"""migrate_v1_to_v2.py — daily parquet 维护工具（tdx-data-schema-v2 提案步骤 3-5）。

子命令:
    status                默认。统计 v2 合规率与 .v1.bak 数量（v2 步骤 1 baseline）
    migrate [--apply]     把 date 列非 date32[day] 的 active parquet 归一为 v2（dry-run 默认）
    cleanup-v1-bak [--apply]  删除 .v1.bak.parquet 迁移备份（v2 步骤 5：双写期 ≥30 天后执行）

约束（不可破坏原则）:
- 仅改变 date 列类型，不动字段名 / 字段顺序 / 行内容。
- 原文件备份为 <code>.v1.bak.parquet，不删除历史数据（migrate 阶段）。
- 备份保留期 ≥30 天，cleanup 前需 status 显示 active 已 100% v2 合规。
- 任何 .bak 文件 freshness.py / check_schema.py / predict.py 都已自动排除，不影响运行时。

用法:
    venv/bin/python scripts/migrate_v1_to_v2.py                              # status
    venv/bin/python scripts/migrate_v1_to_v2.py migrate --apply              # dry-run 默认
    venv/bin/python scripts/migrate_v1_to_v2.py cleanup-v1-bak              # 列出待删
    venv/bin/python scripts/migrate_v1_to_v2.py cleanup-v1-bak --apply      # 真正删除

依赖（仅 status 子命令需联网可选；其余纯本地操作）:
    pyarrow >= 14
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
ADJ_DIR = ROOT / "data" / "adj_factor"
EXPECTED_DATE_TYPE = "date32[day]"
V1_BAK_SUFFIX = ".v1.bak.parquet"
DUAL_WRITE_MIN_DAYS = 30  # v2 提案步骤 4：双写期 30 天


def _is_active(p: Path) -> bool:
    """Active parquet（非迁移备份）。"""
    return ".bak" not in p.name


def _is_v1_bak(p: Path) -> bool:
    return p.name.endswith(V1_BAK_SUFFIX)


def _norm_date_series(s) -> pd.Series:  # noqa: F821
    """统一三态 date（int64 YYYYMMDD / timestamp / date32）→ datetime.date。"""
    import pandas as pd
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype(str), format="%Y%m%d").dt.date
    return pd.to_datetime(s, errors="coerce").dt.date


def _date_field_type(p: Path) -> str | None:
    try:
        schema = pq.read_schema(p)
    except Exception:
        return None
    dt = next((f.type for f in schema if f.name == "date"), None)
    return str(dt) if dt is not None else None


def _file_age_days(p: Path) -> float:
    """基于 mtime 的文件年龄（天）。"""
    mtime = p.stat().st_mtime
    return (datetime.now(tz=UTC).timestamp() - mtime) / 86400.0


# ---------- subcommand: status ----------
def cmd_status(_args) -> int:
    files = sorted(DAILY_DIR.glob("*.parquet"))
    active = [p for p in files if _is_active(p)]
    v1_bak = [p for p in files if _is_v1_bak(p)]

    # active 合规率
    type_counter: Counter[str] = Counter()
    need_migrate: list[Path] = []
    for p in active:
        dt = _date_field_type(p)
        type_counter[dt or "<no_date_field>"] += 1
        if dt != EXPECTED_DATE_TYPE:
            need_migrate.append(p)

    # v1.bak 备份年龄
    ages = [_file_age_days(p) for p in v1_bak]
    oldest = max(ages) if ages else 0.0
    newest = min(ages) if ages else 0.0

    print("=" * 70)
    print(f"TDX v2 schema 状态  @  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 70)
    print(f"  daily 总文件:     {len(files)}")
    print(f"  active:           {len(active)}  (非 .bak)")
    print(f"  .v1.bak 备份:     {len(v1_bak)}  (待 cleanup)")
    print()
    print(f"  active date 类型分布: {dict(type_counter)}")
    print(f"  v2 合规 (date32): {len(active) - len(need_migrate)}/{len(active)}  "
          f"({100*(len(active)-len(need_migrate))/max(1,len(active)):.2f}%)")
    if need_migrate:
        print(f"  ⚠ 需 migrate:     {len(need_migrate)} 个")
    else:
        print("  ✓ 无需 migrate    （active 100% v2 合规）")
    print()
    if v1_bak:
        print(f"  v1.bak 年龄:      oldest={oldest:.1f}d, newest={newest:.1f}d "
              f"(双写期满需 ≥{DUAL_WRITE_MIN_DAYS}d)")
        if oldest >= DUAL_WRITE_MIN_DAYS and not need_migrate:
            print("  ✓ 可执行 cleanup-v1-bak （双写期满 + active 已 100% v2 合规）")
        else:
            print("  ✗ 暂不执行 cleanup: 双写期未满 或 active 未 100% 合规")
    else:
        print("  ✓ 无 v1.bak 备份（v2 步骤 5 已完成）")
    print()
    return 0


# ---------- subcommand: migrate ----------
def cmd_migrate(args) -> int:
    import pandas as pd

    files = sorted(p for p in DAILY_DIR.glob("*.parquet") if _is_active(p))
    to_migrate: list[Path] = []
    for p in files:
        if _date_field_type(p) != EXPECTED_DATE_TYPE:
            to_migrate.append(p)

    print(f"扫描 {len(files)} 个 active 文件，需迁移: {len(to_migrate)}")
    if not to_migrate:
        print("✓ 无需迁移")
        return 0
    if args.limit:
        to_migrate = to_migrate[: args.limit]
        print(f"  --limit 截断至 {len(to_migrate)} 个")

    migrated = skipped = 0
    for p in to_migrate:
        try:
            df = pd.read_parquet(p)
        except Exception as e:
            print(f"  [ERR] 读取 {p.name}: {e}")
            skipped += 1
            continue
        # 校验：列集合必须 = v2 期望
        if set(df.columns) != {"date", "open", "high", "low", "close", "volume", "amount"}:
            print(f"  [ERR] {p.name} 列集异常: {sorted(df.columns)}")
            skipped += 1
            continue
        df["date"] = _norm_date_series(df["date"])
        if not args.apply:
            print(f"  [DRY] 将迁移 {p.name} -> date32 ({len(df)} rows)")
            continue
        bak = p.with_name(p.stem + V1_BAK_SUFFIX)
        if not bak.exists():
            os.replace(p, bak)  # 原子改名（POSIX 原子）
        df.to_parquet(p, index=False, engine="pyarrow")
        print(f"  [OK] 迁移 {p.name} -> date32 ({len(df)} rows, 备份 {bak.name})")
        migrated += 1

    if not args.apply:
        print("（dry-run 完成，未写入。加 --apply 执行真实迁移）")
    else:
        print(f"完成: 迁移 {migrated} 个，跳过 {skipped} 个")
    return 0


# ---------- subcommand: cleanup-v1-bak ----------
def cmd_cleanup(args) -> int:
    files = sorted(DAILY_DIR.glob("*" + V1_BAK_SUFFIX))
    if not files:
        print("✓ 无 .v1.bak.parquet 文件可清理")
        return 0

    # 安全闸门 1：active 必须 100% v2 合规
    active = [p for p in DAILY_DIR.glob("*.parquet") if _is_active(p)]
    non_compliant = [p for p in active if _date_field_type(p) != EXPECTED_DATE_TYPE]
    if non_compliant:
        print(f"❌ 安全闸门未通过：还有 {len(non_compliant)} 个 active 非 v2 合规")
        print("   请先执行 `migrate --apply` 完成归一")
        return 2

    # 安全闸门 2：双写期 ≥ 30 天
    ages = [_file_age_days(p) for p in files]
    oldest = max(ages)
    if oldest < DUAL_WRITE_MIN_DAYS:
        print(f"❌ 安全闸门未通过：最旧 .v1.bak 仅 {oldest:.1f}d (<{DUAL_WRITE_MIN_DAYS}d 双写期)")
        print(f"   最少还需等待 {DUAL_WRITE_MIN_DAYS - oldest:.1f} 天")
        return 2

    total_size = sum(p.stat().st_size for p in files)
    print(f"待清理 .v1.bak: {len(files)} 个，共 {total_size/1024/1024:.1f} MB")
    print(f"  年龄: oldest={oldest:.1f}d, newest={min(ages):.1f}d (均 ≥{DUAL_WRITE_MIN_DAYS}d 双写期)")
    print("  active 100% v2 合规 ✓")

    if not args.apply:
        print("（dry-run 完成，未删除。加 --apply 执行真实删除）")
        return 0

    deleted = failed = 0
    for p in files:
        try:
            p.unlink()
            deleted += 1
        except Exception as e:
            print(f"  [ERR] 删除 {p.name}: {e}")
            failed += 1
    print(f"完成: 删除 {deleted} 个，失败 {failed} 个")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="daily parquet v1↔v2 维护（status / migrate / cleanup-v1-bak）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_status = sub.add_parser("status", help="v2 合规率与 .v1.bak 状态")
    p_status.set_defaults(func=cmd_status)

    p_mig = sub.add_parser("migrate", help="把 date 列非 date32[day] 的 active parquet 归一为 v2")
    p_mig.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    p_mig.add_argument("--limit", type=int, default=0, help="仅处理前 N 个异常文件（调试）")
    p_mig.set_defaults(func=cmd_migrate)

    p_clean = sub.add_parser("cleanup-v1-bak", help="删除 .v1.bak.parquet 迁移备份（v2 步骤 5）")
    p_clean.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    p_clean.set_defaults(func=cmd_cleanup)

    args = ap.parse_args()
    if args.cmd is None:
        args.cmd = "status"
        args.func = cmd_status
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
