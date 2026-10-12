"""tdxfeed/freshness.py — 数据新鲜度校验（阶段入口守卫）

目的：在每个 stage CLI 入口（predict / plan_e_backtest / plan_e_journal / plan_e_sim_live）
开头跑一次快速抽样检查，若大量样本末日期 < 阈值则 `sys.exit(2)` 阻断。

为什么抽样而非全量：parquet 文件 8000+ 个，全量扫每次 ~10s，会显著拖慢每次调用；
抽样 50 个文件足够反映整体新鲜度，<100ms。

阈值默认 2 天（覆盖周末），可按需上调。

用法（在 CLI 脚本 main() 入口）：
    from tdxfeed.freshness import require_fresh
    require_fresh(max_age_days=2)  # 缺数据则 sys.exit(2)

CLI bypass：
    --no-freshness-check
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")


def _last_date_from_parquet(fp: Path) -> pd.Timestamp | None:
    """读 parquet 的 date 列最后一行的日期。"""
    try:
        df = pd.read_parquet(fp, columns=["date"])
    except Exception:
        return None
    if df.empty:
        return None
    d = df["date"].iloc[-1]
    # int64 YYYYMMDD 漂移（已知）：先 to_datetime str
    if isinstance(d, (int, np.integer)) and not isinstance(d, bool):
        return pd.to_datetime(str(int(d)), format="%Y%m%d")
    return pd.to_datetime(d)


def require_fresh(parquet_dir: Path = DEFAULT_PARQUET_DIR, max_age_days: int = 2, sample_size: int = 50) -> None:
    """抽样校验数据新鲜度，若过期则 sys.exit(2) 阻断。

    阈值语义：抽样中超过 20% 末日期 < today - max_age_days 则视为过期。
    """
    if not parquet_dir.exists():
        print(f"❌ parquet 目录不存在: {parquet_dir}", file=sys.stderr)
        print("  请先跑 full_sync: nohup venv/bin/python -m tdxfeed.full_sync &", file=sys.stderr)
        sys.exit(2)

    files = list(parquet_dir.glob("*.parquet"))
    if not files:
        print(f"❌ parquet 目录为空: {parquet_dir}", file=sys.stderr)
        sys.exit(2)

    rng = np.random.default_rng(42)
    sample = list(rng.choice(files, min(sample_size, len(files)), replace=False))
    threshold_date = datetime.now().date() - timedelta(days=max_age_days)

    stale = 0
    parsed = 0
    for f in sample:
        last = _last_date_from_parquet(Path(f))
        if last is None:
            stale += 1
            continue
        parsed += 1
        if last.date() < threshold_date:
            stale += 1

    pct_stale = stale / len(sample) * 100
    if pct_stale > 20:
        print(
            f"❌ 数据新鲜度不达标：抽样 {len(sample)} 只，{pct_stale:.0f}% 末日期早于 {threshold_date}",
            file=sys.stderr,
        )
        print(f"  parsed_ok={parsed}/{len(sample)} 阈值={max_age_days}d  请先跑 full_sync", file=sys.stderr)
        sys.exit(2)
    print(
        f"  ✅ 新鲜度通过：{len(sample) - stale}/{len(sample)} 末日期 ≥ {threshold_date}（阈值 {max_age_days}d）",
        flush=True,
    )


__all__ = ["require_fresh"]
