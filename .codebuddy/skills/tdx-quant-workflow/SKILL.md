---
name: tdx-quant-workflow
description: "tdx-data-feed 量化研究端到端 pipeline。一键触发数据采集→模型训练→每日预测→方案E回测→实盘跟踪完整流程。当用户描述涉及完整流程、全部跑一遍、端到端 pipeline、从数据到回测时使用。"
allowed-tools: Read,Grep,Glob,Bash,Edit,Write
display_name: "tdx-quant-workflow"
visibility: "project"
---

# tdx-quant-workflow

## 适用场景

需要一次性完成 tdx-data-feed 端到端流程时使用，包括：
- "把整个 pipeline 跑一遍"
- "数据 → 训练 → 预测 → 回测 → 实盘 完整走一次"
- "验证模型和策略的有效性"
- "项目交付前的全套验证"

## 触发的 5 阶段

```
数据采集 → 模型训练 → 每日预测 → 方案 E 回测 → 实盘 journal
   ↓            ↓             ↓              ↓                ↓
 tdx-data    tdx-model    tdx-predictor  tdx-backtester   journal.md
 collector    trainer
```

> **重要区分（生产 vs 研究）**：本流程的**生产预测器是 LightGBM 翻倍信号模型**（`predict.py` 实际调用，`vpnl_huber_stage1` 为默认）。
> LLM（Qwen3-14B QLoRA）仅用于 `kline-analysis` 等**可解释/对话式分析**，实测准确率≈随机，**不是**生产预测主路径。
> 阶段 2 同时列出两条训练线，但只有 LightGBM 线是生产必需。

## 执行步骤

### 阶段 1：数据采集（tdx-data-collector）
```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python -m tdxfeed.full_sync          # 主入口：A股日线+指数（akshare 取代码清单 + Sina 拉 K 线 + 财务出数），白天后台运行
venv/bin/python scripts/min_sync2.py          # 分钟线增量（可选）
venv/bin/python scripts/download_etf_bond.py  # ETF/债券（可选）
venv/bin/python scripts/download_universe.py  # 股票池/代码表（可选）
```
> 注：旧文档里的 `scripts/download_baostock.py` / `scripts/download_akshare_bj.py` / `scripts/build_adj_factor.py`
> 均已不存在，已替换为上述真实入口（复权/财务现由 `tdxfeed/full_sync` 内置 `fin_parquet` 模块产出）。

**验证**：`ls data/parquet/daily | wc -l`（当前约 8035，目标 ≥ 8000）

### 阶段 2：模型训练（tdx-model-trainer，可选）
> 生产预测器是 **LightGBM 翻倍信号模型**（`predict.py` 实际调用）。LLM（Qwen3-14B QLoRA）仅用于 `kline-analysis` 等可解释分析，准确率≈随机，非生产主路径。

**A. 生产信号模型（LightGBM，CPU）** — 若 `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt` 已存在，跳过：
```bash
venv/bin/python scripts/find_d_signal_5d.py   # 训练 d_signal_5d 系列，产出 data/double_up/d_signal_5d_model_*.txt
```

**B. LLM 研究路径（Qwen3-14B QLoRA，可选）** — 若 `train/output/qwen3-14b-tdx-v7` 已存在，跳过：
```bash
nohup venv/bin/llamafactory-cli train train/v7_lora.yaml > /tmp/v7.log 2>&1 &
# 等到 eval_loss ≤ 0.20 才算 OK
```

**验证（生产）**：`test -f data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`

### 阶段 3：每日预测（tdx-predictor）
```bash
venv/bin/python scripts/predict.py --top 30 --threshold 0.7
# 默认已用 vpnl_huber_stage1 + polars 特征管线（阶段 1 因子迁移已上线，commit + tag stage1-switch）。
# 如需回滚到旧模型/管线：
#   venv/bin/python scripts/predict.py --model vpnl_huber --features legacy --top 30 --threshold 0.7
```
**验证**：`test -f data/predictions/predict_latest.md`

### 阶段 4：方案 E 回测（tdx-backtester）
```bash
venv/bin/python scripts/plan_e_backtest.py --compare
```
**验证**：`test -f data/plan_e/plan_e_compare_*.md`（取最新时间戳文件；`--compare` 产出 `plan_e_compare_{ts}.md`）

### 阶段 5：实盘 journal
> 仅记录，不下单。每日 18:35 写入 `journal_YYYY-MM-DD.md`。

```bash
venv/bin/python scripts/plan_e_journal.py --date $(date +%Y-%m-%d)
```

## 全流程检查清单

| # | 阶段 | 命令 | 通过条件 |
|---|---|---|---|
| 1 | 数据 | `ls data/parquet/daily | wc -l` | ≥ 8000 |
| 2 | 翻倍样本 | `ls data/double_up/double_up_2020_2026_detail.csv` | 存在 |
| 3 | 生产模型 | `test -f data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt` | 存在（LLM 可选：`test -d train/output/qwen3-14b-tdx-v7`） |
| 4 | 预测 | `test -f data/predictions/predict_latest.md` | 存在 |
| 5 | 回测 | `test -f data/plan_e/plan_e_compare_*.md` | 存在（`--compare` 最新时间戳文件） |
| 6 | 健康 | `make canary` | 6 项全 OK |

## 触发前确认

> 使用本 skill 前先询问用户：

1. **范围**：是否要重跑模型训练？（通常否；生产 LightGBM 模型已就位）
2. **实盘**：是否要写入实盘 journal？（每日 18:35 自动）
3. **回测参数**：默认 hold=10 / tp=0.30 / sl=-0.10 / threshold=0.7

## 输出汇总

完成后自动生成 `data/pipeline_YYYY-MM-DD.md` 包含：

- 各阶段耗时 / 状态
- 关键指标（数据条数 / 预测数 / 胜率 / 收益）
- 失败清单（若有）
- 链接到各阶段最新报告

## 不要做

- ❌ 跳过数据校验
- ❌ 在生产时间（9:30-15:00）跑重训
- ❌ 自动提交实盘订单
- ❌ 覆盖历史预测 / 回测报告
- ❌ 修改模型参数后未评估直接部署

## 失败回滚

任一阶段失败，立即停止后续阶段，输出失败原因 + 修复建议：
- 阶段 1 失败 → 检查 Sina / akshare 网络连通性（清单取自 akshare，K线取自 Sina；失败回退 pytdx/内置清单）
- 阶段 2 失败 → 生产模型：检查 CPU/venv + 训练脚本；LLM：检查 GPU + CUDA + venv
- 阶段 3 失败 → 检查 LightGBM 模型文件存在（`data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`）+ 特征管线（`--features polars` 为 stage1 必需，legacy 仅配 `--model vpnl_huber`）
- 阶段 4 失败 → 检查 parquet + adj_factor 一致性
- 阶段 5 失败 → 检查 PostgreSQL 连接

## 相关资源

- `.codebuddy/agents/` — 4 个 Agent 详细职责
- `.codebuddy/rules/` — 3 个项目规范
- `docs/WORKFLOW.md` — 完整工作流文档
- `docs/PLAN_E.md` — 方案 E 策略说明
- `docs/stage1_feature_migration_plan.md` — 阶段 1 因子迁移（vpnl_huber_stage1 + polars）设计 / 回滚文档
- `deploy/README.md` — 容器化部署指南
- 发布标记：`git tag stage1-switch`（2026-10-11，阶段 1 上线）
