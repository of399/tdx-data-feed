# API.md · tdx-data-feed OpenAPI 风格 API 文档

> 本文是 **OpenAPI 风格** 的 API 文档，覆盖所有脚本的 CLI 接口。每个接口包含脚本名称、功能描述、命令语法、参数列表（名称、类型、是否必填、默认值、说明）、输入输出格式、退出码含义、使用示例，以及常见错误与异常说明。
>
> 格式约定：所有 CLI 接口统一视为 `Operation`，每个 Operation 有 `operationId` / `summary` / `parameters` / `responses` / `errors`。
>
> 最后更新：2026-09-27 · 维护者：jiuben

---

## 目录

- [API 总览](#api-总览)
- [数据采集 API](#数据采集-api)
- [量化分析 API](#量化分析-api)
- [模型部署 API](#模型部署-api)
- [实盘跟踪 API](#实盘跟踪-api)
- [监控告警 API](#监控告警-api)
- [辅助工具 API](#辅助工具-api)
- [退出码约定](#退出码约定)
- [常见错误](#常见错误)

---

## API 总览

| ID | 脚本 | 路径 | operationId |
|---|---|---|---|
| 1 | `download_universe.py` | scripts/ | `downloadUniverse` |
| 2 | `find_double_up_c.py` | scripts/ | `findDoubleUp` |
| 3 | `find_double_up_c_v2.py` | scripts/ | `findDoubleUpV2` |
| 4 | `find_d_signal_5d.py` | scripts/ | `findDSignal` |
| 5 | `top10_deep_dive.py` | scripts/ | `top10DeepDive` |
| 6 | `predict.py` | scripts/ | `predictDaily` |
| 7 | `plan_e_backtest.py` | scripts/ | `planEBacktest` |
| 8 | `plan_e_journal.py` | scripts/ | `planEJournal` |
| 9 | `canary_health_check.py` | scripts/ | `canaryHealthCheck` |
| 10 | `deploy_v5.sh` | v5/scripts/ | `deployV5` |
| 11 | `deploy_v6.sh` | v5/scripts/ | `deployV6` |
| 12 | `batch_distill.py` | scripts/ | `batchDistill` |
| 13 | `convert_distill.py` | scripts/ | `convertDistill` |

---

## 数据采集 API

### 1. downloadUniverse

**operationId**: `downloadUniverse`
**脚本**: `scripts/download_universe.py`
**功能**: 下载全 A 股代码清单（含沪深北）
**命令语法**:

```bash
python scripts/download_universe.py [--target 10000]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--target` | int | 否 | 10000 | 目标股票数 |

#### 输入

无（直接调用通联数据 API）

#### 输出

| 类型 | 路径 | 格式 |
|---|---|---|
| 主输出 | `data/all_a_stocks.json` | JSON（5565 只股票清单）|

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 成功 |
| 1 | API 调用失败 |
| 2 | 参数错误 |

#### 示例

```bash
python scripts/download_universe.py --target 6000
# 输出：data/all_a_stocks.json (5565 只)
```

#### 错误

| 错误 | 原因 | 解决 |
|---|---|---|
| ConnectionError | 通联网络断 | 重试 |
| JSONDecodeError | 接口返回异常 | 等待 30s 重试 |

---

## 量化分析 API

### 2. findDoubleUp

**operationId**: `findDoubleUp`
**脚本**: `scripts/find_double_up_c.py`
**功能**: 识别 20 日累计涨幅 ≥ 100% 的翻倍样本（方案 C）
**命令语法**:

```bash
python scripts/find_double_up_c.py
```

#### Parameters

无（硬编码路径）

#### 输入

| 类型 | 路径 |
|---|---|
| 数据 | `data/parquet/daily/*.parquet` (8035 文件)|

#### 输出

| 类型 | 路径 | 格式 |
|---|---|---|
| 主输出 | `data/double_up/double_up_2020_2026_detail.csv` | CSV (22972 行) |
| 副输出 | `data/double_up/double_up_2020_2026_by_code.csv` | CSV |
| 报告 | `data/double_up/double_up_*.md` | Markdown |

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 成功 |
| 1 | 数据加载失败 |

#### 示例

```bash
python scripts/find_double_up_c.py
# 输出：22972 样本 / 1942 只
# 耗时：~5 分钟
```

---

### 3. findDoubleUpV2

**operationId**: `findDoubleUpV2`
**脚本**: `scripts/find_double_up_c_v2.py`
**功能**: 方案 C v2：使用前复权价 + 与 v1 对比偏差分析
**命令语法**:

```bash
python scripts/find_double_up_c_v2.py
```

#### Parameters

无

#### 输入

| 类型 | 路径 |
|---|---|
| 数据 | `data/parquet/daily/*.parquet` + `data/adj_factor/*.parquet` |

#### 输出

| 类型 | 路径 |
|---|---|
| 主输出 | `data/double_up/double_up_v2_*.csv` |
| 对比 | `data/double_up/double_up_v2_compare.csv` |
| 假阳性 | `data/double_up/double_up_only_v1_false_pos.csv` |
| 报告 | `data/double_up/double_up_v2_report_*.md` |

#### 关键发现

- v1 假阳性：4.5%（前复权后不再算翻倍）
- 偏差极小：未复权 vs 前复权价差 < 0.5%

---

### 4. findDSignal

**operationId**: `findDSignal`
**脚本**: `scripts/find_d_signal_5d.py`
**功能**: 训练方案 D LightGBM 模型（翻倍前 5 日信号）
**命令语法**:

```bash
python scripts/find_d_signal_5d.py
```

#### Parameters

无

#### 输入

| 类型 | 路径 |
|---|---|
| 翻倍样本 | `data/double_up/double_up_2020_2026_detail.csv` |
| 日 K | `data/parquet/daily/*.parquet` |

#### 输出

| 类型 | 路径 | 说明 |
|---|---|---|
| 模型 | `data/double_up/d_signal_5d_model.txt` | LightGBM (141 棵树) |
| 特征 | `data/double_up/d_signal_5d_features.csv` | 13023 训练样本 |
| 报告 | `data/double_up/d_signal_5d_report_*.md` | 特征重要性 |

#### 关键指标

- Test AUC: **0.8144**
- 阈值 0.7 精度: 83.15%
- 阈值 0.8 精度: 89.40%
- Top 3 特征: `prev_close` > `avg_amount_5d` > `close_pos_5d`

---

### 5. top10DeepDive

**operationId**: `top10DeepDive`
**脚本**: `scripts/top10_deep_dive.py`
**功能**: Top 10 妖股深度复盘（含行业、连板节奏、量价）
**命令语法**:

```bash
python scripts/top10_deep_dive.py
```

#### Parameters

无

#### 输入

| 类型 | 路径 |
|---|---|
| 翻倍样本 | `data/double_up/double_up_2020_2026_by_code.csv` |
| 行业数据 | akshare `stock_zyjs_ths` |

#### 输出

| 类型 | 路径 |
|---|---|
| 报告 | `data/double_up/top10_deep_dive_*.md` (22KB) |

#### 关键发现

- 北交所占比 4/10
- 平均连板 5.6 个
- 集中在医药 / 科技 / 新能源

---

## 模型部署 API

### 6. predictDaily

**operationId**: `predictDaily`
**脚本**: `scripts/predict.py`
**功能**: 方案 D 全市场 7620 只预测
**命令语法**:

```bash
python scripts/predict.py [options]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--date` | string | 否 | 今天 | 基准日 (YYYY-MM-DD) |
| `--top` | int | 否 | 20 | Top N 高分股 |
| `--threshold` | float | 否 | 0.7 | 高分阈值（0-1）|
| `--codes` | string | 否 | 全 A | 指定股票代码（逗号分隔）|
| `--workers` | int | 否 | 8 | 并行进程数 |
| `--no-skip-bj` | flag | 否 | False | 不跳过北交所 920XXX |
| `--no-skip-st` | flag | 否 | False | 不跳过 ST 股 |
| `--no-skip-new` | flag | 否 | False | 不跳过新股（< 30 日）|

#### 输入

| 类型 | 路径 | 必需 |
|---|---|:---:|
| 模型 | `data/double_up/d_signal_5d_model.txt` | ✅ |
| 日 K | `data/parquet/daily/*.parquet` | ✅ |
| 复权因子 | `data/adj_factor/*.parquet` | ⚠ 缺失时退化为不复权 |
| 全 A 清单 | `data/all_a_stocks.json` | ⚠ 回退到扫描目录 |

#### 输出

| 类型 | 路径 | 说明 |
|---|---|---|
| 全部预测 | `data/predictions/predict_YYYYMMDD_HHMMSS.csv` | 7620 行 |
| Top N | `data/predictions/predict_top{N}_YYYYMMDD_HHMMSS.csv` | - |
| 高分 | `data/predictions/predict_high_YYYYMMDD_HHMMSS.csv` | threshold+ |
| 报告 | `data/predictions/predict_YYYYMMDD_HHMMSS.md` | Markdown |
| Latest | `data/predictions/predict_latest.md` | 软链最新 |

#### 字段说明（输出 CSV）

| 字段 | 类型 | 说明 |
|---|---|---|
| code | string | 股票代码 |
| date | string | 数据日期 |
| close | float | 收盘价 |
| qfq_close | float | 前复权收盘价 |
| volume | float | 成交量 |
| limit | float | 涨停限制 (0.10/0.20/0.30) |
| prev_close | float | T-1 收盘价 |
| ret_5d | float | 5 日累计涨幅 |
| ret_3d | float | 3 日累计涨幅 |
| max_5d_pct | float | 5 日最大单日涨幅 |
| limit_up_5d | int | 5 日涨停次数 |
| limit_up_t1 | int | T-1 是否涨停 |
| pct_t1 | float | T-1 当日涨幅 |
| avg_amount_5d | float | 5 日均成交额 |
| vol_ratio_t1 | float | T-1 量比 |
| volatility_5d | float | 5 日波动率 |
| close_pos_5d | float | 收盘价位置 |
| max_drawdown_5d | float | 5 日最大跌幅 |
| turnover_proxy | float | 换手代理 |
| is_st / is_new / is_bj / is_gem | int | 标记位 |
| prob | float | 预测概率 |
| rank | int | 排名 |

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 成功 |
| 1 | 模型加载失败 |
| 2 | 数据缺失 |

#### 示例

```bash
# Hello World: 3 只 demo
python scripts/predict.py --codes 600519,000001,300750 --top 3

# 全市场
python scripts/predict.py --top 30 --threshold 0.7

# 指定日期
python scripts/predict.py --date 2026-09-20 --top 50

# 含北交所
python scripts/predict.py --no-skip-bj --top 30
```

#### 错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `KeyError: 'adj_factor'` | parquet 列冲突 | 已自动 drop columns |
| `Model file not found` | 模型丢失 | 检查 `data/double_up/d_signal_5d_model.txt` |
| `feature mismatch` | 特征数不对 | 确认 18 维 |

---

## 实盘跟踪 API

### 7. planEBacktest

**operationId**: `planEBacktest`
**脚本**: `scripts/plan_e_backtest.py`
**功能**: 方案 E 翻倍当日跟随策略回测（v1/v2 对比）
**命令语法**:

```bash
python scripts/plan_e_backtest.py [options]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--hold` | int | 否 | 10 | 持仓天数 |
| `--tp` | float | 否 | 0.30 | 止盈比例 |
| `--sl` | float | 否 | -0.10 | 止损比例 |
| `--no-filter` | flag | 否 | False | 关闭 v2 信号过滤 |
| `--today` | flag | 否 | False | 只生成今日信号 |
| `--compare` | flag | 否 | False | v1 vs v2 对比报告 |

#### 输入

| 类型 | 路径 |
|---|---|
| 翻倍样本 | `data/double_up/double_up_2020_2026_detail.csv` |
| 日 K | `data/parquet/daily/*.parquet` |

#### 输出

| 类型 | 路径 |
|---|---|
| 回测明细 | `data/plan_e/plan_e_backtest_{v1,v2}_*.csv` |
| 回测报告 | `data/plan_e/plan_e_backtest_{v1,v2}_*.md` |
| 对比报告 | `data/plan_e/plan_e_compare_*.md` |
| 今日信号 | `data/plan_e/plan_e_today.csv` |

#### v2 vs v1 对比

| 指标 | v1 | v2 |
|---|---:|---:|
| 胜率 | 46.4% | **55.6%** |
| 平均收益 | 7.41% | **12.22%** |
| 样本数 | 97 | 27 |

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 成功 |
| 1 | 数据加载失败 |

#### 示例

```bash
# 默认回测（v2 增强）
python scripts/plan_e_backtest.py

# 今日信号
python scripts/plan_e_backtest.py --today

# v1 vs v2 对比
python scripts/plan_e_backtest.py --compare

# 自定义参数
python scripts/plan_e_backtest.py --hold 5 --tp 0.20 --sl -0.08
```

---

### 8. planEJournal

**operationId**: `planEJournal`
**脚本**: `scripts/plan_e_journal.py`
**功能**: 方案 E 实盘跟踪系统（6 种模式）
**命令语法**:

```bash
python scripts/plan_e_journal.py --mode <mode> [options]
```

#### Parameters（按模式分组）

##### mode=daily（每日信号归档）

| 名称 | 类型 | 必填 | 默认值 |
|---|---|:---:|---:|

无额外参数。

##### mode=status（查看状态）

无额外参数。

##### mode=buy（手动买入）

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--code` | string | ✅ | - | 股票代码 |
| `--price` | float | ✅ | - | 买入价格 |
| `--qty` | int | ✅ | - | 数量（整手）|

##### mode=sell（手动卖出）

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--code` | string | ✅ | - | 股票代码 |
| `--price` | float | ✅ | - | 卖出价格 |
| `--reason` | string | 否 | MANUAL | TP / SL / TIME / MANUAL |

##### mode=simulate（模拟交易）

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--start` | string | ✅ | - | 开始日期 (YYYY-MM-DD) |
| `--end` | string | ✅ | - | 结束日期 (YYYY-MM-DD) |
| `--capital` | float | 否 | 100000 | 初始资金 |

##### mode=weekly（周报）

无额外参数。

#### 输入

| 模式 | 路径 |
|---|---|
| 所有模式 | `data/plan_e/live/positions.json`（如存在）|
| 模式 daily/simulate | `data/parquet/daily/*.parquet` |

#### 输出

| 类型 | 路径 |
|---|---|
| 持仓 | `data/plan_e/live/positions.json` |
| 流水 | `data/plan_e/live/journal.csv` |
| 每日信号 | `data/plan_e/live/daily_signals/signals_YYYYMMDD.csv` |
| 周报 | `data/plan_e/live/weekly_reports/week_YYYYMMDD.md` |

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 成功 |
| 1 | 现金不足 / 持仓超限 |
| 2 | 参数错误 |

#### 示例

```bash
# 每日模式
python scripts/plan_e_journal.py --mode daily

# 模拟 1 个月
python scripts/plan_e_journal.py --mode simulate \
    --start 2026-08-27 --end 2026-09-25 --capital 100000

# 买入
python scripts/plan_e_journal.py --mode buy \
    --code 600519 --price 10.50 --qty 1000

# 卖出（止盈）
python scripts/plan_e_journal.py --mode sell \
    --code 600519 --price 13.65 --reason TP

# 状态
python scripts/plan_e_journal.py --mode status

# 周报
python scripts/plan_e_journal.py --mode weekly
```

#### 错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `现金不足` | cost > cash | 减仓或加资金 |
| `已达最大持仓 10 只` | positions 已满 | 先卖再买 |
| `未持有 XXX` | sell 未持仓 | 检查代码 |

---

## 监控告警 API

### 9. canaryHealthCheck

**operationId**: `canaryHealthCheck`
**脚本**: `scripts/canary_health_check.py`
**功能**: 6 项系统健康检查（每 5 分钟自动跑）
**命令语法**:

```bash
python scripts/canary_health_check.py [--quiet]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--quiet` | flag | 否 | False | 仅异常时输出 |

#### 检查项（6 项）

| Check | 检查内容 | 阈值 | 异常动作 |
|---|---|---|---|
| ollama | `/api/tags` 200 + 有 tdx 模型 | - | restart ollama |
| tdxfeed_sync | systemd Result=success 且 ≤ 24h | - | 查看 sync.err |
| data_files | parquet/daily ≥ 5000 + adj ≥ 5000 | 5000 | 重跑同步 |
| disk | /home 剩余空间 | 20GB | 清理 GGUF |
| timers | 3 个关键 timer 都 active | - | `systemctl start` |
| gpu | nvidia-smi 利用率 | ≤ 95% | 重启进程 |

#### 输出

| 类型 | 路径 | 格式 |
|---|---|---|
| 简洁日志 | `logs/canary.log` | 文本（追加）|
| 时序数据 | `logs/canary_history.jsonl` | JSONL（每行一次检查）|
| 异常 stderr | `logs/canary.err` | 文本 |

#### Responses

| 退出码 | 含义 |
|:---:|---|
| 0 | 全部 6 项 OK |
| 1 | 至少 1 项 ERROR |

#### 示例

```bash
# 详细输出
python scripts/canary_health_check.py

# 静默模式（systemd 自动调用）
python scripts/canary_health_check.py --quiet

# 输出示例：
# [2026-09-27T09:49:40] ✅ollama ✅tdxfeed_sync ✅data_files ✅disk ✅timers ✅gpu | err=0 warn=0
```

#### 字段说明（canary_history.jsonl）

```json
{
  "ts": "2026-09-27T09:49:40",
  "checks": [
    {
      "name": "ollama",
      "status": "ok",
      "details": {
        "total_models": 8,
        "tdx_models": ["qwen3-14b-tdx-v7:latest", ...]
      }
    },
    ...
  ]
}
```

---

## 辅助工具 API

### 10. deployV5

**operationId**: `deployV5`
**脚本**: `v5/scripts/deploy_v5.sh`
**功能**: v5 模型完整部署（merge → GGUF → Modelfile → ollama）
**命令语法**:

```bash
bash v5/scripts/deploy_v5.sh
```

#### Parameters

无

#### 步骤

1. **merge LoRA → HF**（28GB，~2 分钟）
2. **HF → GGUF f16**（29.5GB，~40 秒）
3. **写 Modelfile-v5**
4. **ollama create**

#### 输出

| 类型 | 路径 |
|---|---|
| merged | `/home/jiuben/models/Qwen3-14B-tdx-v5-merged/` |
| GGUF | `/home/jiuben/models/Qwen3-14B-tdx-v5-merged-f16.gguf` |
| Modelfile | `/home/jiuben/models/Modelfile-v5` |
| ollama | `qwen3-14b-tdx-v5:latest` |

---

### 11. deployV6

**operationId**: `deployV6`
**脚本**: `v5/scripts/deploy_v6.sh`
**功能**: v6 模型完整部署
**命令语法**:

```bash
bash v5/scripts/deploy_v6.sh
```

参数与输出同 deployV5，模型名换为 v6。

---

### 12. batchDistill

**operationId**: `batchDistill`
**脚本**: `scripts/batch_distill.py`
**功能**: 批量蒸馏 LLM 输出为训练样本
**命令语法**:

```bash
python scripts/batch_distill.py [--limit 0] [--batch 8]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--limit` | int | 否 | 0 | 限制样本数（0=全部）|
| `--batch` | int | 否 | 8 | 批大小 |

---

### 13. convertDistill

**operationId**: `convertDistill`
**脚本**: `scripts/convert_distill.py`
**功能**: 转换蒸馏数据为 LLaMA-Factory 训练格式
**命令语法**:

```bash
python scripts/convert_distill.py [--name stock_10k]
```

#### Parameters

| 名称 | 类型 | 必填 | 默认值 | 说明 |
|---|---|:---:|---:|---|
| `--name` | string | 否 | stock_10k | 数据集名称 |

---

## 退出码约定

| 退出码 | 含义 | 通用性 |
|:---:|---|---|
| 0 | 成功 | 所有脚本 |
| 1 | 通用错误（数据/参数/IO）| 所有脚本 |
| 2 | 参数错误 | argparse |
| 3 | 模型加载失败 | predict / plan_e |
| 4 | 网络错误 | download_* |
| 5 | OOM | train / deploy |
| 124 | 超时 | canary (ollama 5s) |

---

## 常见错误

### 1. KeyError: 'adj_factor'

**错误信息**：
```
KeyError: 'adj_factor'
  File "predict.py", line 297, in load_all_stocks
```

**原因**：某些 parquet 文件已包含 `adj_factor` 列，merge 时冲突。

**解决**：predict.py 已自动 `df.drop(columns=['adj_factor'])` 再 merge。

---

### 2. ollama 推理只生成 1 token

**症状**：response 为空字符串。

**原因**：Modelfile 配置错误。

**解决**：
```dockerfile
# ✅ 正确
TEMPLATE """{{ .Prompt }}"""
PARAMETER repeat_penalty 1.0

# ❌ 错误
PARAMETER repeat_penalty 1.1  # v7 必须 = 1.0
PARAMETER stop "<|im_start|>"  # 立刻触发停止
```

---

### 3. GGUF 转换失败（DFLASH 错误）

**错误信息**：
```
AttributeError: type object 'MODEL_ARCH' has no attribute 'DFLASH'
```

**原因**：gguf 包版本太旧。

**解决**：
```bash
pip install --force-reinstall gguf
# 或从 llama.cpp master zip 复制
# /tmp/llama-cpp-fix/llama.cpp-master/gguf-py/gguf → venv/.../site-packages/gguf
```

---

### 4. ollama convert_hf_to_gguf.py 404 (14 字节)

**症状**：`/home/jiuben/llama.cpp/convert_hf_to_gguf.py` 内容只剩 `404: Not Found`。

**解决**：
```bash
curl -sL -o /home/jiuben/llama.cpp/convert_hf_to_gguf.py \
  "https://raw.githubusercontent.com/ggerganov/llama.cpp/master/convert_hf_to_gguf.py"
```

---

### 5. vllm-venv 路径不存在

**错误**：`/home/jiuben/tdx-data-feed/vllm-venv/bin/python: 没有那个文件或目录`

**解决**：直接用 `venv/bin/python`（已合并到 venv）。

---

### 6. systemd timer 不触发

**排查**：
```bash
systemctl --user list-timers --all   # 检查是否 active
systemctl --user daemon-reload       # 重载
systemctl --user start XXX.timer     # 手动启动
journalctl --user -u XXX.service -f  # 查看日志
```

---

### 7. predict 概率全 0.5

**原因**：模型文件损坏。

**解决**：
```bash
# 重新训练
python /tmp/v5_train_single.py

# 或检查模型文件
ls -la data/double_up/d_signal_5d_model.txt
head -3 data/double_up/d_signal_5d_model.txt
```

---

### 8. 方案 E 模拟跑很久

**症状**：`--mode simulate` 跑 1 月需要 30 分钟。

**原因**：每日信号计算单线程。

**优化**：未来可用 ProcessPoolExecutor 并行。

---

## 附录

### A. CLI 接口状态码总览

| 脚本 | mode/dataset | 成功 | 失败 |
|---|---|:---:|:---:|
| download_universe.py | - | 0 | 1/2 |
| find_double_up_c.py | - | 0 | 1 |
| find_double_up_c_v2.py | - | 0 | 1 |
| find_d_signal_5d.py | - | 0 | 1 |
| top10_deep_dive.py | - | 0 | 1 |
| predict.py | - | 0 | 1/2 |
| plan_e_backtest.py | - | 0 | 1 |
| plan_e_journal.py | daily/weekly/status | 0 | 1 |
| plan_e_journal.py | buy | 0 | 1 (现金不足) |
| plan_e_journal.py | sell | 0 | 1 (未持仓) |
| plan_e_journal.py | simulate | 0 | 1 |
| canary_health_check.py | - | 0 | 1 (有 ERROR) |

### B. 完整命令速查

```bash
# === 数据 ===
python -m tdxfeed.cli sync
python scripts/download_universe.py
python scripts/download_adj_factor_b.py

# === 分析 ===
python scripts/find_double_up_c.py
python scripts/find_double_up_c_v2.py
python scripts/find_d_signal_5d.py
python scripts/top10_deep_dive.py

# === 预测 ===
python scripts/predict.py --top 30

# === 策略回测 ===
python scripts/plan_e_backtest.py --compare
python scripts/plan_e_journal.py --mode simulate \
    --start 2026-08-27 --end 2026-09-25 --capital 100000

# === 实盘 ===
python scripts/plan_e_journal.py --mode daily
python scripts/plan_e_journal.py --mode buy --code 600519 --price 10.50 --qty 1000
python scripts/plan_e_journal.py --mode sell --code 600519 --price 13.65 --reason TP
python scripts/plan_e_journal.py --mode weekly

# === 部署 ===
bash v5/scripts/deploy_v5.sh
bash v5/scripts/deploy_v6.sh

# === 监控 ===
python scripts/canary_health_check.py
systemctl --user list-timers --all
```

---

## 许可

私有项目 · 内部使用 · 未经授权不得传播

> 最后更新：2026-09-27 · 维护者：jiuben