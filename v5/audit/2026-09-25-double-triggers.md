# 2026-09-25 候选股票清单 (20 日涨幅 ≥ 100%)

> 来源: tdx-data-feed/data/parquet/daily/*.parquet (8035 只股票)
> 算法: rolling 20 日 max(high) / min(low) - 1 ≥ 1.0
> 时间范围: 2001-01-01 ~ 2026-09-24
> 一键重跑: `./venv/bin/python v5/scripts/scan_double.py --out-prefix v5/audit/double_triggers`

## 数据来源

| 数据 | 路径 |
|---|---|
| OHLCV 日线 | `data/parquet/daily/*.parquet` |
| 复权因子 | `data/adj_factor/*.parquet` (仅 2020+, 未使用) |
| 名称映射 | `data/all_a_stocks.json` |
| 行业 / IPO / 状态 | baostock.query_stock_industry + query_stock_basic |

## 计算窗口与复权

- **窗口**: 连续 20 个交易日
- **复权**: raw close (未复权, adj_factor 仅 2020+)
- **跨度约束**: start → end 自然日 ≤ 60 (20 交易日 ≈ 30 自然日 + 长假 buffer)
- **端点约束**: low_min_idx ≤ high_max_idx (起涨点早于顶点)
- **ST**: code_name 以 ST/*ST 开头
- **次新股**: 触发日距 IPO ≤ 60 自然日

## 字段 (19 列)

`symbol, stock_name, industry, market, start_date, end_date, start_price, end_price, range_pct, win_days, span_days, win_low_sum_vol, win_amount, avg_amount, is_st, is_new, has_zero_vol, ipo_dt, days_since_ipo, status`

## 统计

| 项 | 值 |
|---|---|
| 总 trigger | 39,979 |
| 候选股票 (去重) | 3,727 |
| 总耗时 | ~50s (scan) + ~60s (enrich) |
| 市场分布 | SZ 22,187 / SH 13,215 / BJ 4,577 |
| ST 触发 | 2,023 |
| 次新股触发 | 7,383 |
| 极端涨幅 | bj920489 28614% (新三板早期) |

## 输出文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `double_triggers_raw.csv` | 39,979 | 原始 triggers |
| `double_triggers_enriched.csv` | 39,979 | enriched |
| `double_triggers_candidate.csv` | 3,727 | 每只股票最早 trigger |

## 已知局限

1. 换手率: 缺流通股本, 用 `avg_amount = amount/20` 代理
2. 停牌检测: `volume==0` 近似, 不准
3. 复权不连续: 2020 前 raw close 有除权跳变, 但 20 日 max/min 平滑了
4. 行业仅证监会分类: 缺 SW / Wind
5. akshare 在线失败: 无法补 free_float / SW 分类

## 重新生成

```bash
cd /home/jiuben/tdx-data-feed
./venv/bin/python v5/scripts/scan_double.py --out-prefix v5/audit/double_triggers
# 加 --limit N 测试 / --no-enrich 跳过 enrich
```
