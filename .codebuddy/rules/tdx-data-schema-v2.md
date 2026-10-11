---
description: tdx-data-feed 数据 schema v2 提案。从 v1（已实际漂移）升级到 v2（统一 date32 + 显式 5 列 adj_factor）。实施需走完整变更流程。
alwaysApply: false
enabled: true
updatedAt: 2026-09-27T11:10:00.000Z
supersedes: tdx-data-schema.md
status: PROPOSAL
---

<system_reminder>
TDX data schema v2 提案（基于现状逆向对齐）。

## 背景

v1 (`tdx-data-schema.md`) 规定与现状已发生 **三处漂移**：

| 字段 | v1 规定 | 现状实测 |
|---|---|---|
| `daily.date` 类型 | `date32` | 三态：`int64 (5,221) / timestamp[us] (2,469) / timestamp[ns] (345)` |
| `daily.adj_factor` 列 | `存在 float64` | **不存在**（7 列而非 8 列） |
| `adj_factor` schema | 2 列：`date, adj_factor` | **5 列**：`code, date, adj_factor, qfq_close, raw_close` |

下游 `predict.py` / `plan_e_backtest.py` 已通过 `pd.to_datetime` 自适应 date 类型，并通过 `merge` 独立取 `adj_factor`。
但 `find_double_up_c_v2.py` / `top10_deep_dive.py` 等老脚本 `to_datetime(format='%Y%m%d')` 假定 int 字符串，对 timestamp 类型会失败。
**`incremental_update` 在代码库零引用**，是未落地的愿景。

## v2 正式 schema

### `data/parquet/daily/{code}.parquet`

```
date           date32       # 交易日（统一）
open           float64      # 开盘价（前复权）
high           float64      # 最高价（前复权）
low            float64      # 最低价（前复权）
close          float64      # 收盘价（前复权）
volume         float64      # 成交量（股）
amount         float64      # 成交额（元）
```

> **注**：`adj_factor` **不内嵌**于 daily，**独立存于** `data/adj_factor/{code}.parquet`。
> 下游通过 `predict.py` merge 方式拉取；本规则与现状一致，不再要求 daily 含 adj_factor 列。

### `data/adj_factor/{code}.parquet`（正式 5 列）

```
code           string       # 6 位代码（零填充）
date           date32       # 交易日
adj_factor     float64      # 复权因子（累积，= qfq_close / raw_close）
qfq_close      float64      # 前复权收盘价（baostock adjustflag=2）
raw_close      float64      # 不复权收盘价（baostock adjustflag=3）
```

> v1 文档规定 2 列，与实现不符；v2 以实现为准，并保留 `qfq_close/raw_close` 用于：
> - 调试核验：`adj_factor = qfq_close / raw_close`
> - 后复权 hfq 计算预留：`raw_close` 是基准价。

### `data/double_up/double_up_2020_2026_detail.csv`

与 v1 一致（不漂移）：

| 字段 | 类型 | 备注 |
|---|---|---|
| code | string | 6 位代码（无前缀）|
| start_date | date | 起点（YYYY-MM-DD）|
| end_date | date | 终点 |
| peak_date | date | **首次触发翻倍 100% 累计涨幅的日期** |
| peak_pct | float | 最大累计涨幅（如 1.50 = 150%）|
| hold_days | int | 持仓天数 |

### `data/double_up/d_signal_5d_features.csv`

与 v1 一致（18 维特征 + label）。

## 不可破坏原则

1. **不删除** 已有 parquet 文件（迁移时备份为 `.v1.bak.parquet`）
2. **不修改** 字段名（v2 与 v1 列名一致）
3. **不引入** 新必填字段（保持列数与顺序不变：daily 7 列、adj_factor 5 列）
4. **不破坏** 历史日期数据（迁移只换列类型，不动 row 内容）

## 增量更新规则（v2 落地版）

```python
# scripts/incremental_update.py — 由 tdxfeed.full_sync 调用
def incremental_update(symbol: str) -> int:
    """读末日期 +1 起拉增量 → 合并写入。
    返回新增条数；0 = 无更新；-1 = 无文件（首次全量）。

    数据源优先级：Sina → akshare → Tencent（同 full_sync）。
    """
    fp = PARQUET / f"{symbol}.parquet"
    if not fp.exists():
        return -1  # 触发首次全量

    df_old = pd.read_parquet(fp)
    last_date = pd.to_datetime(df_old['date'].max()).date()
    start = (last_date + timedelta(days=1)).isoformat()

    bars = fetch_range(symbol, start=start, end=today())
    if not bars:
        return 0

    new = pd.DataFrame(bars, columns=['date','open','high','low','close','volume','amount'])
    merged = pd.concat([df_old, new], ignore_index=True)
    merged['date'] = pd.to_datetime(merged['date'], errors='coerce')
    merged = merged.dropna(subset=['date','close'])
    merged = merged.drop_duplicates(subset=['date'], keep='last')
    merged = merged.sort_values('date').reset_index(drop=True)
    # amount 保护：同日取 max
    merged['amount'] = merged.groupby('date')['amount'].transform('max')

    # Schema 校验（v2）
    assert list(merged.columns) == ['date','open','high','low','close','volume','amount'], \
        f"列不匹配: {list(merged.columns)}"
    write_daily_v2(fp, merged)  # date → date32
    return len(merged) - len(df_old)
```

要点：
- **列数 7 列严格不变**（不加 adj_factor）
- **date 强制 date32**（write_daily_v2 内部 cast）
- **amount 同日 max**（防 Sina amount=0 覆盖真实值）
- **去重 keep='last'**（新数据优先）

## 字段变更流程（v2 完整 5 步）

| 步骤 | 动作 | 产物 |
|---|---|---|
| 1 | 创建本提案 | `.codebuddy/rules/tdx-data-schema-v2.md` ✅ |
| 2 | 给所有 Agent 发通知 | 在 `.codebuddy/agents/*.md` description 加 `use PROACTIVELY when v2` |
| 3 | 数据迁移脚本 | `scripts/migrate_v1_to_v2.py`（默认 dry-run；`--apply` 真正迁移） |
| 4 | 双写期：新旧 schema 共存 30 天 | 原文件备份 `.v1.bak.parquet`；新文件 `.v2.parquet`（或原位覆盖） |
| 5 | 切流 + 删旧数据 | 30 天后检查 `.v1.bak.parquet` 无引用，执行删除 |

## 不兼容变更 = 禁止

- ❌ 改字段名（即使语义相同）
- ❌ 改字段类型（v1→v2 是例外，但仅限 daily.date date32 化 + 列数严格不变）
- ❌ 改 parquet 文件名（即使含义相同）
- ❌ 删除历史日期数据
- ❌ 修改 `peak_date` 计算口径
- ❌ 在 daily 中加 adj_factor 列（v2 维持 7 列）

如必须做上述变更，请走完整流程。
</system_reminder>
