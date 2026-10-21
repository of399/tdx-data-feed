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

## ⚠️ 最重要区分：生产预测器 vs 研究型 LLM

本流程的**生产预测主路径是 LightGBM 信号模型**（`predict.py` 实际调用），纯 CPU 运行，无需 GPU，是方案 D 每日预测的唯一生产组件：
- **默认小仓位**：`--position-mode small` → `daban`（29 维特征，次日收盘涨停二分类），Top 5，模型文件 `data/double_up/d_signal_5d_model_daban.txt`。
- **大仓位**：`--position-mode full` → `vpnl_huber_stage1`（21 维精选特征 + 候选因子，PnL Huber 回归），Top 20，模型文件 `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`。
- **回滚**：`--model vpnl_huber`（早期回归默认）或 `--model prod`（18 维分类）。

**LLM（Qwen3-14B QLoRA，如 `qwen3-14b-tdx-v7`）仅用于可解释性 / 对话式分析**（对应 `kline-analysis` 等技能），实测准确率≈随机（≈48-49%，近多数类基线），**不是**生产预测主路径，也**不参与**每日预测或回测。

> 阶段 2 同时列出两条训练线，但**只有 LightGBM 线是生产必需**；LLM 线为可选研究用途，请勿将其当作预测器。

## 触发的 5 阶段

```
数据采集 → 模型训练 → 每日预测 → 方案 E 回测 → 实盘 journal
   ↓            ↓             ↓              ↓                ↓
 tdx-data    tdx-model    tdx-predictor  tdx-backtester   journal.csv
 collector    trainer
```

## 执行步骤

### 阶段 1：数据采集（tdx-data-collector）

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python -m tdxfeed.full_sync          # 主入口：akshare 取代码清单 + Sina 拉日K/指数 + fin_parquet 财务出数（核心300 断点续传），白天后台运行
venv/bin/python scripts/backfill_minutes.py --period 15   # 分钟线补齐（可选，断点续跑）
venv/bin/python scripts/download_universe.py  # 沪深股票池补齐（可选）
```

> **已失效脚本（切勿使用）**：`scripts/download_baostock.py`、`scripts/download_akshare_bj.py`、`scripts/build_adj_factor.py`、`min_sync2.py` 经实测**均不存在**。复权因子与财务现由 `tdxfeed.full_sync` 内置模块（`writers` / `fin_parquet`）产出。
>
> **已下线（2026-10-21）**：ETF 与北交所数据已从项目移除；`download_etf_bond.py`、`download_bj_daily.py`、`download_adj_factor_bj.py`、`backfill_adj_etf.py` 仅保留弃用提示，`make data-bj` 已删除。

**验证**：`find data/parquet/daily -maxdepth 1 -name '*.parquet' ! -name '*.v1.bak.parquet' | wc -l`（当前 5,235：5,221 沪深个股 + 14 指数；另有 5,232 个 `.v1.bak` 备份不计入）；`find data/adj_factor -maxdepth 1 -name '*.parquet' | wc -l`（当前 5,221）

### 阶段 2：模型训练（tdx-model-trainer，可选）

**A. 生产信号模型（LightGBM，CPU）**
```bash
# 默认 daban（小仓位，29 维，次日涨停分类）
venv/bin/python scripts/train_daban_model.py
# 大仓位回归模型 vpnl_huber_stage1（21 维精选 + 候选因子）
venv/bin/python scripts/find_d_signal_5d.py
```

**B. LLM 研究路径（Qwen3-14B QLoRA，可选 / 非生产）** — 若 `train/output/qwen3-14b-tdx-v7` 已存在则跳过：
```bash
# 训练配置见 training/ 下 LLaMA-Factory YAML（实际为 tdx_v7_resume.yaml）；LLM 仅用于可解释分析
nohup venv/bin/llamafactory-cli train training/config/tdx_v7_resume.yaml > /tmp/v7.log 2>&1 &
# 等到 eval_loss ≤ 0.20 才算 OK（注意：LLM 预测准确率≈随机，勿用于生产信号）
```

**验证（生产）**：`test -f data/double_up/d_signal_5d_model_daban.txt`（小仓位默认）/ `test -f data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`（大仓位）

### 阶段 3：每日预测（tdx-predictor）

```bash
venv/bin/python scripts/predict.py            # 默认：small 模式 = daban 29 维，Top 5，阈值 0.7
# 大仓位：vpnl_huber_stage1 回归，Top 20
venv/bin/python scripts/predict.py --position-mode full
# 回滚到旧模型（正交，可单独回退模型层）：
venv/bin/python scripts/predict.py --model vpnl_huber --features legacy --top 30 --threshold 0.7
```

**验证**：`test -f data/predictions/predict_latest.md`（同时产出 `predict_YYYYMMDD_HHMMSS.{csv,md}` + `predict_top5_*.csv` + `predict_high_*.csv`）

### 阶段 4：方案 E 回测（tdx-backtester）

```bash
venv/bin/python scripts/plan_e_backtest.py --compare
# --compare 产出：data/plan_e/plan_e_compare_{ts}.md（v1 vs v2 对比，无 _latest 后缀）
```

**验证**：`ls -1t data/plan_e/plan_e_compare_*.md | head -1`（取最新时间戳文件；`test -f` 不支持多文件通配）

### 阶段 5：实盘 journal

> 仅**记录**交易日志，**绝不下单**。实盘与模拟完全分离：
> - 实盘：`data/plan_e/live/journal.csv`（source=real，手动 buy/sell 维护）
> - 模拟：`data/plan_e/live/journal_sim.csv`（`--mode simulate` 独立写入，不污染实盘）

```bash
venv/bin/python scripts/plan_e_journal.py --mode daily          # 汇总当日方案E信号
venv/bin/python scripts/plan_e_journal.py --mode buy  --code 600519 --price 10.50 --qty 10000
venv/bin/python scripts/plan_e_journal.py --mode sell --code 600519 --price 12.30 --reason TP
venv/bin/python scripts/plan_e_journal.py --mode simulate --start 2026-08-27 --end 2026-09-27 --capital 100000
```

**验证**：`test -f data/plan_e/live/journal.csv`

## 全流程检查清单

| # | 阶段 | 命令 | 通过条件 |
|---|---|---|---|
| 1 | 数据 | `find data/parquet/daily -maxdepth 1 -name '*.parquet' \| wc -l` | ≥ 12,000 |
| 2 | 翻倍样本 | `test -f data/double_up/double_up_2020_2026_detail.csv` | 存在 |
| 3 | 生产模型 | `test -f data/double_up/d_signal_5d_model_daban.txt` | 存在（大仓位：`d_signal_5d_model_vpnl_huber_stage1.txt`；LLM 可选：`test -d train/output/qwen3-14b-tdx-v7`） |
| 4 | 预测 | `test -f data/predictions/predict_latest.md` | 存在 |
| 5 | 回测 | `ls -1t data/plan_e/plan_e_compare_*.md \| head -1` | 有输出（`--compare` 最新时间戳文件） |
| 6 | 实盘 | `test -f data/plan_e/live/journal.csv` | 存在 |
| 7 | 健康 | `make canary` | 全部检查项 OK |

## 触发前确认

> 使用本 skill 前先询问用户：

1. **范围**：是否要重跑模型训练？（通常否；生产 LightGBM 模型已就位，仅需确认 `d_signal_5d_model_daban.txt`（默认）或 `d_signal_5d_model_vpnl_huber_stage1.txt`（大仓位）存在）
2. **实盘**：是否要写入实盘 journal？（journal 为手动 buy/sell 记录，不下单；每日 18:10 定时器仅刷新当日信号）
3. **回测参数**：默认 hold=10 / tp=0.30 / sl=-0.10 / threshold=0.7

## 输出汇总

完成后生成 `data/pipeline_YYYY-MM-DD.md`（人工/编排层汇总，非单脚本自动产出）包含：
- 各阶段耗时 / 状态
- 关键指标（数据条数 / 预测数 / 胜率 / 收益）
- 失败清单（若有）
- 链接到各阶段最新报告（`predict_latest.md`、`plan_e_compare_*.md`、`journal.csv`）

## 不要做

- ❌ 跳过数据校验
- ❌ 在生产时间（9:30-15:00）跑重训
- ❌ 自动提交实盘订单
- ❌ 覆盖历史预测 / 回测报告
- ❌ 修改模型参数后未评估直接部署
- ❌ 把 LLM（≈随机准确率）当作生产预测器

## 失败回滚

任一阶段失败，立即停止后续阶段，输出失败原因 + 修复建议：
- **阶段 1 失败** → 检查 Sina / akshare 网络连通性（代码清单取自 akshare，K线/指数取自 Sina；失败回退 pytdx / 内置清单）；复权因子与财务由 `tdxfeed.full_sync` 内置产出，无需单独脚本
- **阶段 2 失败** → 生产模型（LightGBM）：检查 CPU / venv + `scripts/train_daban_model.py`（daban）/ `scripts/find_d_signal_5d.py`（stage1）；LLM（研究）：检查 GPU + CUDA + venv，确认非生产路径
- **阶段 3 失败** → 检查默认模型 `data/double_up/d_signal_5d_model_daban.txt`（或大仓位 `d_signal_5d_model_vpnl_huber_stage1.txt`）存在；默认 `--features polars` 与 stage1 对齐，legacy 仅配 `--model vpnl_huber`；回滚命令见阶段 3
- **阶段 4 失败** → 检查 parquet / adj_factor 一致性；确认 `data/plan_e/` 可写
- **阶段 5 失败** → 检查 `data/plan_e/live/` 写权限；确认 `--mode` 参数合法

## 相关资源

- `.codebuddy/agents/` — 4 个 Agent 详细职责（`tdx-data-collector` / `tdx-model-trainer` / `tdx-predictor` / `tdx-backtester`）
- `.codebuddy/rules/` — 4 个项目规范（`tdx-conventions` / `tdx-data-schema` / `tdx-data-schema-v2` / `tdx-output-format`）
- `skills/` — 8 个配套技能 yaml：`kline-analysis`（K线分析，调用 LLM）、`financial-summary`（本地财务 + finance-deep 按需扩展）、`news-summary`（tdx-news + vault）、`adj-factor`（生产 CPU 计算依赖）、`indicator-match`（与 LightGBM 预测无关，仅 GPU 降级）、`backtest`（tdx-backtest 纯工具回测）、`financial-statement`（三大报表纯工具查询）、`theory-reference`（财经七书讲义纯检索，限定 vault `09-理论讲义` 目录，`llm.model_id: none` 不消耗 GPU；查询中无股票代码且命中概念性提问词时由 `agents/orchestrator.py` 自动路由）
- `docs/WORKFLOW.md` — 完整工作流文档
- `docs/PLAN_E.md` — 方案 E 策略说明
- `docs/PLAN_E_LIVE.md` — 方案 E 实盘跟踪说明
- `docs/stage1_feature_migration_plan.md` — 阶段 1 因子迁移（vpnl_huber_stage1 + polars）设计 / 回滚文档
- `docs/stage1_optuna_report.md` — 阶段 1 Optuna 特征精选报告
- `docs/HYBRID_LLM_STRATEGY.md` — LLM（Qwen3-14B QLoRA）与 LightGBM 生产预测器的定位 / 边界说明，支撑阶段 2「研究型 LLM 非主路径」论断
- `docs/LIVE_TRACKING_GUIDE.md` — 方案 E 实盘跟踪操作指南（journal.csv / journal_sim.csv、每日定时刷新当日信号），支撑阶段 5 journal 功能
- `deploy/README.md` — 容器化部署指南
- 发布标记：`git tag stage1-switch`（2026-10-11，阶段 1 上线）
