# tdx-data-feed

> **A股量化研究平台** — 数据采集 → LoRA 微调 → 每日预测 → 策略回测 → 实盘跟踪
>
> **维护者**：jiuben · **最后更新**：2026-10-21（健康检查 + 数据计数/模型对齐）· **状态**：canary 7/7 ✅ · Ollama 4 个 tdx 模型 ✅ · **v2 schema 100% 合规** ✅

> **一句话**：把沪深 A 股 5,235 个日 K parquet（5,221 只个股 + 14 个指数）+ 5,221 复权因子 → 翻倍样本 → Qwen3-14B LoRA 微调 → Ollama 部署 → 方案 E 实盘策略

---

## 📑 目录

- [0. 新成员 5 分钟上手](#0-新成员-5-分钟上手)
- [1. 系统整体架构](#1-系统整体架构)
- [2. 核心模块与相互关系](#2-核心模块与相互关系)
- [3. 环境依赖与安装](#3-环境依赖与安装)
- [4. 配置参数说明](#4-配置参数说明)
- [5. 快速上手示例](#5-快速上手示例)
- [6. 常见使用场景](#6-常见使用场景)
- [7. 故障排查指引](#7-故障排查指引)
- [8. 项目级 IDE 工作空间（.codebuddy/）](#8-项目级-ide-工作空间codebuddy)
- [9. 容器化部署（Docker Compose）](#9-容器化部署docker-compose)
- [10. CI/CD 流水线](#10-cicd-流水线)
- [11. systemd 自动化调度](#11-systemd-自动化调度)
- [12. 文档索引](#12-文档索引)

---

## 0. 新成员 5 分钟上手

### 🎯 我应该看什么？

| 你是谁 | 先看这章 |
|---|---|
| **第一次接触项目** | 本节（5 分钟）→ §1 架构 → §5 上手示例 |
| **想跑预测/回测** | §3 安装 → §5 上手示例 |
| **想训练模型** | §3 安装 → §4 配置 → docs/WORKFLOW.md |
| **想部署到生产** | §9 容器化 + §10 CI/CD |
| **维护项目/排查故障** | §7 故障排查 + §11 systemd |

### 🚀 3 行命令体验项目

```bash
cd /home/jiuben/tdx-data-feed
source venv/bin/activate
python scripts/predict.py --codes 600519,000001,300750 --top 3
# Web UI（浏览器交互式预测，Gradio）
python scripts/predict.py --web --port 7860
```

**结果**：3 只股票的预测分数（daban 涨停概率 / stage1 预期 PnL）+ 模型特征分析报告。

### 📊 项目关键数字（速查）

| 维度 | 数字 |
|---|---|
| 数据规模 | 5,235 日 K parquet（5,221 沪深个股 + 14 指数；另有 5,232 个 `.v1.bak` 备份）+ 5,221 复权因子 + 22,972 翻倍样本 + 1.67M 财务 |
| LLM 研究线 | v5 / v6 / v7 / v7r / v9r（**非生产预测路径**）|
| Ollama 部署 | 7 个模型（4 个 tdx：`v4fwd` / `v7r-q8` / `v7r1115` / `fast:14b`；另 `deepseek-r1:14b` / `qwen3:14b` / `nomic-embed-text`）|
| 方案 E v2 | 胜率 35.3% / 平均收益 3.16% / 盈亏比 2.71 / 136 样本（2026-10-16 报告）|
| 方案 D 预测 | 5,221 只股票并行打分（默认 daban Top5）|
| 容器化 | 10 service · 8 数据卷 · 250 GB |
| IDE 工作空间 | 4 Agent + 4 Rule + 1 Skill + 2 IDE MCP + 22 Memory（+ 4 个仅记忆的 automation）|

---

### 🧹 数据治理状态（2026-10-21）

- **ETF / 北交所数据下线**：删除 1,652 个 ETF/基金日 K、344 个北交所日 K 及对应 adj_factor / derived / min15 / min60 / legacy lday；`all_a_stocks.json` 由 5,565 降至 5,221；StockVault 344 篇 920 笔记同步删除。归档：`_archive/removed_etf_bj_20261021.tar.gz`。
- **债券数据下线**：`download_universe.py` / `download_etf_bond.py` / `backfill_adj_etf.py` 已移除债券处理；已删除 `data/parquet/daily/` 下 317 个债券落盘文件与 `data/convertible_bond_codes.txt`。**后续不再保留债券数据**。
- **大文件出 git（D7）**：`v5/audit/` 下约 80 MB 审计大文件（parquet/csv/lora/suggestion-md）已 `git rm --cached` 并加入 `.gitignore`（磁盘保留，仅停止跟踪，避免仓库继续膨胀）。
- **min5 补齐（D1）**：历史脚本 `min_sync2.py` 已归档至 `_archive/`（不再可调用）；现有 `data/parquet/min5/` 数据保留，分钟线补齐用 `scripts/backfill_minutes.py`（15/60 分钟）。
- **v5/v6/v7 推理对标（F1）**：`scripts/bench_v567.py` 输出 BLEU/ROUGE + 翻倍分类准确率 + P95 延迟 + 显存峰值，结果见 `data/bench_v567.json`。
- **脚本归档（W1）**：分类目录见 `scripts/INDEX.md`（未做物理移动，避免破坏现有管线）。

---

## 1. 系统整体架构

### 1.1 全景数据流

```
┌──────────────────────────────────────────────────────────────────┐
│                       tdx-data-feed 全景                          │
└──────────────────────────────────────────────────────────────────┘

  ┌────────────┐    ┌────────────┐    ┌────────────┐
  │  数据采集   │───▶│  数据存储   │───▶│  量化分析   │
  │ baostock   │    │ parquet    │    │ plan_*     │
  │ akshare    │    │ adj_factor │    │ find_*     │
  │ tdxfeed/   │    │ double_up  │    │ lightgbm   │
  └────────────┘    └────────────┘    └────────────┘
                                            │
                                            ▼
  ┌────────────┐    ┌────────────┐    ┌────────────┐
  │  应用层     │◀───│  模型部署   │◀───│  模型训练   │
  │ predict.py │    │ Ollama     │    │ v5/v6/v7   │
  │ plan_e_*   │    │ Modelfile  │    │ LLaMA-Fty  │
  │ journal.py │    │ GGUF       │    │ PEFT       │
  └────────────┘    └────────────┘    └────────────┘
        │
        ▼
  ┌────────────────────────────────────────────┐
  │  自动化 + 监控 + IDE 工作空间               │
  │  systemd/*.timer · canary · .codebuddy/    │
  │  prometheus · grafana · docker-compose     │
  └────────────────────────────────────────────┘
```

### 1.2 模块层次

| 层级 | 模块 | 技术栈 | 关键路径 |
|---|---|---|---|
| L1 数据采集 | tdxfeed/ + scripts/download_*.py | baostock, akshare, pytdx | `data/parquet/daily/` |
| L2 数据存储 | data/parquet/ + data/adj_factor/ | Parquet | `data/double_up/` |
| L3 量化分析 | scripts/find_*.py + scripts/plan_*.py | pandas, lightgbm | `data/double_up/d_signal_5d_*` |
| L4 模型训练 | train/output/ + LLaMA-Factory | transformers, peft | `train/output/qwen3-14b-tdx-v*/` |
| L5 模型部署 | Ollama + Modelfile | GGUF, llama.cpp | `models/*-merged-f16.gguf` |
| L6 模型推理 | Ollama HTTP API + scripts/predict.py | requests | `data/predictions/predict_*.md` |
| L7 实盘跟踪 | scripts/plan_e_journal.py | subprocess | `journal_YYYY-MM-DD.md` |
| L8 自动化 | systemd/*.timer | systemd user | `systemd/*.timer` |
| L9 监控 | scripts/canary_health_check.py | requests, nvidia-smi | `logs/canary.log` |
| L10 IDE 配置 | .codebuddy/agents + rules + skills | Markdown | `.codebuddy/` |
| L11 容器化 | deploy/Dockerfile + docker-compose.yml | Docker | `deploy/` |

### 1.3 关键调用关系

```
predict.py ──▶ LightGBM 模型 ──▶ data/predictions/predict_*.md
                ▲
                │
data/double_up/d_signal_5d_model.txt ←── plan_d_train.py（方案 D 训练）
                                        │
data/parquet/daily/*.parquet ───────────┘
                ▲
                │
data/adj_factor/*.parquet ─── build_adj_factor.py
                ▲
                │
scripts/download_baostock.py + download_akshare_bj.py
                ▲
                │
baostock / akshare（外部数据源）
```

---

## 2. 核心模块与相互关系

### 2.1 4 大功能模块

#### 🎯 方案 D：每日全市场预测

| 项 | 内容 |
|---|---|
| 目标 | 全市场打分；small 默认 Top 5，full 模式 Top 20 |
| 输入 | 日 K + 复权因子 + 翻倍样本 |
| 模型 | LightGBM：默认 `daban`（29 维，次日涨停分类）；大仓位 `vpnl_huber_stage1`（21 维，PnL 回归）|
| 特征 | 由模型 `feature_name()` 动态决定（daban 29 / stage1 21 / prod 18）|
| 输出 | `data/predictions/predict_YYYYMMDD_HHMMSS.csv` + `.md` |
| 脚本 | `scripts/predict.py`（新增 `--web` Gradio UI）|
| 调度 | `systemd/predict-daily.timer`（工作日 18:05）|

#### 🎯 方案 E：翻倍当日跟随策略（回测 + 实盘）

| 项 | 内容 |
|---|---|
| 目标 | 翻倍当日 + T+1 涨停 + 量比放大 → 跟随 10 日 |
| v1 规则 | 仅翻倍当日 → 胜率 31.2% |
| **v2 规则** | + T+1 ≥ 9.5% + 量比 ≥ 1.5 → **胜率 35.3%**（2026-10-16 报告）|
| 关键指标 | 胜率 / 平均收益 / 盈亏比 / 夏普 / 最大回撤 |
| 回测脚本 | `scripts/plan_e_backtest.py --compare` |
| 实盘脚本 | `scripts/plan_e_journal.py --mode daily` |
| 输出 | `data/plan_e/plan_e_backtest_*.md` + `journal_*.md` |

#### 🎯 模型训练与部署

| 阶段 | 工具 | 输出 |
|---|---|---|
| 训练 | LLaMA-Factory + PEFT | `train/output/qwen3-14b-tdx-v7/checkpoint-500/` |
| 合并 | PeftModel.merge_and_unload | `models/Qwen3-14B-tdx-v7-merged/` (~28 GB) |
| GGUF 转换 | llama.cpp convert_hf_to_gguf.py | `models/*-merged-f16.gguf` (~29.5 GB) |
| Modelfile | 手写 | `models/Modelfile-v7` |
| 部署 | `ollama create` | Ollama 中的 `qwen3-14b-tdx-v7:latest` |

#### 🎯 实盘跟踪

| 项 | 内容 |
|---|---|
| 模式 | simulate / paper / live |
| journal 文件 | `journal_YYYY-MM-DD.md` |
| 调度 | `systemd/plan-e-journal.timer`（18:35 每日）|
| 周报 | `weekly_YYYY-W{NN}.md`（周五 20:00）|

### 2.2 模块依赖关系

```
predict.py
  └─▶ data/double_up/d_signal_5d_model.txt    （LightGBM 模型）
  └─▶ data/double_up/d_signal_5d_features.csv （18 维基础特征 schema，历史训练集）
  └─▶ data/parquet/daily/{code}.parquet       （日 K）
  └─▶ data/adj_factor/{code}.parquet          （复权因子）

plan_e_backtest.py
  └─▶ data/double_up/double_up_2020_2026_detail.csv （翻倍样本 22972 行）
  └─▶ data/parquet/daily/{code}.parquet              （行情数据）

plan_e_journal.py
  └─▶ data/predictions/predict_latest.md  （今日候选股）
  └─▶ data/parquet/daily/{code}.parquet   （实时行情）
  └─▶ ~/.local/share/plan_e_journal/state.json  （持仓状态）
```

---

## 3. 环境依赖与安装

### 3.1 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| OS | Ubuntu 22.04 | Ubuntu 24.04 |
| CPU | 8 核 | 16 核+ |
| 内存 | 32 GB | 64 GB+ |
| GPU | RTX 3060 (12 GB) / **RTX 5060 Ti (16 GB)** | RTX 4090 (24 GB) / A100 (40 GB) |
| 磁盘 | 250 GB | 500 GB+ SSD |
| Python | 3.14 | 3.14 |
| CUDA Toolkit | 12.4 | 12.4+ |
| nvidia-driver | 580.x+ | 580.x+ |
| Ollama | 0.3.10 | 0.3.13+ |

> **实际部署基线**（截至 2026-09-27）：RTX 5060 Ti 16 GB + CUDA 12.4 + Python 3.14.4 + Ollama 0.x（snap）。
> 16 GB 显存可跑 v7 模型推理，但**长时间训练或大批量回测仍有 OOM 风险**——建议 24 GB+。

### 3.2 系统依赖

```bash
sudo apt update && sudo apt install -y \
    python3.14 python3.14-venv python3.14-dev \
    build-essential git curl wget \
    libgomp1 libssl-dev libffi-dev \
    nvidia-driver-580 nvidia-cuda-toolkit
```

### 3.3 Ollama 安装

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama --version
```

### 3.4 Python 环境

```bash
cd /home/jiuben/tdx-data-feed

# 1. 创建虚拟环境
python3.14 -m venv venv
source venv/bin/activate

# 2. 装填依赖
pip install --upgrade pip wheel
pip install -r requirements.txt
pip install lightgbm llamafactory peft accelerate datasets trl bitsandbytes

# 3. 关键：覆盖 gguf 为 master 版（PyPI 版本缺 DFLASH）
curl -sL https://github.com/ggerganov/llama.cpp/archive/master.zip -o /tmp/llama.zip
unzip -q /tmp/llama.zip 'llama.cpp-master/gguf-py/gguf/*' -d /tmp/gguf-x
VENV_PKG=venv/lib/python3.14/site-packages/gguf
test -d "$VENV_PKG.bak" || cp -r "$VENV_PKG" "$VENV_PKG.bak"
rm -rf "$VENV_PKG"
cp -r /tmp/gguf-x/llama.cpp-master/gguf-py/gguf "$VENV_PKG"

# 4. 验证（master 版 GGUFWriter/Reader 与 PyPI 版实现差异显著）
python -c "import gguf; print('gguf:', gguf.__file__)"
python -c "from gguf import GGUFReader, GGUFWriter; print('GGUFReader+Writer OK')"
python -c "import lightgbm, peft, transformers, redis, ollama; print('all deps OK')"

# 5. 可选：验证 v7 GGUF 可读（443 tensors, arch=qwen3）
python -c "
from gguf import GGUFReader
r = GGUFReader('/home/jiuben/models/Qwen3-14B-tdx-v7-merged-f16.gguf')
print(f'tensors={len(r.tensors)} metadata={len(r.fields)} arch={r.fields[\"general.architecture\"].parts[-1]}')
"

> **注记**（2026-09-27）：原 README 提到 `DFLASH` 常量，但 llama.cpp master **已删除**该 enum
> （78 个 constants 中均无 QWEN/MOE/EXPERT/FLASH 字段）。覆盖 master 的真正价值在于
> **GGUFWriter/Reader 的实际实现差异**（v7 模型的 metadata 写入/读取需要 master 版）。
```

### 3.5 数据准备

```bash
# 沪深数据（脚本分散；实际用 tdxfeed.full_sync 主链路）
python -m tdxfeed.full_sync                  # 主力（Sina → akshare → Tencent）

# 复权因子（沪深）
python scripts/download_adj_factor_b.py      # baostock, 2020-01-01 起

# 后复权因子（hfq，2026-09 新增）
python scripts/download_hfq_factor.py        # → data/hfq_factor/

# 北交所 / ETF 数据已于 2026-10-21 下线，相关脚本仅保留弃用提示

# 翻倍样本
python scripts/find_double_up_c_v2.py

# 验证（v2 schema：date date32 + 7/5 列）
python scripts/check_schema.py                # 应输出 合规率 100%
find data/parquet/daily -maxdepth 1 -name '*.parquet' ! -name '*.v1.bak.parquet' | wc -l   # 5,235
find data/adj_factor -maxdepth 1 -name '*.parquet' | wc -l      # 5,221
test -f data/double_up/double_up_2020_2026_detail.csv  # 应存在
```

> **v2 schema 状态**（截至 2026-09-27）：
>
> | 数据集 | 文件数 | 列数 | date 类型 |
> |---|---|:---:|---|
> | `data/parquet/daily/` | **5,235** | 7 | `date32[day]` ✓ |
> | `data/adj_factor/` | **5,221** | 5 | `date32[day]` ✓ |
> | `data/hfq_factor/` | 5,221 | 5 | `date32[day]` ✓ |
> | `data/parquet/min15/` | 5,221 | - | `timestamp` |
> | `data/parquet/min60/` | 5,221 | - | `timestamp` |
>
> v1→v2 迁移脚本：`scripts/migrate_v1_to_v2.py`（默认 dry-run，`--apply` 真正写入；备份 `.v1.bak.parquet`）

### 3.6 模型部署

```bash
# 已部署 v5/v6/v7，无需重复
ollama list
# 项目模型（4 个，~116 GB）：
#   qwen3-14b-tdx-v5:latest      (29 GB, eval_loss 0.1958)
#   qwen3-14b-tdx-v6:latest      (29 GB, eval_loss 0.2883)
#   qwen3-14b-tdx-v7:latest      (29 GB, eval_loss 0.1914, 最优)
#   qwen3-14b-tdx-fast:14b       (29 GB, baseline)
# 其他可用模型（4 个，可选清理）：
#   deepseek-r1:14b              (9.0 GB)
#   qwen3:32b                    (20 GB)
#   qwen3:14b                    (9.3 GB)
#   nomic-embed-text:latest      (274 MB, 用于 embedding)
```

如需重新部署：

```bash
bash v5/scripts/deploy_v5.sh
bash v5/scripts/deploy_v6.sh   # 如有
bash v5/scripts/deploy_v7.sh   # 重新部署 v7
```

> **Ollama 启动验证**：
> ```bash
> curl -s http://localhost:11434/api/tags | jq '.models[].name'
> # 应输出 8 个模型名
> ```
>
> **stop token 配置**（必须）：所有 v* 模型 Modelfile 需显式设 `PARAMETER repeat_penalty 1.0`，
> 否则推理会立即生成 1 token 即停。详见 §7.1。

### 3.7 systemd 启用

```bash
# 复制 timer + service
cp systemd/*.{service,timer} ~/.config/systemd/user/

# 启用 linger（systemd --user 跨 session）
sudo loginctl enable-linger $USER

# 加载并启动
systemctl --user daemon-reload
# 每日同步是系统级 tdx-sync.timer（16:30），由 root 启用/重启，不在 --user 列表里
for t in predict-daily plan-e-journal plan-e-weekly canary-health; do
    systemctl --user enable $t.timer
    systemctl --user start $t.timer
done

# 验证
systemctl --user list-timers --all
```

> **Timer 实际时间**（截至 2026-09-27）：
>
> | Timer | systemd 配置 | README §11.1 描述 | 状态 |
> |---|---|---|---|
> | `tdx-sync.timer`（系统级）| 工作日 16:30 | 18:00（旧用户级已退役）| ✓ 已收口 |
> | `predict-daily.timer` | **18:05** daily | 18:30 | ⚠️ 配置领先文档 25min |
> | `plan-e-journal.timer` | **18:10** daily | 18:35 | ⚠️ 配置领先文档 25min |
> | `plan-e-weekly.timer` | 周五 20:00 | 周五 20:00 | ✓ 一致 |
> | `canary-health.timer` | 5 min | 5 min | ✓ 一致 |
>
> 以 systemd 实际配置为准；如需与 README 对齐，编辑 `systemd/*.timer` 后 `daemon-reload`。

### 3.8 数据治理（v2 schema · 2026-09-27）

本项目在 2026-09-27 完成数据管道全面治理，从 v1（已实现漂移）升级到 v2（统一规范）。

#### 3.8.1 治理范围

| 项 | v1 现状（治理前）| v2 目标（治理后）|
|---|---|---|
| `daily.date` 类型 | 三态混存：int64 (5,221) / timestamp[us] (2,469) / timestamp[ns] (345) | 统一 `date32[day]` |
| `daily.adj_factor` 列 | rules 文档声明有，实际**未存** | 确认不存（独立存于 adj_factor/）|
| `adj_factor` schema | rules 文档 2 列，实际**5 列** | 正式 5 列：`code, date, adj_factor, qfq_close, raw_close` |
| `incremental_update` | rules 文档有伪代码，**代码库零引用** | 已落地：`tdxfeed/incremental.py` |
| ETF/可转债日 K | 完全缺失 | Sina 拉取 ~1500 只（v2 治理后）|
| 后复权 hfq | 无 | `data/hfq_factor/`（baostock adjustflag=3）|
| 复权因子缺口 | 2,470 只（实际全是 ETF/可转债）| 已用 Sina + `adj_factor=1` 占位解决 |

> 注：ETF/北交所数据已于 2026-10-21 下线，上表为 v2 治理期历史记录，不代表当前数据集。

#### 3.8.2 新增 / 修订脚本

| 脚本 | 用途 | 命令 |
|---|---|---|
| `scripts/check_schema.py` | schema 合规率扫描 | `venv/bin/python scripts/check_schema.py` |
| `scripts/migrate_v1_to_v2.py` | v1→v2 date32 迁移（备份原文件 `.v1.bak.parquet`） | `venv/bin/python scripts/migrate_v1_to_v2.py --apply` |
| `scripts/verify_v2.py` | 端到端验证（schema + predict 抽样 + canary）| `venv/bin/python scripts/verify_v2.py` |
| `tdxfeed/incremental.py` | 增量更新（Tencent 主源 + Akshare + Sina 兜底，写 v2 schema）| `venv/bin/python -m tdxfeed.incremental` |
| `scripts/download_etf_bond.py` | **已下线**（ETF 数据 2026-10-21 移除；脚本仅保留弃用提示）| — |
| `scripts/download_hfq_factor.py` | 后复权因子下载 | `venv/bin/python scripts/download_hfq_factor.py` |
| `tdxfeed/symbols.py` | 扩展 `group()` / `sym_of_sina()` / `market_of()`（含 bj 类别与 ETF/可转债前缀）| （库）|

#### 3.8.3 关键工程发现（2026-09-27）

1. **baostock / akshare `stock_zh_a_daily` 都不支持 ETF/可转债**（实测 0 rows / JSONDecodeError）——
   仅 Sina K 线支持。2470 adj 缺口全部是 ETF/可转债前缀（11/12/15/51/56/58）。
2. **`daily.close` 复权口径混合**：Sina 源写 raw 价，akshare 源写 qfq 价——这是已知漂移，
   predict.py 通过 `adj_factor merge` 容忍；**严格统一需要 source-tagged 列（v3 议题）**。
3. **TencentSource 真正支持 `start_date/end_date`**（URL 模板带参），是增量更新首选。
4. **gguf master 已删除 `DFLASH` 常量**——78 个 constants 里完全没有 QWEN/MOE/EXPERT/FLASH 字段；
   覆盖 master 的真正价值是 GGUFWriter/Reader 的实现差异（v7 metadata 写入/读取）。

#### 3.8.4 双写期与回滚

迁移脚本默认行为：
- `dry-run`（默认）：仅打印迁移计划，不修改任何文件
- `--apply`：实际写入；**原文件备份为 `.v1.bak.parquet`**，保留 30 天
- 30 天后确认下游兼容后，可执行 `find data/ -name "*.v1.bak.parquet" -delete` 清理

回滚示例：
```bash
# daily 单文件回滚
mv data/parquet/daily/sh600000.v1.bak.parquet data/parquet/daily/sh600000.parquet
```

---

## 4. 配置参数说明

### 4.1 模型版本参数

| 文件 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `models/Modelfile-v7` | `temperature` | 0.3 | 采样温度 |
| `models/Modelfile-v7` | `top_p` | 0.95 | nucleus 采样 |
| `models/Modelfile-v7` | `top_k` | 20 | top-k 采样 |
| `models/Modelfile-v7` | `num_ctx` | 4096 | 上下文窗口 |
| `models/Modelfile-v7` | `repeat_penalty` | 1.0 | 重复惩罚（必须 1.0）|

### 4.2 预测参数（predict.py）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--top N` | 30 | Top N 候选股 |
| `--threshold 0.7` | 0.7 | 高分阈值 |
| `--codes` | (空) | 指定股票列表（逗号分隔）|
| `--workers N` | 8 | 并发进程数 |

### 4.3 回测参数（plan_e_backtest.py）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--hold N` | 10 | 持有天数 |
| `--tp 0.30` | 0.30 | 止盈（30%）|
| `--sl -0.10` | -0.10 | 止损（-10%）|
| `--compare` | false | v1 vs v2 对比 |

### 4.4 环境变量

```bash
# Ollama 配置
export OLLAMA_HOST=localhost
export OLLAMA_PORT=11434
export TDX_LLM_BACKEND=ollama          # runtime.py 走 Ollama；不设则加载本地 transformers+peft
export TDX_OLLAMA_MODEL=qwen3-14b-tdx-v7r-q8

# 数据路径
export TDX_HOME=/home/jiuben/tdx-data-feed
export TDX_DATA_DIR=$TDX_HOME/data

# 数据库（容器化部署用）
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_DB=tdx
export POSTGRES_USER=tdx
export POSTGRES_PASSWORD=xxx

# 凭证
export GITHUB_TOKEN=ghp_xxxxx
```

#### 4.4.1 `.env` 占位符策略（2026-09-27）

`.env` 文件中 3 个密码已改为 shell 变量占位符形式：

```bash
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
GRAFANA_PASSWORD=${GRAFANA_PASSWORD}
```

⚠️ **副作用**：docker-compose 解析 .env 时**不会二次展开** `${VAR}` 语法，
会当作字面字符串。直接 `docker compose up -d` 会导致容器内收到字面 `${POSTGRES_PASSWORD}`，
登录失败。

**正确部署流程**（4 选 1）：

| 方式 | 命令 | 适用 |
|---|---|---|
| **A. Shell 注入** | `export POSTGRES_PASSWORD=$(openssl rand -hex 32); export REDIS_PASSWORD=...; export GRAFANA_PASSWORD=...; docker compose up -d` | 临时 / CI |
| **B. 强制必填** | 改 docker-compose.yml：`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?必须显式注入}` | 防漏配 |
| **C. envsubst** | `envsubst < .env.example > .env && docker compose up -d` | 模板化 |
| **D. 直接写密码** | `sed -i 's/${POSTGRES_PASSWORD}/actual-secret/g' .env` | 本地临时 |

原始 .env 备份：`.env.original.{YYYYMMDD_HHMMSS}`。

### 4.5 容器化配置（deploy/）

| 文件 | 作用 |
|---|---|
| `Dockerfile` | tdx-app 镜像构建 |
| `docker-compose.yml` | 10 个 service 编排 |
| `.env.example` | 环境变量模板（请勿提交 .env）|
| `deploy/prometheus/prometheus.yml` | 监控采集配置 |
| `deploy/grafana/dashboards/tdx-overview.json` | 仪表板 |

---

## 5. 快速上手示例

### 示例 1：单只股票预测（10 秒）

```bash
cd /home/jiuben/tdx-data-feed
source venv/bin/activate

python scripts/predict.py --codes 600519 --top 1
```

**输出**：`data/predictions/predict_*.md` 含分数（daban 为涨停概率 / stage1 为预期 PnL）+ 模型特征。

### 示例 2：全市场预测（默认 daban Top 5）

```bash
python scripts/predict.py                              # small：daban 29 维，Top 5，阈值 0.7
python scripts/predict.py --position-mode full         # full：stage1 回归，Top 20
ls -la data/predictions/predict_latest.md
```

**输出**：`predict_top5_*.csv` + 全部股票 CSV + Markdown 报告（阈值分布以本次报告为准）。

### 示例 3：方案 E v1 vs v2 对比（3 分钟）

```bash
python scripts/plan_e_backtest.py --compare
cat data/plan_e/plan_e_compare_*.md
```

**输出**：v1 31.2% vs v2 35.3% 胜率对比（2026-10-16 报告：670 vs 136 笔）。

### 示例 4：方案 E 参数扫描

```bash
for hold in 5 10 15; do
    for tp in 0.20 0.30 0.50; do
        python scripts/plan_e_backtest.py \
            --hold $hold --tp $tp --sl -0.10 \
            --output data/plan_e/grid_h${hold}_tp${tp}.csv
    done
done
```

### 示例 5：调用 Ollama 推理

```bash
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3-14b-tdx-v7r-q8:latest",
  "prompt": "用一句话分析 600519 茅台当前估值",
  "stream": false,
  "options": {"stop": [], "repeat_penalty": 1.0}
}' | python3 -m json.tool
```

### 示例 6：Docker 一键启动

```bash
cd /home/jiuben/tdx-data-feed
cp .env.example .env && vim .env
docker compose build
docker compose up -d
docker compose logs -f tdx-app
```

### 示例 7：触发 IDE Agent

打开 IDE 对话框，输入：

```text
@tdx-predictor 跑一下今日全市场预测
@tdx-model-trainer 把 v7 部署到 Ollama
@tdx-backtester 跑方案 E 回测
```

---

## 6. 常见使用场景

### 场景 1：每日 18:30 自动跑预测

由 `predict-daily.timer` 触发，无需人工介入。流程：

1. 数据同步（工作日 16:30，系统级 `tdx-sync.timer`）
2. 预测（18:30）
3. 写 journal（18:35）
4. 周报汇总（周五 20:00）

### 场景 2：手工研究某只股票

```bash
# 1. 拉取历史行情
python scripts/download_baostock.py --codes 600519

# 2. 跑预测
python scripts/predict.py --codes 600519 --top 1

# 3. 调 Ollama 深度分析
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3-14b-tdx-v7r-q8:latest",
  "prompt": "深度分析 600519 茅台近 30 日资金面、技术面、基本面",
  "stream": false
}'
```

### 场景 3：调整方案 E 阈值

```bash
# 修改 plan_e_backtest.py 的阈值常量
# 或者用参数覆盖
python scripts/plan_e_backtest.py --threshold 0.10  # T+1 涨幅
python scripts/plan_e_backtest.py --vol-ratio 2.0    # 量比要求
```

### 场景 4：重训模型（增量数据）

```bash
# 1. 拉新数据（生产入口）
venv/bin/python -m tdxfeed.full_sync

# 2. 重训（续训配置）
nohup venv/bin/llamafactory-cli train training/config/tdx_v7_resume.yaml > /tmp/v7.log 2>&1 &

# 3. 合并 + GGUF + 部署
venv/bin/python -c "
import torch
from transformers import AutoModelForCausalLM
from peft import PeftModel
base = AutoModelForCausalLM.from_pretrained(
    '/home/jiuben/models/Qwen3-14B',
    torch_dtype=torch.bfloat16, device_map='cpu', low_cpu_mem_usage=True)
model = PeftModel.from_pretrained(base, 'train/output/qwen3-14b-tdx-v7-resume/checkpoint-1115')
merged = model.merge_and_unload()
merged.save_pretrained('models/Qwen3-14B-tdx-v7r-merged')
"

venv/bin/python /home/jiuben/llama.cpp/convert_hf_to_gguf.py \
  models/Qwen3-14B-tdx-v7r-merged \
  --outfile models/Qwen3-14B-v7r-merged-f16.gguf --outtype f16

ollama rm qwen3-14b-tdx-v7r:latest
ollama create qwen3-14b-tdx-v7r -f /home/jiuben/models/Modelfile-v7r
```

### 场景 5：跨机器部署

```bash
# 1. 源机器打包
rsync -avz --exclude='venv' --exclude='*.gguf' --exclude='logs' \
    jiuben@src:/home/jiuben/tdx-data-feed/ /opt/tdx-data-feed/

# 2. 目标机器重建
cd /opt/tdx-data-feed
python3.14 -m venv venv
. venv/bin/activate && pip install -r requirements.txt
# 覆盖 gguf master（关键步骤）
# 复制 Ollama 模型 + Modelfile
ollama create qwen3-14b-tdx-v7 -f Modelfile-v7
```

### 场景 6：监控指标

```bash
# 健康检查
python scripts/canary_health_check.py

# Prometheus + Grafana（容器化部署）
open http://localhost:3000   # Grafana
open http://localhost:9090   # Prometheus
```

---

## 7. 故障排查指引

### 7.1 模型转换/部署问题

#### ❌ `AttributeError: module 'gguf' has no attribute 'DFLASH'`

**原因**：venv 内的 gguf 0.19.0 缺 DFLASH 属性（v7 模型需要）。

**解决**：从 llama.cpp master 覆盖：

```bash
VENV_PKG=venv/lib/python3.14/site-packages/gguf
test -d "$VENV_PKG.bak" || cp -r "$VENV_PKG" "$VENV_PKG.bak"
rm -rf "$VENV_PKG"
cp -r /tmp/gguf-x/llama.cpp-master/gguf-py/gguf "$VENV_PKG"
```

#### ❌ Ollama 推理只生成 1 token 立即停

**原因**：Modelfile 缺少显式 `repeat_penalty 1.0` 设定。

**解决**：编辑 `models/Modelfile-v7`：

```dockerfile
FROM "/home/jiuben/models/Qwen3-14B-tdx-v7-merged-f16.gguf"
TEMPLATE "{{ .Prompt }}"
PARAMETER temperature 0.3
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_ctx 4096
PARAMETER repeat_penalty 1.0     ← 必须
```

重建：

```bash
ollama rm qwen3-14b-tdx-v7:latest
ollama create qwen3-14b-tdx-v7 -f /home/jiuben/models/Modelfile-v7
```

调用时：

```bash
curl ... -d '{
  "options": {"stop": [], "repeat_penalty": 1.0}  ← 必须
}'
```

#### ❌ 模型 LoRA merge OOM（CUDA out of memory）

**解决**：用 CPU 模式：

```python
base = AutoModelForCausalLM.from_pretrained(
    '/home/jiuben/models/Qwen3-14B',
    torch_dtype=torch.bfloat16,
    device_map="cpu",                    ← CPU
    low_cpu_mem_usage=True
)
```

### 7.2 数据问题

#### ❌ `KeyError: 'adj_factor'` in predict.py

**原因**：重复列。

**解决**：predict.py 已自动 drop，确认使用最新版：

```python
if 'adj_factor' in df.columns:
    df = df.drop(columns=['adj_factor'])
```

#### ❌ baostock / akshare 下载失败

**检查网络**：

```bash
curl -I https://baostock.com
ping baostock.com
```

**使用代理：

```bash
export http_proxy=http://proxy:8080
export https_proxy=http://proxy:8080
```

### 7.3 systemd 问题

#### ❌ timer 不触发

**检查**：

```bash
systemctl status tdx-sync.timer
journalctl -u tdx-sync.service -n 50
```

**修复**：

```bash
sudo loginctl enable-linger $USER
systemctl --user daemon-reload
sudo systemctl restart tdx-sync.timer   # 系统级单元，需要 root
```

### 7.4 GPU 问题

#### ❌ CUDA out of memory

**排查**：

```bash
nvidia-smi
free -h
```

**缓解**：

- 减小 `predict.py --workers`（8 → 4）
- 减小 Ollama `OLLAMA_NUM_PARALLEL`（2 → 1）
- 减小 `num_ctx`（4096 → 2048）

### 7.5 IDE 工作空间问题

#### ❌ 项目 Agent 不显示

**确认**：

```bash
ls /home/jiuben/tdx-data-feed/.codebuddy/agents/*.md
head -5 /home/jiuben/tdx-data-feed/.codebuddy/agents/tdx-predictor.md
# 必须有 frontmatter，且 `tools:` 字段是复数
```

**完全重启**：

```bash
pkill -f "CodeBuddy CN"
sleep 3
# 重新打开 IDE
```

详见 `.codebuddy/INTEGRATIONS.md` 和 `.codebuddy/MEMORY_GUIDE.md`。

### 7.6 容器化问题

#### ❌ GPU passthrough 失败

**检查 NVIDIA Container Toolkit**：

```bash
nvidia-ctk --version
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

**安装**（如未装）：

```bash
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

---

## 8. 项目级 IDE 工作空间（.codebuddy/）

本项目在 `.codebuddy/` 下部署了完整的 7 类组件：

### 8.1 组件清单

| 类型 | 路径 | 数量 |
|---|---|---|
| **Agent** | `.codebuddy/agents/*.md` | 4 |
| **Rule** | `.codebuddy/rules/*.md` | 3 |
| **Skill** | `.codebuddy/skills/<name>/SKILL.md` | 1 |
| **MCP** | `.codebuddy/.mcp.json` | 1 |
| **Memory** | `.codebuddy/memory/*.md` | 2 |
| **Automation** | `.codebuddy/automations/<id>/memory.md` | 2 |
| **Plugin** | IDE 内启用 | - |

### 8.2 4 个 Agent

| Agent | 触发词 | 职责 |
|---|---|---|
| `tdx-data-collector` | 下载股票数据 / akshare / 增量同步 | 数据采集 |
| `tdx-model-trainer` | 训练 / LoRA /合并 / GGUF / 部署 Ollama | 模型训练与合并 |
| `tdx-predictor` | 每日预测 / Top 5 / predict.py | 全市场预测 |
| `tdx-backtester` | 方案E / 胜率 / v1 vs v2 / 实盘 | 策略回测 |

**使用方式**：

```text
@tdx-predictor 跑一下今日全市场预测
使用 tdx-quant-workflow 跑完整 pipeline
```

### 8.3 3 个 Rule（项目约定）

- `tdx-conventions` — 目录结构、命名规范、提交规范
- `tdx-data-schema` — parquet schema 锁定
- `tdx-output-format` — 报告格式规范

### 8.4 集成与记忆

**集成**：通过 IDE 顶部"集成"菜单手动启用（OAuth / API Key），不可通过文件配置。

**记忆**：项目级 `.codebuddy/memory/` 是 Agent 工作上下文，**不显示**在 IDE "记忆"菜单（IDE 记忆菜单是会话级历史）。

详见：
- `.codebuddy/INTEGRATIONS.md`
- `.codebuddy/MEMORY_GUIDE.md`

---

## 9. 容器化部署（Docker Compose）

> **⚠️ 本节为可选部署方式**（截至 2026-09-27）。
> **当前实际部署方式 = venv + systemd**（参见 §3 安装流程）。Docker Compose
> 编排的 10 个 service **从未实际运行过**，仅作为多机/生产环境部署的备选方案。
>
> 选择哪种部署方式？
>
> | 场景 | 推荐 | 章节 |
> |---|---|---|
> | 本地开发（单机） | **venv + systemd** | §3 |
> | 多机部署 / 生产 | Docker Compose | §9 |
> | 混合（venv 推理 + 容器化监控）| Docker Compose（仅启动 postgres/redis/prometheus/grafana）| §9.4 |

### 9.1 一键部署（前提：选择 Docker 方式时）

```bash
cd /home/jiuben/tdx-data-feed
cp .env.example .env.original.${TS}     # 备份原始
cp .env.example .env
# 注入密码（任选 §4.4.1 中 4 种方式之一）
export POSTGRES_PASSWORD=$(openssl rand -hex 32)
export REDIS_PASSWORD=$(openssl rand -hex 32)
export GRAFANA_PASSWORD=$(openssl rand -hex 32)

docker compose build
docker compose up -d
docker compose ps    # 应有 10 个 service
```

### 9.2 服务清单（10 个）

| Service | 端口 | 说明 |
|---|---|---|
| tdx-app | 8080 | 主应用（Python 3.14 + venv）|
| ollama | 11434 | GPU 推理 |
| ollama-exporter | 9835 | Ollama Prometheus 导出 |
| postgres | 5432 | 元数据 |
| postgres-exporter | 9187 | PG Prometheus 导出 |
| redis | 6379 | 缓存 + 消息队列 |
| redis-exporter | 9121 | Redis Prometheus 导出 |
| node-exporter | 9100 | 主机指标 |
| prometheus | 9090 | 监控采集 |
| grafana | 3000 | 仪表板 |

### 9.3 访问入口

| 服务 | URL | 凭据 |
|---|---|---|
| tdx-app Web | http://localhost:8080 | - |
| Ollama API | http://localhost:11434 | - |
| Grafana | http://localhost:3000 | admin / 见 `.env` |
| Prometheus | http://localhost:9090 | - |

详细说明：`deploy/README.md`

### 9.4 混合部署（venv + 部分容器化）

适用场景：本地推理用 venv，但需要 Postgres/Redis/Prometheus/Grafana 容器化监控。

```bash
# 只启动基础设施（不启动 tdx-app / ollama）
docker compose up -d postgres postgres-exporter redis redis-exporter node-exporter prometheus grafana

# 主应用 + Ollama 用 systemd + 本地进程
make timer-enable       # 启用 5 个 systemd timer（数据同步/预测/journal/周报/canary）
ollama serve &          # Ollama 用 systemd 或直接前台
```

此时环境变量指向本地：

```bash
# .env 或 export
OLLAMA_HOST=localhost   # 不是 ollama container
POSTGRES_HOST=localhost # postgres container 暴露的端口
REDIS_HOST=localhost
```

---

## 10. CI/CD 流水线

### 10.1 GitHub Actions

`.github/workflows/docker-publish.yml` 自动构建并推送 Docker 镜像：

| 触发 | 行为 |
|---|---|
| `main` 分支推送 | 推送 `latest` + 主分支 sha 标签 |
| `v*.*.*` 标签 | 推送语义化版本标签（`1.2.3` / `1.2` / `1`）|
| Pull Request | 仅构建验证，不推送 |
| 手动触发 | 自定义标签 |

**目标仓库**：`ghcr.io/jiuben/tdx-data-feed`

**特性**：
- ✅ Provenance attestation（SLSA）
- ✅ SBOM 自动生成
- ✅ Trivy 漏洞扫描 → GitHub Security
- ✅ 多阶段构建缓存

---

## 11. systemd 自动化调度

### 11.1 Timer 清单

| Timer | 时间（实际配置）| README 描述 | 动作 |
|---|---|---|---|
| `tdx-sync.timer`（系统级）| 工作日 16:30 | 18:00（旧用户级已退役）| 全市场增量同步 |
| `predict-daily.timer` | **18:05** daily | 18:30 | 全市场预测 |
| `plan-e-journal.timer` | **18:10** daily | 18:35 | 写实盘 journal |
| `plan-e-weekly.timer` | 周五 20:00 | 周五 20:00 | 周报汇总 |
| `canary-health.timer` | 5 min | 5 min | 健康检查 |

> **修订建议**：实际 systemd timer 时间（18:05 / 18:10）比文档描述领先 25 分钟，
> 这是因为 daily pipeline 调整为"sync 16:30（系统级）→ predict 18:05 → journal 18:10"（sync 原为 18:00 用户级，2026-10-28 退役）
> 的紧凑流程（数据同步后立即预测与写 journal）。如需以文档为准，编辑 `systemd/*.timer` 后
> `daemon-reload`。

### 11.2 启用 / 禁用

```bash
# 启用（预测/归档这几只；同步由系统级 tdx-sync.timer 承担）
systemctl --user enable predict-daily.timer
systemctl --user start predict-daily.timer

# 禁用
systemctl --user disable predict-daily.timer

# 查看状态
systemctl --user list-timers --all
```

### 11.3 日志

```bash
journalctl -u tdx-sync.service -n 50 -f
tail -f logs/canary.log
```

---

## 12. 文档索引

### 12.1 项目内文档

| 文档 | 内容 |
|---|---|
| **README.md**（本文档）| 系统总览 + 入门 |
| `docs/INDEX.md` | **docs/ 目录入口索引**（47 文档按 7 段分类，2026-10-16 新增）|
| `docs/ROADMAP_2026-10-16.md` | **迭代路线图**（4 项：v9 量化 / D→E 联调 / 监控栈 / docs 治理）|
| `docs/WORKFLOW.md` | 工作流全景（程序 + Agent + Skill + MCP）|
| `docs/WORKFLOW_REVIEW_2026-10-16-v2.md` | 工作流审查 v2（最新）|
| `docs/PLAN_E.md` | 方案 E 策略详解 |
| `docs/PLAN_E_LIVE.md` | 方案 E 实盘启动指南 |
| `docs/API.md` | API 接口文档 |
| `docs/ARCHITECTURE.md` | 架构深度说明 |
| `docs/tdx-data-feed-数据字典.md` | 数据 schema |
| `docs/volume-单位治理计划书.md` | 成交量单位治理 |
| `docs/pushplus_bridge_status_2026-10-16.md` | pushplus-bridge 退役记录（2026-10-16）|
| `DELIVERY.md` | 项目交付报告 |
| `CHANGELOG.md` | 变更日志 |
| `CONTRIBUTING.md` | 贡献指南 |
| `SECURITY.md` | 安全策略 |

### 12.2 项目级 IDE 文档

| 文档 | 内容 |
|---|---|
| `.codebuddy/README.md` | 7 类组件索引 |
| `.codebuddy/INTEGRATIONS.md` | 集成启用指南 |
| `.codebuddy/MEMORY_GUIDE.md` | 记忆机制说明 |

### 12.3 容器化文档

| 文档 | 内容 |
|---|---|
| `deploy/README.md` | 容器化部署指南 |
| `deploy/grafana/dashboards/tdx-overview.json` | 仪表板 JSON |

### 12.4 关键脚本索引

| 脚本 | 命令 | 备注 |
|---|---|---|
| 数据采集（主力）| `python -m tdxfeed.full_sync` | Sina → akshare → Tencent 多源兜底 |
| 沪深复权因子 | `python scripts/download_adj_factor_b.py` | baostock 2020-01-01 起 |
| 后复权因子 | `python scripts/download_hfq_factor.py` | → `data/hfq_factor/`（v2 新增）|
| 北交所 / ETF 日 K | — | **已下线（2026-10-21）**，脚本仅保留弃用提示 |
| **增量更新** | `python -m tdxfeed.incremental` | **v2 新增**：Tencent 主源，写 date32 |
| **schema 合规扫描** | `python scripts/check_schema.py` | **v2 新增** |
| **v1→v2 迁移** | `python scripts/migrate_v1_to_v2.py --apply` | **v2 新增**：备份 `.v1.bak.parquet` |
| **端到端验证** | `python scripts/verify_v2.py` | **v2 新增**：schema + predict 抽样 + canary |
| 方案 D 预测 | `python scripts/predict.py` | small=daban/Top5；full=stage1/Top20 |
| 方案 E 回测 | `python scripts/plan_e_backtest.py --compare` | v1 vs v2 |
| 方案 E 实盘 | `python scripts/plan_e_journal.py` | simulate / paper / live |
| 健康检查 | `python scripts/canary_health_check.py` | 6 项 |
| v7 部署 | `bash v5/scripts/deploy_v7.sh` | LoRA → GGUF → Ollama |

### 12.5 Ollama 模型速查

```bash
ollama list

curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3-14b-tdx-v7r-q8:latest",
  "prompt": "分析 600519 茅台",
  "stream": false,
  "options": {"stop": [], "repeat_penalty": 1.0}
}' | python3 -m json.tool
```

### 12.6 部署状态对照表（2026-09-27 实测）

| 组件 | README 描述 | 实际部署状态 | 一致性 |
|---|---|---|:---:|
| Python venv | 3.14 + 依赖 | 3.14.4 + 17/17 依赖通过 | ✓ |
| gguf master | 覆盖（避免 DFLASH）| 已覆盖（master 已删 DFLASH，但 GGUFWriter/Reader 实现差异仍必要）| ✓ |
| Ollama | 0.3.10+ | snap 0.x（HTTP 200，PID 7583）| ✓ |
| Ollama 模型 | 4 个 tdx | 4 个 tdx（v5/v6/v7/fast:14b）+ 4 个其他（deepseek-r1, qwen3:32b/14b, nomic-embed）| ⚠️ |
| data/parquet/daily | ≥ 5000 | 5,235（5,221 沪深个股 + 14 指数；v2 date32 合规率 100%）| ✓ |
| data/adj_factor | ≥ 5000 | 5,221（v2 date32 合规率 100%）| ✓ |
| systemd timer | 5 个 | 用户级 4 个（canary-health/predict-daily/plan-e-journal/plan-e-weekly）+ 系统级 tdx-sync | ✓ |
| predict-daily 时间 | 18:30 | 18:05 | ⚠️ |
| plan-e-journal 时间 | 18:35 | 18:10 | ⚠️ |
| Docker Compose | 10 service 部署 | **未部署**（容器是 GitHub MCP 服务，与本项目无关）| ⚠️ |
| `.env` | `cp .env.example .env` | 已创建，3 个密码占位符化（`${VAR}` 形式）| ✓ |
| GPU | 推荐 4090 24GB | RTX 5060 Ti 16GB | ⚠️ |
| `.github/workflows/` | docker-publish | 4 个（docker-publish + lint + quick-sop + sync-docs）| ✓ |
| `.codebuddy/` 7 类 | Agent×4 + Rule×3 + Skill×1 + MCP×1 + Memory | 全部就位 | ✓ |
| 文档完整性 | 15 份索引文档 | 全部存在 | ✓ |

#### 12.6.1 偏差处理建议

| 类型 | 建议动作 |
|---|---|
| ⚠️ timer 时间 | 以 systemd 实际配置为准；如需文档对齐，编辑 `systemd/*.timer` 后 `daemon-reload` |
| ⚠️ Ollama 多 4 个模型 | 清理 `ollama rm <model>` 或更新 §3.6 描述完整列表 |
| ⚠️ Docker 未部署 | 不必强制；当前 venv+systemd 部署已可运行；如需容器化参考 §9.4 混合模式 |
| ⚠️ GPU 不足 | 监控 OOM；`predict.py --workers 8 → 4`；Ollama `OLLAMA_NUM_PARALLEL=1` |

---

## 🎯 速查命令卡片

```bash
# === 一键查看系统状态 ===
systemctl --user list-timers --all
ollama list
find data/parquet/daily -maxdepth 1 -name '*.parquet' | wc -l
tail logs/canary.log
cat data/predictions/predict_latest.md | head -40

# === 一键跑完整预测（默认 small：daban Top5）===
python scripts/predict.py

# === 一键跑方案 E ===
python scripts/plan_e_backtest.py --compare

# === 一键模拟验证 ===
python scripts/plan_e_journal.py --mode simulate \
    --start 2026-08-27 --end 2026-09-25 --capital 100000

# === 一键启动 IDE Agent ===
@tdx-predictor 跑一下今日全市场预测
@tdx-backtester 跑方案 E 回测

# === 一键容器化部署 ===
docker compose up -d
docker compose logs -f tdx-app
```

---

## 📜 许可

**私有项目** · 仅限内部使用 · 未经授权不得传播

---

> **最后更新**：2026-10-21 · **维护者**：jiuben
> **系统健康度**：canary 7/7 通过 · Ollama 已部署 7 个模型（4 个 tdx）· 数据 5,235 日K / 5,221 复权因子 · 项目级 IDE 配置 38 个文件
