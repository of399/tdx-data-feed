#!/usr/bin/env python
"""一次性回填 daily parquet 的 amount 缺口（Sina 日 K 无成交额）。

口径：amount 缺失（<=0）且 volume>0 时，amount = close × volume(手) × 100。
已有真实成交额保持不变；volume=0 的停牌日保持 0。

安全约定（2026-10-26 加固）：
- **默认 dry-run**：只统计不写盘；真正落盘必须显式加 `--apply`。
- 落盘前逐文件备份到 `data/parquet/daily_amount_bak/`（`<code>.amountfix.bak.parquet`）；
  备份目录不在 `daily/` 内，不会干扰 canary 的日K 计数与各脚本的 `.bak` 排除逻辑。

用法：
    venv/bin/python scripts/backfill_daily_amount.py                # dry-run（默认）
    venv/bin/python scripts/backfill_daily_amount.py --apply        # 落盘（先备份后写）
    venv/bin/python scripts/backfill_daily_amount.py --apply --no-day
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.amount_fix import backfill_daily_dir  # noqa: E402
from tdxfeed.paths import DATA_ROOT, daily_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 daily parquet 成交额缺口（默认 dry-run）")
    ap.add_argument("--daily-dir", default=str(daily_dir()))
    ap.add_argument("--out-root", default=str(DATA_ROOT))
    ap.add_argument("--backup-dir", default=str(daily_dir().with_name("daily_amount_bak")),
                    help="落盘前的原文件备份目录")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--start", type=int, default=20260916,
                    help="仅补该日及之后（YYYYMMDD，默认 20260916）")
    ap.add_argument("--apply", action="store_true",
                    help="真正落盘（默认只统计，不写盘）")
    ap.add_argument("--no-day", action="store_true", help="不重建 .day 文件")
    args = ap.parse_args()

    stats = backfill_daily_dir(
        args.daily_dir,
        dry_run=not args.apply,
        update_day=not args.no_day,
        workers=args.workers,
        out_root=args.out_root,
        start_date=args.start,
        backup_dir=Path(args.backup_dir) if args.apply else None,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["n_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
