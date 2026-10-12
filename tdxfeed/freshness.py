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
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")

# 已知 A 股休市日（按交易所年度公告补充；示例含 2026 国庆调休）。
# 仅用于"豁免休市日"，缺失会导致该日之后首日仍按周末逻辑少算一天，偏保守（放行）。
CN_HOLIDAYS: set[str] = {
    "2026-01-01", "2026-01-02",
    "2026-04-06", "2026-04-07",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}

# 可替换的"今天"入口（便于单元测试 monkeypatch）
def _today() -> date:
    return date.today()


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


def last_trading_day(ref: date, holidays: set[str] | None = None) -> date:
    """返回 ref 之前（不含 ref 当日）最近的已收盘交易日。

    保守地跳过周末与已知休市日，因此即使盘前运行也不会要求当日数据。
    周一 → 上一个周五；节假日次日 → 跳过该休市日。
    """
    holidays = holidays if holidays is not None else CN_HOLIDAYS
    d = ref
    while True:
        d -= timedelta(days=1)
        if d.weekday() >= 5:        # 周六(5) / 周日(6)
            continue
        if d.isoformat() in holidays:
            continue
        return d


def _subtract_trading_days(ref: date, n: int, holidays: set[str]) -> date:
    """从 ref 向前回退 n 个已收盘交易日（跳过周末与休市）。"""
    d = ref
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if d.isoformat() in holidays:
            continue
        n -= 1
    return d


def require_fresh(parquet_dir: Path = DEFAULT_PARQUET_DIR, max_age_days: int = 2,
                  sample_size: int = 50, allowed_stale_trading_days: int | None = None,
                  holidays: set[str] | None = None) -> None:
    """抽样校验数据新鲜度，若过期则 sys.exit(2) 阻断。

    交易日感知：阈值基准取「最近一个已收盘交易日」，而非今天（日历日），
    从而避免周一/节假日因周末不交易被误杀。允许落后天数默认沿用 max_age_days，
    也可显式传入 allowed_stale_trading_days（按交易日回退，跳过周末/休市）。
    """
    if not parquet_dir.exists():
        print(f"❌ parquet 目录不存在: {parquet_dir}", file=sys.stderr)
        print("  请先跑 full_sync: nohup venv/bin/python -m tdxfeed.full_sync &", file=sys.stderr)
        sys.exit(2)

    files = list(parquet_dir.glob("*.parquet"))
    if not files:
        print(f"❌ parquet 目录为空: {parquet_dir}", file=sys.stderr)
        sys.exit(2)

    if holidays is None:
        holidays = CN_HOLIDAYS
    allowed = allowed_stale_trading_days if allowed_stale_trading_days is not None else max_age_days
    base = last_trading_day(_today(), holidays)
    threshold_date = _subtract_trading_days(base, allowed, holidays)

    rng = np.random.default_rng(42)
    sample = list(rng.choice(files, min(sample_size, len(files)), replace=False))

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
        print(f"  parsed_ok={parsed}/{len(sample)} 最近交易日={base} 阈值={threshold_date}  请先跑 full_sync", file=sys.stderr)
        sys.exit(2)
    print(
        f"  ✅ 新鲜度通过：{len(sample) - stale}/{len(sample)} 末日期 ≥ {threshold_date}（最近交易日 {base}）",
        flush=True,
    )


__all__ = ["last_trading_day", "require_fresh"]
