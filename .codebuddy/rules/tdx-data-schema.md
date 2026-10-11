---
description: tdx-data-feed 数据 schema 锁定。任何数据变更必须先评估兼容性。parquet 字段名 / 类型 / 顺序不可改。
alwaysApply: true
enabled: true
updatedAt: 2026-09-27T10:35:00.000Z
---

<system_reminder>
TDX data schema is FROZEN. Do not modify parquet schemas.

## parquet schema 详情

### `data/parquet/daily/{code}.parquet`

```
date           date32       # 交易日
open           float64      # 开盘价（前复权）
high           float64      # 最高价（前复权）
low            float64      # 最低价（前复权）
close          float64      # 收盘价（前复权）
volume         float64      # 成交量（股）
amount         float64      # 成交额（元）
adj_factor     float64      # 复权因子
```

### `data/adj_factor/{code}.parquet`

```
date           date32
adj_factor     float64      # 复权因子（累积）
```

### `data/double_up/double_up_2020_2026_detail.csv`

| 字段 | 类型 | 备注 |
|---|---|---|
| code | string | 6 位代码（无前缀）|
| start_date | date | 起点（YYYY-MM-DD）|
| end_date | date | 终点 |
| peak_date | date | **首次触发翻倍 100% 累计涨幅的日期** |
| peak_pct | float | 最大累计涨幅（如 1.50 = 150%）|
| hold_days | int | 持仓天数 |

### `data/double_up/d_signal_5d_features.csv`

> 方案 D 训练样本：18 维特征 + label

| 列 | 含义 |
|---|---|
| code | 6 位代码 |
| signal_date | 信号日（peak_date）|
| ret_5d | 5 日累计涨幅 |
| ret_20d | 20 日累计涨幅 |
| vol_ratio_t1 | T+1 量比 |
| pct_t1 | T-1 单日涨幅 |
| ... | 共 18 维 |
| label | 是否 5 日内再涨 30%+（0/1）|

## 不可破坏原则

1. **不删除** 已有 parquet 文件
2. **不修改** 字段名 / 字段类型 / 字段顺序
3. **不引入** 新必填字段（必须可空 + 默认值）
4. **不在 parquet** 中存字符串时间戳

## 增量更新规则

```python
def incremental_update(code: str):
    fp = Path(f'data/parquet/daily/{code}.parquet')
    if not fp.exists():
        return download_full(code)

    df_old = pd.read_parquet(fp)
    last_date = df_old['date'].max()

    df_new = download_range(code, start=last_date + 1)
    df_all = pd.concat([df_old, df_new]).drop_duplicates('date').sort_values('date')

    # Schema 校验
    assert list(df_all.columns) == ['date','open','high','low','close','volume','amount','adj_factor']

    df_all.to_parquet(fp, index=False, engine='pyarrow')
```

## 字段变更流程（必须走）

1. 在 `.codebuddy/rules/` 创建新的 schema 提案（如 `tdx-data-schema-v2.md`）
2. 给所有相关 Agent 发通知（description 加 `use PROACTIVELY`）
3. 提供数据迁移脚本（`scripts/migrate_v1_to_v2.py`）
4. 双写期：新 schema + 旧 schema 共存 30 天
5. 切流 + 删旧数据

## 不兼容变更 = 禁止

- ❌ 改字段名（即使语义相同）
- ❌ 改字段类型（如 float64 → int64）
- ❌ 改 parquet 文件名（即使含义相同）
- ❌ 删除历史日期数据
- ❌ 修改 `peak_date` 计算口径

如必须做上述变更，请走完整流程。
</system_reminder>
