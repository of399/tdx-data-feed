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

from tdxfeed.paths import daily_dir  # noqa: E402

DEFAULT_PARQUET_DIR = daily_dir()

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


# A 股个股代码前缀集合（与 quant.multifactor.factors.is_stock_code 对齐）。
# 不含：159/51/56 段 ETF/LOF/基金、920/8 段北交所（流动性差、不在 full_sync 范围）。
_A_SHARE_STOCK_PREFIXES: tuple[str, ...] = (
    "600", "601", "603", "605",   # 沪市主板
    "688",                       # 沪市科创板
    "900",                       # 沪市 B 股
    "000", "001", "002", "003",   # 深市主板
    "300", "301",                 # 深市创业板
    "200",                       # 深市 B 股
)


def _is_a_share_stock(filename: str) -> bool:
    """判定文件名（形如 sh600000.parquet / 600000.parquet / bj920026.parquet）
    是否属于 full_sync 覆盖的 A 股个股 universe。"""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # 去除路径与后缀
    # 去除可能的市场前缀
    for prefix in ("sh", "sz", "bj"):
        if stem.lower().startswith(prefix):
            stem = stem[2:]
            break
    # 北交所 bj920xxx / 8xxxxx 不属于 A 股个股（流动性差、30% 涨跌停）
    if stem.startswith(("920", "8")):
        return False
    return stem.startswith(_A_SHARE_STOCK_PREFIXES)


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

    # ⚠ 必须排除 .bak 迁移备份（v1→v2 期间遗留的 <code>.v1.bak.parquet）：
    # 这些是 5+ 天前的 v1 schema 快照，date 列是 int YYYYMMDD（合法但末日期停滞），
    # 混入抽样会让 require_fresh 误判 ~42% 过期（5232/7231）。
    # 与 scripts/predict.py:419 / scripts/check_schema.py:37-38 保持一致。
    # 进一步限定到 A 股个股 universe（predict 实际使用的范围）：
    # ETF/LOF/基金（159/51/56 开头）与北交所（920/8 开头）不在 full_sync 的
    # akshare 清单中、长期停留在 9-14 末日期，会污染抽样。
    files = [p for p in parquet_dir.glob("*.parquet")
             if ".bak" not in p.name and _is_a_share_stock(p.name)]
    if not files:
        print(f"❌ parquet 目录无活跃 A 股个股数据: {parquet_dir}", file=sys.stderr)
        print("  请先跑 full_sync: nohup venv/bin/python -m tdxfeed.full_sync &", file=sys.stderr)
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
