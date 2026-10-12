# ARCHITECTURE.md · tdx-data-feed 架构决策记录

> 本文档是 **架构决策记录**（Architecture Decision Records，ADR），记录所有关键技术选型、系统分层设计、模块职责划分、数据流与依赖关系，以及每项决策的背景、备选方案、权衡和最终结论。
>
> 最后更新：2026-09-27 · 维护者：jiuben

---

## 目录

- [ADR 索引](#adr-索引)
- [ADR-001 · 选择 Qwen3-14B 作为基础 LLM](#adr-001--选择-qwen3-14b-作为基础-llm)
- [ADR-002 · 数据源选型：baostock + akshare](#adr-002--数据源选型baostock--akshare)
- [ADR-003 · 存储格式：Parquet + JSON](#adr-003--存储格式parquet--json)
- [ADR-004 · 模型训练框架：LLaMA-Factory + LoRA](#adr-004--模型训练框架llama-factory--lora)
- [ADR-005 · 部署平台：Ollama + GGUF](#adr-005--部署平台ollama--gguf)
- [ADR-006 · 模型迭代策略：v5→v6→v7](#adr-006--模型迭代策略v5v6v7)
- [ADR-007 · 量化方案 D 选型：LightGBM](#adr-007--量化方案-d-选型lightgbm)
- [ADR-008 · 方案 E 策略：v1→v2 升级](#adr-008--方案-e-策略v1v2-升级)
- [ADR-009 · 编排调度：systemd user timer](#adr-009--编排调度systemd-user-timer)
- [ADR-010 · 监控告警：自研 canary](#adr-010--监控告警自研-canary)
- [系统分层设计](#系统分层设计)
- [模块职责矩阵](#模块职责矩阵)
- [数据流图](#数据流图)
- [依赖关系图](#依赖关系图)

---

## ADR 索引

| ADR | 标题 | 状态 | 日期 |
|---|---|---|---|
| [ADR-001](#adr-001--选择-qwen3-14b-作为基础-llm) | 选择 Qwen3-14B 作为基础 LLM | 已采纳 | 2026-09-22 |
| [ADR-002](#adr-002--数据源选型baostock--akshare) | 数据源选型：baostock + akshare | 已采纳 | 2026-09-15 |
| [ADR-003](#adr-003--存储格式parquet--json) | 存储格式：Parquet + JSON | 已采纳 | 2026-09-15 |
| [ADR-004](#adr-004--模型训练框架llama-factory--lora) | 模型训练框架：LLaMA-Factory + LoRA | 已采纳 | 2026-09-22 |
| [ADR-005](#adr-005--部署平台ollama--gguf) | 部署平台：Ollama + GGUF | 已采纳 | 2026-09-25 |
| [ADR-006](#adr-006--模型迭代策略v5v6v7) | 模型迭代策略：v5→v6→v7 | 已采纳 | 2026-09-25 |
| [ADR-007](#adr-007--量化方案-d-选型lightgbm) | 量化方案 D 选型：LightGBM | 已采纳 | 2026-09-25 |
| [ADR-008](#adr-008--方案-e-策略v1v2-升级) | 方案 E 策略：v1→v2 升级 | 已采纳 | 2026-09-26 |
| [ADR-009](#adr-009--编排调度systemd-user-timer) | 编排调度：systemd user timer | 已采纳 | 2026-09-25 |
| [ADR-010](#adr-010--监控告警自研-canary) | 监控告警：自研 canary | 已采纳 | 2026-09-27 |

---

## ADR-001 · 选择 Qwen3-14B 作为基础 LLM

### 背景

需要选型一个**中文 + 量化分析能力**的 LLM 作为微调基础，需要权衡中文能力、长上下文支持、本地部署可行性。

### 备选方案

| 方案 | 优势 | 劣势 | 评估 |
|---|---|---|---|
| **Qwen3-14B**（选）| 中文原生 / 长上下文 32K / 14B 显存友好 / Apache 2.0 | - | ⭐⭐⭐⭐⭐ |
| DeepSeek-R1-14B | 推理强 | 中文弱 / 显存大 | ⭐⭐⭐ |
| Qwen2.5-32B | 性能强 | 显存 24GB+ / 量化慢 | ⭐⭐⭐ |
| Yi-1.5-34B | 中文好 | 显存 40GB+ | ⭐⭐ |
| Llama3-70B | 通用强 | 中文弱 / 显存 80GB+ | ⭐ |

### 决策

选择 **Qwen3-14B** 作为基础模型。

### 权衡

- ✅ 中文原生支持，A股术语理解无障碍
- ✅ 14B 适合单卡 16GB + bf16（带 offload 即可）
- ✅ Apache 2.0 商用许可
- ✅ HuggingFace 生态完善（transformers / peft 直接支持）
- ❌ 比 7B 慢 ~2x，但比 32B 快 ~3x

### 结论

Qwen3-14B 在中文能力 / 显存占用 / 微调友好度上达到最佳平衡点。

---

## ADR-002 · 数据源选型：baostock + akshare

### 背景

需要全 A 股 5565 只股的日 K + 复权因子 + 财务数据。涉及沪深（sh/sz）和北交所（bj）两个市场。

### 备选方案

| 方案 | 沪深 | 北交所 | 复权因子 | 财务 | 评估 |
|---|---|---|---|---|---|
| **baostock + akshare**（选）| ✅ 免费稳定 | ⚠ akshare | ✅ | ✅ | ⭐⭐⭐⭐⭐ |
| tushare pro | ✅ 实时 | ✅ | ✅ | ✅ | ⚠ 限频 200/分钟，要积分 |
| 东方财富 choice | ✅ | ✅ | ✅ | ✅ | ⚠ 收费 |
| 同花顺 ifind | ✅ | ✅ | ✅ | ✅ | ❌ 收费 |
| wind | ✅ | ✅ | ✅ | ✅ | ❌ 极贵 |

### 决策

- **沪深日 K**：`baostock`（稳定，复权准确，免费无限制）
- **沪深复权因子**：`baostock` query_history_k_data_plus(adjustflag="2")
- **北交所日 K**：`akshare.stock_zh_a_hist`（带 920XXX 前缀）
- **北交所复权因子**：`akshare` 新浪源（baostock 不支持 920XXX）

### 权衡

- ✅ 全免费，无 API 限制
- ✅ baostock 复权因子精度高
- ⚠ akshare 北交所偶发断连（已在 `test_bjs_akshare.py` 加冒烟测试）
- ❌ 需要维护两套数据源

### 结论

双源混合（baostock 主 + akshare 兜底）是当前免费场景下的最优解。

---

## ADR-003 · 存储格式：Parquet + JSON

### 背景

每天产生 5565+8035=13600 个新文件，需要选择高效、可压缩、支持 pandas 的存储格式。

### 备选方案

| 方案 | 压缩比 | 读速度 | pandas 支持 | 评估 |
|---|---|---|---|---|
| **Parquet**（选）| 10x | 快 | ✅ | ⭐⭐⭐⭐⭐ |
| CSV | 1x | 慢 | ✅ | ⭐⭐ |
| HDF5 | 5x | 快 | ✅ | ⭐⭐⭐ |
| Feather | 8x | 极快 | ✅ | ⭐⭐⭐⭐ |
| SQLite | 3x | 中 | ⚠ | ⭐⭐ |

### 决策

- **日 K / 复权因子**：`Parquet`（snappy 压缩）
- **元信息（股票清单 / 行业）**：`JSON`
- **特征 / 训练数据**：`CSV`（易读，方便 git diff）
- **报告 / 日志**：`Markdown` + `JSONL`

### 权衡

- ✅ Parquet 列式存储，pandas 读取快（8035 文件 < 1 分钟）
- ✅ snappy 压缩比 ~10x（5565 文件约 200MB）
- ❌ 不能直接 cat 看（需 parquet-tools）

### 结论

Parquet 是时序金融数据的最佳选择，JSON 用于结构化元信息。

---

## ADR-004 · 模型训练框架：LLaMA-Factory + LoRA

### 背景

需要选择模型微调框架，支持 LoRA + bf16 + 多数据集。

### 备选方案

| 方案 | LoRA | bf16 | 多数据集 | 评估 |
|---|---|---|---|---|
| **LLaMA-Factory**（选）| ✅ | ✅ | ✅ | ⭐⭐⭐⭐⭐ |
| transformers Trainer | ✅ | ✅ | ⚠ 需手写 | ⭐⭐⭐ |
| trl SFTTrainer | ✅ | ✅ | ⚠ | ⭐⭐⭐⭐ |
| axolotl | ✅ | ✅ | ✅ | ⭐⭐⭐⭐ |
| unsloth | ✅ | ✅ | ⚠ 仅 llama | ⭐⭐⭐ |

### 决策

- **训练框架**：`LLaMA-Factory 0.9.5`
- **微调方法**：`LoRA`（rank=8 / 16，alpha=16 / 32）
- **精度**：`bf16` + `paged_adamw_8bit`
- **目标模块**：`q_proj / k_proj / v_proj / o_proj / gate_proj / up_proj / down_proj`

### 权衡

- ✅ LLaMA-Factory 一行 yaml 配齐
- ✅ peft 0.20.0 + transformers 4.57.6 兼容良好
- ✅ LoRA r=8 时只训练 0.16% 参数（~24M / 14B）
- ❌ 多 GPU 同步效率比 deepspeed 略低
- ⚠ rank=8 vs 16 不兼容（v5 vs v6），无法直接合并（v8 fusion 未完成）

### 结论

LLaMA-Factory + LoRA 是当前性价比最高的微调方案。

---

## ADR-005 · 部署平台：Ollama + GGUF

### 背景

训练好的模型需要本地部署，提供 HTTP API 给上层应用调用。

### 备选方案

| 方案 | 易用性 | 性能 | API | 评估 |
|---|---|---|---|---|
| **Ollama + GGUF**（选）| ✅ | 30 t/s | REST | ⭐⭐⭐⭐⭐ |
| vLLM | ⚠ 配置复杂 | 200 t/s | OpenAI 兼容 | ⭐⭐⭐⭐ |
| Text Generation WebUI | ⚠ | 中 | REST | ⭐⭐⭐ |
| llama.cpp 直接调用 | ❌ | 30 t/s | C++ | ⭐⭐ |
| LM Studio | ✅ | 30 t/s | GUI | ⭐⭐⭐ |

### 决策

- **部署平台**：`Ollama`
- **模型格式**：`GGUF f16`（精度无损，~29.5GB）
- **HTTP API**：`http://localhost:11434/api/generate`
- **量化等级**：暂不量化（先保精度，速度可接受 30 t/s）

### 权衡

- ✅ Ollama 一行 `ollama create` 部署
- ✅ 自动支持 Modelfile / template / stop tokens
- ✅ 内置 KV cache + flash attention
- ⚠ f16 占空间（4 个模型 × 29GB = 120GB）
- ❌ GGUF 转换需要 llama.cpp + gguf ≥ 0.19（含 DFLASH 属性）

### 结论

Ollama + GGUF f16 是当前最省心的部署方案。

---

## ADR-006 · 模型迭代策略：v5→v6→v7

### 背景

3 个模型版本并存：v5（rank=8，12656 样本）、v6（rank=16，5238 样本）、v7（v5 续训，8000 样本）。

### 版本对比

| 版本 | LoRA r | 训练数据 | eval_loss | 部署 | 备注 |
|---|---|---|---:|---|---|
| v4 fast | 8 | 4733 | 0.2300 | ✅ | 基础参考 |
| **v5** | **8** | **12656** | **0.1958** | ✅ | 主力版本 |
| v6 | 16 | 5238 | 0.2883 | ✅ | rank 升级实验 |
| **v7** | **8** | **8000（v5+v6）** | **0.1914** | ✅ | **当前最优** |

### 决策

1. **v5 保留**：作为稳定基线（r=8，低显存，12656 样本最丰富）
2. **v6 保留**：作为高 rank 对比实验（eval_loss 0.2883 不理想）
3. **v7 启用**：v5 LoRA + v5+v6 合并数据续训 → eval_loss 最低
4. **v8 fusion 暂缓**：rank 不兼容（r=8 vs r=16），需 model souping 或重训

### 权衡

- ✅ 3 版本对比可观察迭代效果
- ✅ v7 = v5 + 新数据 最佳性价比
- ❌ 多版本占空间（3×29GB = 87GB）
- ❌ v8 model souping 风险高（70GB 内存需求）

### 结论

当前主用 **v7**（eval_loss 0.1914），v5 作 fallback，v6 作实验参照。

---

## ADR-007 · 量化方案 D 选型：LightGBM

### 背景

方案 D 需要识别"翻倍前 5 日"的信号特征，要求高精度 + 可解释性。

### 备选方案

| 方案 | 精度 | 训练速度 | 可解释性 | 评估 |
|---|---|---|---|---|
| **LightGBM**（选）| 高 | 快 | ✅ feature_importance | ⭐⭐⭐⭐⭐ |
| XGBoost | 高 | 中 | ✅ | ⭐⭐⭐⭐ |
| CatBoost | 高 | 中 | ✅ | ⭐⭐⭐⭐ |
| Random Forest | 中 | 快 | ✅ | ⭐⭐⭐ |
| Neural Net (MLP) | 中 | 慢 | ❌ | ⭐⭐ |
| Transformer | 高 | 极慢 | ❌ | ⭐⭐ |

### 决策

- **模型**：`LightGBM`
- **超参**：`n_estimators=500, max_depth=6, learning_rate=0.05`
- **特征**：18 维（详见 README 4.5）
- **样本**：13023（正 6414 / 负 6609，平衡）
- **结果**：Test AUC = **0.8144**，阈值 0.7 精度 83.15%

### 权衡

- ✅ AUC 0.81 + 特征重要性可解释（prev_close > avg_amount_5d > close_pos_5d）
- ✅ 训练 30s，推理 7620 只 < 1 分钟
- ✅ 平衡数据避免过拟合
- ❌ 无法直接给出非线性交互解释
- ⚠ 数据分布变化（监管 / 市场风格）需重训

### 结论

LightGBM 在精度 + 速度 + 可解释性上达到最佳平衡。

---

## ADR-008 · 方案 E 策略：v1→v2 升级

### 背景

方案 E（翻倍当日跟随）v1 胜率仅 36.7%，平均收益 3.52%，需优化信号过滤。

### v1 vs v2 对比

| 指标 | v1 旧规则 | v2 增强版 | 提升 |
|---|---:|---:|---:|
| 样本数 | 97 | 27 | -70 |
| 胜率 | 46.4% | **55.6%** | **+9.2pp** |
| 平均收益 | 7.41% | **12.22%** | **+4.81pp** |
| 盈亏比 | - | **3.00** | - |

### 决策

- **触发条件升级**：仅翻倍 → 翻倍 + T+1 ≥ 9.5% + 量比 ≥ 1.5
- **入场时机**：T 日（v1）→ T+2 开盘（v2，避开 T+1 涨停）
- **出场规则**：保持 TP 30% / SL -10% / TIME 10 日

### 权衡

- ✅ 胜率 +9.2pp，平均收益 +4.81pp
- ✅ 盈亏比 3.00（健康）
- ❌ 信号命中率 1.4%（1942 → 27），机会少
- ❌ 实盘样本不足（27 笔），需 1 个月跟踪验证

### 结论

v2 信号更紧，单笔质量更高；适合**小资金高频次**而非大资金。

---

## ADR-009 · 编排调度：systemd user timer

### 背景

需要定期跑 5 个任务（sync / predict / journal / weekly / canary），需选择编排工具。

### 备选方案

| 方案 | 易用性 | 监控 | 依赖 | 评估 |
|---|---|---|---|---|
| **systemd user timer**（选）| ✅ | journalctl | systemd | ⭐⭐⭐⭐⭐ |
| cron | ✅ | syslog | cron daemon | ⭐⭐⭐⭐ |
| airflow | ⚠ 重型 | web UI | 复杂 | ⭐⭐ |
| prefect / dagster | ⚠ | web UI | 复杂 | ⭐⭐ |
| CodeBuddy automation | ✅ | IDE | workbuddy | ⭐⭐⭐⭐ |

### 决策

- **方案**：systemd user timer（`~/.config/systemd/user/`）
- **任务数**：5 个 timer + 5 个 service
- **日志**：`StandardOutput=append:logs/*.log`

### 5 个 Timer 配置

| Timer | 频率 | 任务 |
|---|---|---|
| canary-health.timer | *:0/5（每 5min）| 健康检查 |
| tdxfeed-sync.timer | Mon..Fri 18:00 | 拉行情 |
| predict-daily.timer | Mon..Fri 18:05 | 方案 D 预测 |
| plan-e-journal.timer | Mon..Fri 18:10 | 方案 E 信号 |
| plan-e-weekly.timer | Fri 20:00 | 方案 E 周报 |

### 权衡

- ✅ systemd 原生支持 Persistent（错过后补跑）
- ✅ 用户级无需 root 权限
- ✅ journalctl 内置日志查询
- ✅ 可通过 `systemctl --user` 启停
- ❌ Mac/Windows 不支持（仅 Linux）
- ❌ 配置文件需复制到 `~/.config/systemd/user/`

### 结论

systemd user timer 是 Linux 服务器上的最佳实践。

---

## ADR-010 · 监控告警：自研 canary

### 背景

需要监控系统 7×24 状态：ollama / 数据 / GPU / timer / 磁盘。

### 备选方案

| 方案 | 易用性 | 定制性 | 依赖 | 评估 |
|---|---|---|---|---|
| **自研 canary**（选）| ✅ | ✅ | 仅 Python | ⭐⭐⭐⭐⭐ |
| Prometheus + Grafana | ⚠ 重型 | ✅ | 复杂 | ⭐⭐⭐ |
| Datadog | ✅ | ✅ | 付费 | ⭐⭐⭐⭐ |
| New Relic | ✅ | ✅ | 付费 | ⭐⭐⭐ |

### 决策

- **监控工具**：自研 `scripts/canary_health_check.py`
- **6 项检查**：ollama / tdxfeed_sync / data_files / disk / timers / gpu
- **触发**：systemd timer 每 5 分钟
- **输出**：`logs/canary.log` + `logs/canary_history.jsonl`

### 6 项检查

| Check | 阈值 | 异常动作 |
|---|---|---|
| ollama | `/api/tags` 200 + 有 tdx 模型 | 重启 ollama |
| tdxfeed_sync | Result=success 且 ≤ 24h | 查看 logs/sync.err |
| data_files | parquet ≥ 5000 + adj ≥ 5000 | 重跑同步 |
| disk | 剩余 ≥ 20GB | 清理 GGUF |
| timers | 3 个关键 timer 都 active | `systemctl start` |
| gpu | 利用率 ≤ 95% | 重启进程 |

### 权衡

- ✅ 自研可精确控制 6 项检查
- ✅ systemd timer 自动调度
- ✅ 异常时 exit 1 → systemd 记录
- ❌ 无图形化 dashboard（需另搭）
- ❌ 无自动修复（仅检测）

### 结论

自研 canary 满足当前需求，未来如需 dashboard 可接 Prometheus。

---

## 系统分层设计

### 9 层架构

```
┌─────────────────────────────────────────────────────┐
│ L9 监控层 · canary-health (5min)                     │
├─────────────────────────────────────────────────────┤
│ L8 自动化层 · systemd user timer (5 个)              │
├─────────────────────────────────────────────────────┤
│ L7 应用层 · predict.py / plan_e_journal.py          │
├─────────────────────────────────────────────────────┤
│ L6 推理层 · Ollama HTTP API (4 个 tdx 模型)         │
├─────────────────────────────────────────────────────┤
│ L5 部署层 · Modelfile + GGUF + ollama create        │
├─────────────────────────────────────────────────────┤
│ L4 训练层 · LLaMA-Factory + LoRA (v5/v6/v7)         │
├─────────────────────────────────────────────────────┤
│ L3 量化分析层 · find_*.py + plan_*.py + LightGBM    │
├─────────────────────────────────────────────────────┤
│ L2 数据存储层 · parquet + JSON (8035 + 5565)         │
├─────────────────────────────────────────────────────┤
│ L1 数据采集层 · tdxfeed/ + scripts/download_*.py    │
└─────────────────────────────────────────────────────┘
```

### 各层职责

| 层 | 模块 | 输入 | 输出 |
|---|---|---|---|
| L1 采集 | tdxfeed / download_* | API (baostock/akshare) | parquet/daily, adj_factor |
| L2 存储 | parquet 落盘 | 原始数据 | 文件系统 |
| L3 分析 | find_double_up / find_d_signal / plan_e | parquet | CSV + LightGBM 模型 |
| L4 训练 | train/* + LLaMA-Factory | jsonl | LoRA adapter |
| L5 部署 | deploy_*.sh | LoRA + base | GGUF + Ollama 模型 |
| L6 推理 | Ollama | prompt | text response |
| L7 应用 | predict / journal | parquet / API | CSV + Markdown |
| L8 自动化 | systemd timer | 时间 | 触发 L7 |
| L9 监控 | canary | 系统状态 | log + stderr |

---

## 模块职责矩阵

### 数据采集（L1）

| 模块 | 职责 | 调用方 |
|---|---|---|
| tdxfeed.cli sync | 拉取单股日 K（sina/akshare/tencent 三源兜底）| systemd timer + 手动 |
| download_adj_factor_b.py | 批量下载沪深复权因子（baostock）| 手动 |
| download_adj_factor_bj.py | 下载北交所复权因子（akshare 新浪）| 手动 |
| download_bj_daily.py | 下载北交所日 K | 手动 |
| download_universe.py | 全 A 股代码清单 | 首次部署 |
| download_adj_factor_watch.py | watchdog 自动重启挂死进程 | 手动启动 |

### 量化分析（L3）

| 模块 | 职责 | 输出 |
|---|---|---|
| find_double_up_c.py | 识别翻倍样本（不复权）| 22972 样本 |
| find_double_up_c_v2.py | 前复权价对比 + 偏差分析 | v2 csv + 对比报告 |
| find_d_signal_5d.py | LightGBM 训练（AUC 0.8144）| d_signal_5d_model.txt |
| top10_deep_dive.py | Top 10 妖股复盘 | 22KB MD |
| plan_e_backtest.py | 方案 E 回测（v1/v2 对比）| backtest csv + md |
| gen_dashboard.py | HTML 综合仪表盘 | training_dashboard.html |

### 模型训练（L4）

| 模块 | 职责 | 输出 |
|---|---|---|
| /tmp/v5_train_single.py | v5 训练（r=8，12656 样本）| qwen3-14b-tdx-v5-simple/ |
| /tmp/v6_train_single.py | v6 训练（r=16，5238 样本）| qwen3-14b-tdx-v6-gpu1/ |
| /tmp/v7_train_v3.py | v7 续训（v5 + 新数据）| qwen3-14b-tdx-v7/ |
| merge_v5_v6_lora.py | v5 + v6 LoRA fusion（未完成）| - |

### 模型部署（L5）

| 模块 | 职责 |
|---|---|
| v5/scripts/deploy_v5.sh | 4 步部署：merge → GGUF → Modelfile → ollama |
| v5/scripts/deploy_v6.sh | 同上 |
| /home/jiuben/llama.cpp/convert_hf_to_gguf.py | HF → GGUF 转换器 |

### 应用（L7）

| 模块 | 模式 | 职责 |
|---|---|---|
| predict.py | CLI | 全市场 7620 只预测 |
| plan_e_journal.py | daily/weekly/status/buy/sell/simulate | 实盘跟踪 6 模式 |
| canary_health_check.py | -/quiet | 6 项健康检查 |

---

## 数据流图

### 训练数据流

```
baostock/akshare
       ↓
download_adj_factor_b.py
       ↓
data/adj_factor/{code}.parquet (5565)
       ↓
tdxfeed.cli sync
       ↓
data/parquet/daily/{code}.parquet (8035)
       ↓
find_double_up_c.py
       ↓
double_up_2020_2026_detail.csv (22972)
       ↓
build_distill_skeleton.py + batch_distill.py
       ↓
train/data/tdx_v5_train.jsonl (12656)
       ↓
/tmp/v5_train_single.py + LLaMA-Factory
       ↓
train/output/qwen3-14b-tdx-v5-simple/checkpoint-1188/
       ↓
v5/scripts/deploy_v5.sh
       ↓
1. PeftModel.merge_and_unload → /home/jiuben/models/Qwen3-14B-tdx-v5-merged/ (28GB)
2. convert_hf_to_gguf.py → Qwen3-14B-tdx-v5-merged-f16.gguf (29.5GB)
3. Modelfile-v5
4. ollama create qwen3-14b-tdx-v5
       ↓
http://localhost:11434/api/generate
```

### 实盘数据流

```
每日 18:00 (systemd) → tdxfeed-sync → 更新 parquet/daily
每日 18:05 (systemd) → predict-daily → LightGBM 推理 → predict_*.csv/md
每日 18:10 (systemd) → plan-e-journal → plan_e_backtest --today → signals_*.csv
                          ↓
                      用户决策（09:30 开盘）
                          ↓
                     buy/sell CLI → journal.csv + positions.json
                          ↓
每周五 20:00 (systemd) → plan-e-weekly → 周报 week_*.md

每 5 分钟 (systemd) → canary-health → canary.log + stderr 异常告警
```

---

## 依赖关系图

### 外部依赖

```
tdx-data-feed
├── 数据源
│   ├── baostock (免费无限制)
│   ├── akshare (akshare库)
│   └── 通联数据 (download_universe.py)
├── 训练
│   ├── Qwen3-14B (HuggingFace)
│   ├── LLaMA-Factory 0.9.5
│   ├── transformers 4.57.6
│   ├── peft 0.20.0
│   └── gguf ≥ 0.19
├── 部署
│   ├── Ollama (latest)
│   └── llama.cpp (master)
├── Python
│   ├── pandas 3.0.5
│   ├── numpy 2.5.3
│   ├── pyarrow 25.0.1
│   └── lightgbm (latest)
└── 系统
    ├── systemd (Linux)
    ├── nvidia-smi (GPU)
    └── Python 3.14
```

### 内部依赖

```
L1 采集 ──→ L2 存储 ──→ L3 分析 ──→ L4 训练 ──→ L5 部署 ──→ L6 推理 ──→ L7 应用
                                                                      ↓
                                                              L8 自动化 (systemd)
                                                                      ↓
                                                              L9 监控 (canary)
```

### 脚本依赖矩阵

| 脚本 | 依赖 |
|---|---|
| predict.py | data/parquet/daily, data/adj_factor, d_signal_5d_model.txt, lightgbm |
| plan_e_backtest.py | data/double_up/double_up_2020_2026_detail.csv, data/parquet/daily |
| plan_e_journal.py | plan_e_backtest.py (subprocess), parquet/daily |
| find_double_up_c.py | data/parquet/daily |
| find_d_signal_5d.py | double_up csv, parquet, lightgbm |
| deploy_v5.sh | train/output/v5/, llama.cpp, ollama |
| canary_health_check.py | ollama API, systemd CLI, nvidia-smi |

---

## 总结

| 维度 | 决策 |
|---|---|
| LLM | Qwen3-14B（中文 + 显存友好）|
| 数据源 | baostock（沪深）+ akshare（北交所）|
| 存储 | Parquet（数据）+ JSON（元信息）+ CSV（特征）|
| 训练 | LLaMA-Factory + LoRA r=8/16 |
| 部署 | Ollama + GGUF f16 |
| 量化模型 | LightGBM（AUC 0.81）|
| 策略 | 方案 E v2（胜率 55.6%，盈亏比 3.00）|
| 调度 | systemd user timer（5 个）|
| 监控 | 自研 canary（6 项检查）|

> 本 ADR 体系持续更新。任何重大架构变更需新增 ADR-XXX 记录。

---

## 许可

私有项目 · 内部使用 · 未经授权不得传播

> 最后更新：2026-09-27 · 维护者：jiuben
