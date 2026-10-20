# 工作空间构成盘点 与 LLM 训练/模型工作相关性判定

（2026-10-20 11:25）

> 审计对象：`/home/jiuben/tdx-data-feed`（工作空间根）
> 判定问题：LLM 训练及相关模型工作是否与本工作空间"完全无关"
> 方法：目录实测 + 全仓 grep + venv 依赖实测 + systemd 定时器实况 + 服务监听实况（非记忆推断）

---

## 📊 核心指标

| # | 指标 | 实测值 | 判定 |
|---|---|---|---|
| 1 | 工作空间总体量 | 代码+文档 ~2 MB，**数据 6.9 G**，**模型 30 G**，venv 8.7 G，训练输出另计 | 三层资产 |
| 2 | 生产决策链路的模型类型 | **LightGBM**（`lgb.Booster`），非 LLM | LLM 无关 |
| 3 | 生产定时器中的 LLM 任务 | **0 个**（13 个 timer 全查） | LLM 无关 |
| 4 | LLM 线代码文件数 | **≈ 100+**（training 3 py + 17 yaml、v5 65 py、scripts 12 个、agents 3 py、v5/mcp_servers 4 个） | 强相关 |
| 5 | LLM 权重资产 | **30 G**（`models/Qwen3-14B-tdx-v9-merged` 28 G + `_archive` 2.2 G）+ `train/output/` 10 个 LoRA adapter | 强相关 |
| 6 | LLM 训练语料 | **≈ 800 M**（v4 271M / v4fwd 415M / v5_train 44M / v6_train 20M / distill 51M） | 强相关 |
| 7 | 已在运行的 LLM 推理服务 | **Ollama active，监听 127.0.0.1:11434**，7 个模型（v4fwd/v7r-q8/v7r1115/fast/deepseek-r1/qwen3:14b/nomic-embed） | 运行中 |
| 8 | 生产对 LLM 的**运行时**依赖 | **1 处**（canary 探活 `check_ollama()`） | 弱耦合 |
| 9 | 生产对 LLM 的**资源**依赖 | 宿主 30 G 内存，Ollama 常驻峰值 25.4 G | 强耦合（已致 2 次 OOM） |

---

## 🎯 Top 10 组成明细

| # | 区域 | 路径 | 体量 | 内容 | 与 LLM 关系 |
|---|---|---|---|---|---|
| 1 | 数据采集与特征 | `tdxfeed/` 26 py | 436 K | Sina/akshare 抓取、Parquet 落盘、`features_5d.py`（29 维 Polars/LightGBM 特征）、`factors_candidates.py` | ❌ 无关 |
| 2 | 行情原始数据 | `data/parquet/` + `sh/ sz/*.day` | 6.9 G | 46434 个 parquet + 7703 个 .day 原始二进制行情 | ❌ 无关 |
| 3 | 生产推理 | `scripts/predict.py` | — | **唯一模型加载 = `lgb.Booster(model_file=...)`**（489/540/696/704 行） | ❌ 无关（GBDT） |
| 4 | 策略回测 | `scripts/plan_e_backtest.py` | — | grep `lgb\|Booster\|torch\|transformers\|ollama` **零命中** = 纯规则 | ❌ 无关 |
| 5 | 因子/回测库 | `quant/` 15 py、`data/double_up/` 22972 样本 | 384 K | 因子引擎、CV 工具、翻倍样本 | ❌ 无关 |
| 6 | **LLM 训练线** | `training/`（3 py + 17 yaml + eval） | 280 K | LLaMA-Factory 0.9.5 QLoRA：v5/v7/v8/v9/v9r 配置、`train_lora.py`、`run_v4_eval.py` | ✅ **强相关** |
| 7 | **LLM 权重** | `models/` 30 G、`train/output/` 10 个 adapter | 30 G+ | 合并模型 `Qwen3-14B-tdx-v9-merged` 28 G、v5~v9r 各代 LoRA | ✅ **强相关** |
| 8 | **LLM 语料** | `data/v4 v4fwd v5_train v6_train distill` | ≈800 M | 文本化训练集与蒸馏产物 | ✅ **强相关** |
| 9 | **LLM 推理服务** | `v5/mcp_servers/llm_ollama.py`、`vllm_health.py`、`agents/runtime.py`、`scripts/llm_signal_plan_e.py` | — | MCP 5 工具（chat/embed/reason/classify/judge）、vLLM→Ollama 回退链、SkillRuntime 本地 peft 加载 | ✅ **强相关** |
| 10 | 部署与运维 | `systemd/`(11 svc+9 timer)、`deploy/`、`skills/`(5 yaml)、`canary` | 92 K/96 K | 调度、容器化（Grafana 面板含 Ollama API）、技能配置 | ⚠️ **间接** |

---

## 1. 工作空间实际组成（分区盘点）

### 1.1 代码与逻辑
```
tdxfeed/    26 py  436K   数据采集 / 特征工程 / 因子 / 新鲜度守卫
scripts/    99 py + 23 sh     生产脚本（predict/plan_e/canary/训练/评估 混放）
quant/      15 py  384K   多因子引擎、CV 工具
agents/      3 py  128K   SkillRuntime / orchestrator / cli（含本地 LLM 加载）
tests/      15 py  400K   单元测试（96 passed 基线）
v5/         65 py + 5310 jsonl  154M   历史 v5 代际（LLM 评估/量化/自动化）
_archive/   10 py  236K   早期脚本（含 _archive/mcp/servers/vault.py）
```

### 1.2 配置与依赖
| 文件 | 作用 | LLM 相关 |
|---|---|---|
| `requirements.txt` / `.lock` | 依赖声明与精确锁定 | ✅ torch 2.11.0+cu128 / transformers 4.57.6 / peft 0.18.1 / llamafactory 0.9.5 / bitsandbytes 0.50.2 / accelerate / datasets 4.0.0 / gguf 0.19.0 / sentencepiece |
| `config.yaml` / `core_stocks.json` | 运行参数 / 核心股票池 | ❌ |
| `ruff.toml` / `.pre-commit-config.yaml` / `Makefile` | 静态检查与任务编排 | ❌ |
| `Dockerfile` / `docker-compose.yml` / `deploy/` | 容器化（entrypoint 自动 `ollama pull ${TDX_OLLAMA_MODEL:-qwen3-14b-tdx-v7}`；Grafana 面板含 Ollama API） | ⚠️ |
| `systemd/` 11 service + 9 timer | 调度 | ❌ 生产 timer 无 LLM 任务 |
| `skills/` 5 yaml | 技能声明 | ⚠️ `kline-analysis`(qwen3-14b-tdx, transformers+peft)、`financial-summary`/`news-summary`(qwen3-7b，仅档位标注)、`adj-factor`/`indicator-match`(model_id: none) |
| `.codebuddy/skills/` 3 个 | 项目级技能（quant-workflow / obsidian 两项） | ❌ |
| `.codebuddy/rules/` | 项目规则（schema v2 + tcb 规则集） | ❌ |

### 1.3 数据
| 目录 | 体量 | 内容 | LLM 相关 |
|---|---|---|---|
| `data/parquet/` `data/adj_factor/` | 6.9 G（46434 parquet） | 日 K + 复权因子（schema 冻结） | ❌ |
| `sh/ sz/` | 7703 个 .day | 通达信原始二进制 | ❌ |
| `data/double_up/` | 404 M | 22972 翻倍样本 + LightGBM 模型 `.txt` | ❌（GBDT） |
| `data/v4 v4fwd v5_train v6_train distill` | ≈800 M | LLM 文本训练集 + 蒸馏产物 | ✅ |
| `data/predictions/` | — | 每日预测（LightGBM 产出） | ❌ |
| `data/plan_e/` | — | 实盘 journal + `llm_sig_*.csv`（LLM 信号） | ⚠️ 混合目录 |
| `data/factors industry sentiment valuation concept` | — | 因子/行业/情绪/估值 | ❌ |

### 1.4 模型与运行时
- `models/`：**30 G** — `Qwen3-14B-tdx-v9-merged`（28 G，v9 已判定失败）+ `_archive/`（2.2 G，v9 失败备份）
- `train/output/`：10 个 LoRA adapter 目录（v5 / v5-simple / v6-gpu1 / v7 / v7-resume / v8-merged / v8-resume / **v9r（失败）** / verify-single）
- `venv/` 8.7 G、`logs/` 12 M
- Ollama：**snap.ollama.listener active，监听 127.0.0.1:11434**，已装 7 个模型（v4fwd 15G / v7r-q8 15G / v7r1115 29G / fast 29G / deepseek-r1:14b / qwen3:14b / nomic-embed-text）

---

## 2. 明确结论

### 2.1 与 LLM 训练**无关**的部分（可直接认定为非 LLM 技术栈）

| 判定项 | 证据 |
|---|---|
| **生产选股推理** | `scripts/predict.py` 模型加载仅 `lgb.Booster(model_file=ACTIVE_MODEL_PATH)`（行 489/540/696/704），**无 torch / transformers / peft / ollama 引用**；默认模型 `daban` 是 LightGBM 文本分类器 |
| **方案E 回测与实盘 journal** | `plan_e_backtest.py` 对 `lgb\|Booster\|torch\|transformers\|ollama` **零命中** = 纯规则（翻倍事件 + 次日涨幅 + 量比阈值） |
| **全部生产定时器** | 13 个 timer 全查：`tdxfeed-sync`(18:00) / `predict-daily`(18:05) / `plan-e-journal`(18:10) / `plan-e-monitor`(18:40) / `plan-e-weekly` / `tdx-clean-logs` / `canary-health` / `trainsh-health` —— **无一条执行 LLM 训练或 LLM 推理** |
| **LLM 信号产物不进入闭环** | `data/plan_e/llm_sig_*.csv` 仅被离线脚本 `compare_plan_e_signals.py`、`oracle_plan_e_ceiling.py` 消费，**生产 journal/monitor 不读取** |
| **数据采集 / 特征 / 因子** | `tdxfeed/`、`quant/`、`data/parquet` 全为数值计算与 IO，无模型 |
| **`v5/` 与生产代码解耦** | 全仓 grep `from v5` / `import v5` **零命中**；`agents/`、`skills/` 亦**未被任何 systemd service 调用** |

> 结论：**"打板选股 + 方案E 回测"这条实盘决策链路，与 LLM 训练及大模型推理完全无关**，它是一条 LightGBM + 规则引擎的纯 CPU 链路。

### 2.2 与 LLM **强相关**（直接相关）的部分

| 类别 | 具体位置 | 性质 |
|---|---|---|
| 训练代码 | `training/config/*.yaml`(17)、`training/scripts/train_lora.py`、`training/eval/run_v4_eval.py` | LLaMA-Factory 0.9.5 + QLoRA（Qwen3-14B, rank 8/16） |
| 权重资产 | `models/Qwen3-14B-tdx-v9-merged` 28 G、`train/output/*` 10 个 adapter | v9/v9r 已判定 mode collapse 失败，**仍占 ~30 G** |
| 训练语料 | `data/v4`(271M) `data/v4fwd`(415M) `data/v5_train`(44M) `data/v6_train`(20M) `data/distill`(51M) | JSONL 文本训练集 |
| 推理服务封装 | `v5/mcp_servers/llm_ollama.py`（MCP 5 工具）、`vllm_health.py`（vLLM→Ollama 回退）、`agents/runtime.py`（`transformers+peft` 本地加载 14B LoRA，可 `TDX_LLM_BACKEND=ollama` 切换）、`scripts/llm_signal_plan_e.py`（Ollama HTTP 11434） | 真实可运行的 LLM 推理路径 |
| 工具链 | `v5/scripts/quantize_awq*.py`、`scripts/merge_v5_v6_lora.py`、`scripts/batch_distill.py`、`scripts/distill_outputs.py`、依赖 `gguf` | 量化/合并/蒸馏 |
| 部署 | `deploy/scripts/docker-entrypoint.sh` 自动 `ollama pull`；Grafana 面板含 Ollama API 链接 | 容器链路带 LLM 镜像 |

### 2.3 间接关联（4 条，按风险排序）

| # | 关联 | 机制 | 风险 | 实证 |
|---|---|---|---|---|
| 1 | **资源竞争** | Ollama 常驻 7 个模型（峰值 25.4 G）挤占宿主 30 G 内存；与训练/推理进程叠加触发 **Kernel OOM Killer** | 🔴 高（已发生） | 2026-10-16/17 两次 OOM 杀掉训练进程；曾致 v9/v9r 评估中断 |
| 2 | **运维健康检查耦合** | `canary_health_check.py` 的 7 维中含 `check_ollama()` → `GET http://localhost:11434/api/tags`；Ollama 挂 → canary 报 err（**但不影响 predict 出结果**） | 🟡 中（噪音源） | 实测 Ollama 正在运行；Ollama 停机时 canary 会红 |
| 3 | **运行时按需加载** | `agents/runtime.py` 的 SkillRuntime 对配置了 `llm` 的 skill 会**本地 transformers+peft 加载 14B LoRA**（`skills/kline-analysis.yaml` 即配置 `qwen3-14b-tdx`） | 🟡 中 | 当前 systemd 未调用 agents/skills，属"可触发未触发" |
| 4 | **结构混放** | 12 个 LLM 脚本与生产脚本同放 `scripts/` 根（`llm_signal_plan_e.py`、`merge_v5_v6_lora.py`、`batch_distill.py`、`finalize_eval.py`…）；`v5/` 5712 文件与生产同仓；`data/plan_e/` 内 LLM 与 LightGBM 产物同目录 | 🟡 中（误触发/误读） | 已发生过"误以为 predict 用 LLM"的认知偏差 |

---

## 📈 统计分布（LLM 成分在三类资产中的占比）

| 资产类别 | LLM 相关 | 非 LLM 相关 | 判定 |
|---|---|---|---|
| 决策链（predict / plan_e / journal / monitor / 回测） | **0%** | 100% | 纯 LightGBM + 规则 |
| 数据层（parquet / adj_factor / .day / double_up / factors） | ≈0%（仅 800 M 独立语料目录） | 100% | 数值数据 |
| 资产层（models / train/output / v5 / training / agents） | **≈90%** | 少量 | LLM 历史线 |
| 依赖层（requirements + venv 8.7 G） | torch/transformers/peft/llamafactory/gguf 等 **7 个核心包** | lightgbm/polars/akshare 等 | 双栈共存 |
| 调度层（13 timer） | 0% | 100% | 无 LLM 任务 |

> 结构性判断：**这是"生产栈 = GBDT，研究栈 = LLM"的双栈工作空间**，LLM 栈已因连续两代 mode collapse 被判定止损，但**资产未退出、推理服务未下线、运维探活未摘除**，因而表现为"决策无关、结构强相关、运行时有弱耦合"。

---

## ⚠️ 风险提示

1. **"完全无关"的判断不成立**：若对外声明"该工作空间不涉及 LLM 训练与模型工作"，将与仓库事实冲突——存在 30 G 权重、800 M 语料、17 份训练配置、12 个 LLM 脚本与 1 个运行中的 Ollama 服务。**准确表述应为"生产决策链路不依赖 LLM，但仓库内保留 LLM 研究线资产"**。
2. **存量权重风险**：`models/Qwen3-14B-tdx-v9-merged`(28 G) 对应的 v9 已判定失败并归档 2.2 G，其余 28 G 仍为"未标注失败"的活跃目录，存在被误用/误部署风险（v9r 已有 `FAILED_DO_NOT_USE.md`，v9-merged **没有**）。
3. **资源风险**：Ollama 常驻 25 G 与宿主机 30 G 内存的余量极小，任何训练/批量推理都可能触发 OOM Killer，**波及的不只是 LLM 线**（2026-10-17 OOM 杀进程时同时影响训练与后处理合并）。
4. **运维噪音**：`canary` 依赖 Ollama 存活，与真实生产（LightGBM）健康度无关，会制造"红色告警但生产正常"的误判。
5. **误触发风险**：12 个 LLM 脚本与生产脚本同目录，且 `scripts/canary_health_check.py`、`scripts/plan_e_confidence.py` 含 LLM 字样（前者仅探活、后者仅比对离线信号），人工巡检时易误判。
6. **数值类结论的边界**：本文所有"占比/数量"为 2026-10-20 磁盘实测快照；后续清理权重/归档 v5 会使数字失效。

---

## 🔧 下一步建议

1. **立刻做（低成本，消除误用与误判）**
   - 给 `models/Qwen3-14B-tdx-v9-merged` 补 `FAILED_DO_NOT_USE.md`（与 v9r 对齐），避免 28 G 失败权重被误部署。
   - 把 canary 的 Ollama 维度标注为**非生产依赖**（或移到独立 `check_llm_infra()` 分组），使生产告警不再被 LLM 状态污染。
2. **结构隔离（中期）**
   - 把 12 个 LLM 脚本迁出 `scripts/` 根目录 → `scripts/llm/`（与生产脚本物理分区），并在 `Makefile` 分离 target；`v5/` 标注为只读历史区。
   - `data/plan_e/` 内 LLM 信号产物移入 `data/plan_e/llm/` 子目录。
3. **资源治理（按需）**
   - 若确认 LLM 线长期止损：卸载 Ollama 中已不用的 7 个模型中的冗余项（保留 1~2 个）、评估 `models/` 28 G 清理，释放内存与磁盘。
   - 保留 `data/v4fwd` + `train/output/qwen3-14b-tdx-v7r` 作"若重启 LLM 时的对照基线"，其余可清。
4. **需要你决策的信息缺口**（见下节）——这几项我无法从代码推断。

---

## 需补充确认的信息（信息不足项）

| # | 待确认 | 为什么必须问你 | 影响决策 |
|---|---|---|---|
| 1 | canary 是否保留 Ollama 探活维度 | 取决于生产是否还打算用 LLM 出信号 | 决定 canary 维度数与告警口径 |
| 2 | `models/` 28 G 与 `train/output/` 10 个 adapter 的保留策略 | 磁盘与内存是本机唯一算力，LLM 线已止损 | 决定是否清理、何时清理 |
| 3 | `skills/` 5 个 yaml + `agents/runtime.py` 是否仍在任何流程中被调用 | 当前 systemd 未调用，但存在手工/外部调用可能 | 决定本地 14B 加载路径是否需保留、是否要隔离 |
| 4 | `v5/`（5712 文件、LLM 评估代际）是否归档为独立只读目录/独立仓 | 体积 154 M，混放导致认知偏差 | 决定 v5 处置方式 |
| 5 | `deploy/` 容器链路（含自动 `ollama pull`）是否仍在使用 | 影响部署侧 LLM 依赖是否要摘除 | 决定容器配置是否改 |
| 6 | 是否要做一次"LLM 无关化"隔离（LLM 线整体迁出主仓 + 独立 venv/systemd scope） | 属于架构级动作，成本与收益需你判断 | 决定后续所有清理动作的组织方式 |

---

## 事实边界与自评

- **已实测核实**：目录体量与文件计数（`du`/`find`）、依赖版本（`pip list`）、生产脚本模型加载方式（`grep` 源码行）、定时器与监听实况（`systemctl`/`ss`）、Ollama 模型清单（`ollama list`）、v5 与主包耦合（`grep`）、agents/skills 是否被 systemd 调用（`grep`）。
- **未核实（需你确认）**：Ollama/vLLM 的实际调用方（是否有外部系统直连 11434）、`skills/` 的真实使用方、`deploy/` 是否在用、宿主机除本仓外是否还有其他 LLM 服务。
- **置信度**：目录构成与依赖清单 **高（0.95）**；"生产链路与 LLM 无关" **高（0.92，有源码级 grep + 定时器实况双重证据）**；"间接关联仅 4 条" **中高（0.75**，可能存在仓外直连 Ollama 的调用方未覆盖）**；占比类数字 **高（实测）但时效性弱**。

---

生成时间：2026-10-20 11:25:00
生成者：CodeBuddy（工作空间审计）
数据日期：2026-10-20（磁盘实测快照）
模型版本：不适用（审计类文档）
