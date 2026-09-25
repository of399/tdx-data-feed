# tdx-data-feed 数据字典

> A股/ETF/转债/指数 日K 数据管道 · 数据口径与格式说明
> 更新：2026-09-16（含 volume 单位治理口径）

## 1. 数据范围

| 类型 | 覆盖 | 代码段 |
|---|---|---|
| 沪深A股 | 全量（akshare 清单 5562） | sh: 60/68；sz: 00/30 |
| ETF | 全量（1611） | sh: 51/58/56/52/53；sz: 15/16/18 |
| 可转债 | 有效段（110/113/123/128） | sh: 110/113；sz: 123/128 |
| 指数 | 核心指数 | sh000001/sz399001 等 |

## 2. 目录结构

```
data/
├── sh/lday/            # 上交所 .day 二进制（vipdoc 格式）
│   └── sh600000.day
├── sz/lday/            # 深交所 .day 二进制
│   └── sz000001.day
├── parquet/daily/      # 通用表 Parquet（v4 训练原料）
│   └── sh600000.parquet
├── .state/
│   └── manifest.json   # 增量同步状态（last_date / 根数）
└── v4_sample.json      # 训练样本示例
```

## 3. .day 二进制格式（32 字节/条，小端）

```
偏移  类型    字段          说明
0    int32   date          YYYYMMDD，如 20260915
4    int32   open×100      开盘价×100（如 9.18 → 918）
8    int32   high×100      最高价×100
12   int32   low×100       最低价×100
16   int32   close×100     收盘价×100
20   float32 amount        成交额（元）
24   int32   volume        成交量（手）
28   int32   reserved      保留（0）
```

## 4. Parquet 列定义

| 列 | 类型 | 说明 | 单位 |
|---|---|---|---|
| date | date | 交易日 | — |
| open | float64 | 开盘价 | 元 |
| high | float64 | 最高价 | 元 |
| low | float64 | 最低价 | 元 |
| close | float64 | 收盘价 | 元 |
| volume | float64 | 成交量 | **手**（股÷100；历史混用残留见 §5） |
| amount | float64 | 成交额 | 元（日K 历史数据为 0，5分钟有值） |

## 5. 单位约定（含治理口径）

- **价格**：元，保留 2 位小数（.day 中×100 存 int32）
- **volume（parquet）**：**手**（1 手 = 100 股）为主，**历史混用残留已知**。
  - 2026-09-16 治理：275 只原"股"单位标的曾对 45 只全历史一致者 ÷100 统一手，复验发现按年度中位数判定会掩盖个别手日（sh601112 20260506 被误除两次），**已回滚恢复原值**；
  - **218 只按时间混用残留**（如 sh600930：2025=手、2026=股）——parquet 层不做全局统一，**v4 已用 amount 物理校验逐行修正**（复验 v4 与 parquet 原值单位化 100% 一致）；
  - ETF volume 单位为**份**（1 手 = 100 份）。
- **volume（v4 训练集）**：**股**（手×100 或股保持，amount 物理校验判定，bad=0）
- **amount**：元。Sina 日K 接口不返回 amount，历史数据填 0；5分钟 K 线有值
- **复权**：前复权（Sina 默认）

**amount 物理校验规则**：ratio = amount / (volume×close)；∈(30,300)=手、∈(0.3,3)=股。
**增量防护**：merge_write 已加单位一致性检测，新旧 volume 量级差 >50× 打印 `[单位告警]`。

## 6. manifest.json

```json
{"sh600000": {"last_date": "20260915", "count": 1023}}
```
- `last_date`：最新交易日（YYYYMMDD 字符串）
- `count`：已存 K 线根数

## 7. 数据源与更新

| 环节 | 来源 | 说明 |
|---|---|---|
| 证券清单 | akshare `stock_info_a_code_name`（白天可用） | pytdx 兜底 |
| 日K 数据 | Sina `quotes.sina.cn getKLineData`（白天稳定） | 单次≤1023 根≈4年 |
| 历史回填 | akshare `stock_zh_a_daily`（Sina 底层） | 全历史 1999 起 |
| 增量调度 | systemd timer `tdx-sync.timer`（工作日 16:30） | 执行 `full_sync` |

## 8. 已知限制

- Sina 单次返回上限 1023 根（≈4 年），更早历史需回填脚本
- 腾讯源白天 WAF 501 风控（仅凌晨可用），作为回补备选
- pytdx 行情数据层返回 0 根（仅连接层可用作清单）
- 218 只标的 parquet volume 存在按时间混用（手/股交替），v4 层已逐行修正（复验 100% 一致），parquet 层为已知残留；45 只曾统一已回滚

## 9. 使用示例（Python）

```python
import pandas as pd
df = pd.read_parquet("data/parquet/daily/sh600000.parquet")
# 最近 20 个交易日收盘（volume 单位为手）
print(df.tail(20)[["date", "close", "volume"]])
```
