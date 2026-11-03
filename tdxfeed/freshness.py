"""tdxfeed/freshness.py — 数据新鲜度校验（阶段入口守卫）

目的：在每个 stage CLI 入口（predict / plan_e_backtest / plan_e_journal / plan_e_sim_live）
开头跑一次快速抽样检查，若大量样本末日期 < 阈值则 `sys.exit(2)` 阻断。

为什么抽样而非全量：parquet 文件 8000+ 个，全量**整列读**每次 ~10s，会显著拖慢每次调用；
抽样 50 个文件足够反映整体新鲜度，<100ms。

但中位数抽样答不了「有没有个别标的掉队」——某一只票整表停更时中位数纹丝不动
（台账 KI-OPS-005）。那条问题由 `table_freshness()` 全量逐文件回答：它走 footer 列统计，
实测单表 5,200+ 文件 0.2 秒级，不是这里记的 ~10s 量级（见其 docstring 的成本实测）。

阈值默认 2 天（覆盖周末），可按需上调。

用法（在 CLI 脚本 main() 入口）：
    from tdxfeed.freshness import require_fresh
    require_fresh(max_age_days=2)  # 缺数据则 sys.exit(2)

用法（巡检维度，全量尾部）：
    from tdxfeed.freshness import table_freshness
    table_freshness(files, date.today(), allowed_trading_days=1)

CLI bypass：
    --no-freshness-check
"""
from __future__ import annotations

import datetime as _dt
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tdxfeed.execution.limits import normalize_code
from tdxfeed.paths import daily_dir

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

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
        _ERROR_COUNT['_last_date_from_parquet'] += 1
        return None
    if df.empty:
        return None
    d = df["date"].iloc[-1]
    # int64 YYYYMMDD 漂移（已知）：先 to_datetime str
    if isinstance(d, (int, np.integer)) and not isinstance(d, bool):
        return pd.to_datetime(str(int(d)), format="%Y%m%d")
    return pd.to_datetime(d)


def _decode_date_stat(value: object, logical_type: str) -> pd.Timestamp | None:
    """把 parquet row-group 统计里的 date 上界解码成 Timestamp；认不出来返回 None。

    返回 None 不是「文件坏了」，而是「这条统计我不认」——调用方会回退到
    `_last_date_from_parquet` 整列读末行，所以 int64 YYYYMMDD 之类的漂移仍由
    那一份既有实现负责，不在这里重复一套解析规则。
    """
    if value is None:
        return None
    # datetime 是 date 的子类，必须先判 datetime
    if isinstance(value, (pd.Timestamp, _dt.datetime)):
        return pd.Timestamp(value)
    if isinstance(value, _dt.date):
        return pd.Timestamp(value)
    if isinstance(value, str):
        try:
            return pd.to_datetime(value)
        except (ValueError, TypeError):
            _ERROR_COUNT['_decode_date_stat'] += 1
            return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        # date32/date64 的物理类型是 INT32，统计值是 epoch days。
        # pyarrow ≥ 某版会替我们解成 datetime.date，这里只兜住它没解的情况。
        if logical_type.startswith(("date32", "date64")):
            return pd.Timestamp(int(value), unit="D")
        return None
    return None


def _max_date_from_parquet_stats(fp: Path) -> pd.Timestamp | None:
    """只读 footer（元数据 + 各 row group 的列统计）取 date 列上界的最大值。

    为什么不用 `_last_date_from_parquet` 做全表扫：整列读实测 daily 5,232 只
    21.3 秒、hfq_factor 5,221 只 21.6 秒（约 4.1 ms/只），超出 20 秒预算；footer
    统计实测同两张表分别是 0.19 秒与 0.17 秒（约 0.04 ms/只，快 ~110 倍）。
    canary 每 5 分钟跑一次、且 `TimeoutStartSec=120`，21 秒×N 张表会直接把巡检拖爆。

    取舍（必须知道的三点语义差）：
      1. 这里取的是**最大值**，`_last_date_from_parquet` 取的是**末行**。生产表
         （daily / adj_factor / hfq_factor / derived）按日期升序追加写，两者逐文件
         实测一致（全表 min/max 两法完全相同）；若某表末行不是最大日期，"表有没有
         前进"这个问题按最大值回答也更对。
      2. 统计是按 row group 存的，必须**遍历全部 row group** 取最大——只读第一个
         会把一只 2020 年起、末段在 2031 年的表读成 2020 年。
      3. 统计缺失（写文件时 `write_statistics=False`）或值不认识 → 返回 None，
         调用方整列读回退；真实数据 daily/hfq 各全表 0 只需要回退。
    """
    try:
        pf = pq.ParquetFile(fp)
        schema = pf.schema_arrow
        idx = schema.get_field_index("date")
        if idx < 0:
            return None
        logical = str(schema.field(idx).type)
    except Exception:
        # 读不出 footer 就交给整列读那条路径去计吞错，这里不重复计数
        return None
    try:
        n_rg = pf.num_row_groups
        if n_rg == 0:
            return None
        best: pd.Timestamp | None = None
        for rg in range(n_rg):
            stats = pf.metadata.row_group(rg).column(idx).statistics
            if stats is None:
                return None
            ts = _decode_date_stat(stats.max, logical)
            if ts is None:
                return None
            if best is None or ts > best:
                best = ts
        return best
    except Exception:
        return None


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
    stem = normalize_code(stem)
    # 北交所 bj920xxx / 8xxxxx 不属于 A 股个股（流动性差、30% 涨跌停）
    if stem.startswith(("920", "8")):
        return False
    return stem.startswith(_A_SHARE_STOCK_PREFIXES)


def sample_latest_date(files, sample_size: int = 30, seed: int = 42) -> date | None:
    """抽样读 parquet 的末日期，返回中位数日期（None 表示没有可读文件）。

    用中位数而不是「最旧/最新」：表停更时几乎所有文件都停在同一天，中位数能反映
    整体状态；个别文件坏掉或本就滞后的标的不会把整张表误判为陈旧。
    """
    files = [Path(f) for f in files]
    if not files:
        return None
    rng = np.random.default_rng(seed)
    # 先转 object 数组再 choice：直接对 Path 列表 choice 会被 numpy 当成字符串/对象
    # 推断，长度不同的路径会造出奇怪的定长 dtype。
    pool = np.array(files, dtype=object)
    sample = rng.choice(pool, min(sample_size, len(pool)), replace=False)
    dates = [d.date() for d in (_last_date_from_parquet(Path(f)) for f in sample) if d is not None]
    if not dates:
        return None
    dates.sort()
    return dates[len(dates) // 2]


def stale_threshold(ref: date, allowed_trading_days: int = 3,
                    holidays: set[str] | None = None) -> date:
    """晚于（<）该日期即视为陈旧：最近已收盘交易日再往前回退 N 个交易日。

    真陈旧判据是 `latest < limit`；末日期正好等于 limit 不算陈旧。
    """
    hol = holidays if holidays is not None else CN_HOLIDAYS
    return _subtract_trading_days(last_trading_day(ref, hol), allowed_trading_days, hol)


def table_freshness(files, ref_date: date, allowed_trading_days: int = 1,
                    holidays: set[str] | None = None) -> dict:
    """全量逐文件末日期 → 尾部新鲜度统计（KI-OPS-005：中位数抽样看不见单只停更）。

    与 `sample_latest_date` 的分工：抽样中位数回答「整张表有没有前进」（快、<100ms，
    stage 入口守卫用），本函数回答「有没有个别标的掉队」（全量、单表实测 0.2 秒级）。
    两者互不替代，本函数不改变 `sample_latest_date` 的行为。

    参数
      files               待统计的文件（Path 或 str 可迭代）；**过滤由调用方负责**
                          （A 股个股判定见 `_is_a_share_stock`，迁移备份见 ".bak" 排除），
                          这样同一份实现能同时服务 daily / adj_factor / hfq_factor。
      ref_date            新鲜度基准日（一般是今天）；阈值 = 该日往前
                          「最近已收盘交易日 - allowed_trading_days 个交易日」。
      allowed_trading_days 容忍回退的交易日数，默认 1：baostock 当日 EOD 晚间才发布，
                          因子表与派生表结构性只能追到 T-1，那是正常态不是故障。

    返回 {total, parsed, latest, oldest, stale_count, stale_ratio, threshold, examples}
      total / parsed   文件总数 / 成功读出末日期的文件数。解析失败率 = 1 - parsed/total，
                       属**另一维**（覆盖率），不进 stale_count：一个读不出日期的文件没有
                       可比较的日期，把它同时算成「停更」就是同一件事告警两遍。
      latest/oldest    成功读出文件的最大/最小末日期（date | None）。
      threshold        `stale_threshold(ref_date, allowed_trading_days, holidays)` 的返回值；
                       末日期 **<** threshold 才计陈旧（等于 threshold 是 T-1 正常态）。
      stale_count      陈旧文件数。
      stale_ratio      stale_count / **total**（分母含读不出日期的文件；total=0 时 0.0）。
      examples         最滞后的 ≤5 个代码，按 (末日期升序, 文件名) 排序，最严重的在最前。
                       形态是文件名去掉 `.parquet`（保留 sh/sz/bj 前缀）：daily 里
                       000001/000016/000688 等同码双文件（指数 vs 个股），normalize 后
                       会撞车，所以这里不剥前缀。

    成本纪律：全量扫必须实测。默认走 footer 统计（见 `_max_date_from_parquet_stats`），
    统计缺失或不认识才回退整列读；整列读实测单表 ~21 秒，超 20 秒预算，所以它只能是回退。
    """
    hol = holidays if holidays is not None else CN_HOLIDAYS
    threshold = stale_threshold(ref_date, allowed_trading_days, hol)

    total = 0
    parsed = 0
    dates: list[date] = []
    stale: list[tuple[date, str]] = []
    for raw in files:
        fp = Path(raw)
        total += 1
        last = _max_date_from_parquet_stats(fp)
        if last is None:
            last = _last_date_from_parquet(fp)      # 回退：整列读末行（慢，但认 YYYYMMDD）
        if last is None:
            continue
        parsed += 1
        d = last.date()
        dates.append(d)
        if d < threshold:
            stale.append((d, fp.stem))

    stale.sort()
    return {
        "total": total,
        "parsed": parsed,
        "latest": max(dates) if dates else None,
        "oldest": min(dates) if dates else None,
        "stale_count": len(stale),
        "stale_ratio": (len(stale) / total) if total else 0.0,
        "threshold": threshold,
        "examples": [name for _, name in stale[:5]],
    }


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


__all__ = ["last_trading_day", "require_fresh", "sample_latest_date",
           "stale_threshold", "table_freshness"]
