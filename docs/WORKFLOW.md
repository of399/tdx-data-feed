# tdx-data-feed 工作流全景文档

> 生成：2026-09-27 · 范围：tdx-data-feed 完整系统（含数据采集 → LoRA 微调 → 妖股识别 → 模型评估 → 报告产出全链路）
>
> 阅读路径：第 1 章建立心智模型 → 第 2-3 章按数据流向读 → 第 4-6 章按组件维度查 → 第 7 章是速查表

---

## 0. 系统目标（一句话）

把 A 股全市场 5565 只股票的历史 K 线 + 复权因子 + 财务数据喂给 **Qwen3-14B 量化分析师**（经 LoRA 微调），输出两类产品：
- **结构化样本**（翻倍样本/预警信号，CSV+MD）
- **可对话模型**（Ollama 部署的 qwen3-14b-tdx-v5/v6/v7 三选一）

---

## 1. 软件程序角色矩阵

### 1.1 数据采集层（6 个）

| 脚本 | 角色 | 输入 | 输出 | 触发方式 |
|---|---|---|---|---|
| `scripts/download_adj_factor_b.py` | A 股全市场复权因子（沪深）| baostock API | `data/adj_factor/{code}.parquet` × 5221 | 手动 / watchdog |
| `scripts/download_adj_factor_bj.py` | 北交所 920XXX 复权因子 | akshare 新浪源 | 同上 × 344 | 手动 |
| `scripts/download_bj_daily.py` | 北交所 920XXX 日 K 线 | akshare | `data/parquet/daily/bj{code}.parquet` × 344 | 手动 |
| `scripts/download_adj_factor_watch.py` | 上面两个的 watchdog | - | 自动重启挂死的子进程 | 手动启动 |
| `scripts/download_universe.py` | A 股全市场代码清单 | 通联数据 | `data/all_a_stocks.json` | systemd timer |
| `scripts/analyze_one.py` | 单只股多因子分析（技术面）| parquet/daily + adj_factor | dict（落库） | 单股触发 |

### 1.2 训练层（3 个 wrapper + 1 个 yaml）

| 脚本 | 角色 | 基础 | 数据 | 输出 |
|---|---|---|---|---|
| `/tmp/v5_train_single.py` | v5 训练 | Qwen3-14B（无 LoRA）| `tdx_v5_train.jsonl` 12656 条 | `train/output/qwen3-14b-tdx-v5-simple/` |
| `/tmp/v6_train_single.py` | v6 训练 | Qwen3-14B（无 LoRA）| `tdx_v6_train.jsonl` 5238 条 | `train/output/qwen3-14b-tdx-v6-gpu1/` |
| `/tmp/v7_train_v3.py` | v7 训练（v5 续训）| v5 LoRA ckpt-1188 | `tdx_v7_train.jsonl` 8000 条 | `train/output/qwen3-14b-tdx-v7/` |
| `training/config/tdx_v5_lora_simple.yaml` | 训练超参配置 | - | - | - |

### 1.3 部署层（2 个 shell + 1 个 helper）

| 脚本 | 角色 | 步骤 |
|---|---|---|
| `v5/scripts/deploy_v5.sh` | v5 全流程部署 | ①merge LoRA→28GB · ②GGUF f16 转换 · ③写 Modelfile · ④ollama create |
| `v5/scripts/deploy_v6.sh` | v6 全流程部署（结构与 v5 一致）| 同上 |
| `scripts/merge_v5_v6_lora.py` | LoRA 权重融合（**v5=rank8 vs v6=rank16，shape 不兼容，未完成**）| v5+v6 safetensors → v8 |

### 1.4 分析层（4 个核心脚本）

| 脚本 | 角色 | 输入 | 输出 |
|---|---|---|---|
| `scripts/find_double_up_c.py` | 方案 C v1：**20 日内翻倍**筛选（不复权）| parquet/daily 8035 | `data/double_up/double_up_2020_2026_detail.csv` (22972 样本) + `double_up_2020_2026_by_code.csv` |
| `scripts/find_double_up_c_v2.py` | 方案 C v2/v3：前复权价翻倍 + v1 对比 | parquet/daily + adj_factor | v3 csv + 假阳性/漏判分析 + report MD |
| `scripts/top10_deep_dive.py` | Top 10 妖股深度复盘 | v1 by_code + akshare stock_zyjs_ths | 22KB MD（涨停节奏/连板/成交量/行业）|
| `scripts/find_d_signal_5d.py` | 方案 D：翻倍前 5 日信号识别（LightGBM）| v1 detail + parquet | AUC 0.8144 模型 + 特征重要性报告 |

### 1.5 评估层（2 个）

| 脚本 | 角色 | 输入 | 输出 |
|---|---|---|---|
| `v5/scripts/v6_eval.py` | v6 vs v4 fast（30 只核心股）| ollama /api/generate | 一致率 + 平均相似度报告 |
| `v5/scripts/v5_v6_compare.py` | v5 vs v6（同一 prompt diff）| 同上 | 字段完整度 + 风格差异报告 |

### 1.6 监控与编排（5 个）

| 脚本 | 角色 | 触发 |
|---|---|---|
| `scripts/train_health.sh` | 训练进程健康检查（ckpt 记录、孤儿跳过）| systemd timer 每 5min |
| `scripts/plot_loss.py` | loss 曲线可视化 | 手动 `--all` 自动扫所有 v* 目录 |
| `scripts/gen_dashboard.py` | 综合 HTML 仪表盘 | 手动（包含所有数据快照）|
| `systemd/tdxfeed-sync.timer` | 行情同步（每晚 18:00）| systemd timer |
| `.codebuddy/automations/v5-daemon/` | v5 训练自动续训（CodeBuddy 自动化）| CodeBuddy 调度 |

### 1.7 数据转换 / 辅助（5+ 个）

- `scripts/distill_outputs.py`、`scripts/batch_distill.py`、`scripts/build_distill_skeleton.py`、`scripts/convert_distill.py` — 蒸馏数据预处理
- `scripts/finalize_eval.py`、`scripts/redo_finalize.py` — 评估结果定稿
- `scripts/test_bjs_akshare.py` — 北交所 akshare 接口冒烟测试
- `scripts/audit_log_rotate.py`、`scripts/canary_*` — 日志轮转 / 金丝雀

---

## 2. Agent 协作模型

### 2.1 主 Agent（当前会话）

| 属性 | 值 |
|---|---|
| 模型 | MiniMax-M3（会话级）|
| 工具集 | execute_command、read_file、write_to_file、replace_in_file、search_*.、lsp、todo_write、use_skill、task、read_lints 等 |
| 工作目录 | `/home/jiuben/tdx-data-feed/` |
| 核心职责 | ①理解需求 ②拆任务 ③调度子 Agent / 调脚本 / 调 Skill ④验收输出 ⑤写入 DELIVERY.md |

### 2.2 子 Agent（code-explorer）

| 属性 | 值 |
|---|---|
| 调用方式 | `task` 工具 → `subagent_name=code-explorer` |
| 能力 | LSP（documentSymbol / workspaceSymbol / goToDefinition / findReferences / incomingCalls / outgoingCalls / hover / locate / readSymbol）+ search_content + read_file + list_dir |
| 触发场景 | 跨目录/多文件搜索、代码导航、"X 在哪定义"、"X 被谁调用" |
| 数据流向 | 主 Agent 发任务 → code-explorer 返回摘要 → 主 Agent 据此决策 |

### 2.3 Team Mode（异步协作）

| 属性 | 值 |
|---|---|
| 调用方式 | `task` 工具 + `name=team_name=X` 参数 |
| 成员 | `frontend-dev` / `backend-dev` / `reviewer`（按需命名）|
| 通信 | `send_message`（type=message / broadcast / shutdown_request / shutdown_response / plan_approval_response）|
| 持久化 | `{workspace}/.codebuddy/teams/{team_name}/` |
| 使用场景 | 跨多模块并行开发（本次 tdx-data-feed 任务**未触发**，因为单人单 GPU 工作流不需要异步并行）|

### 2.4 自动化任务（automations）

- `v5-daemon`：定义在 `.codebuddy/automations/v5-daemon/automation.toml`（内容未读到，推测是 v5 训练自动续训）
- 触发机制：CodeBuddy 调度器（不是 systemd）
- 与 systemd timer 区别：systemd 管数据同步，automations 管模型训练

---

## 3. Skill 调用矩阵

| Skill | 类别 | 调用场景 | 本项目使用 |
|---|---|---|---|
| **skill-creator** | manager | 创建新 skill | 否（未触发）|
| **lsp-code-analysis** | manager | 代码语义导航（找定义/引用）| 是（code-explorer 子 Agent 内置）|
| **多模态内容生成** | manager | 文生视频/图/3D | 否 |
| **pptx** | plugin | 任何 .pptx 操作 | 否 |
| **pdf** | plugin | 任何 .pdf 操作 | 否 |
| **docx** | plugin | 任何 .docx 操作 | 否（DELIVERY.md 是 MD 不是 docx）|
| **xlsx** | plugin | 任何 .xlsx 操作 | 否 |
| **agent-browser** | plugin | 浏览器自动化（macOS/Linux/Win）| 否（方案 D 用 akshare 不是浏览器）|
| **playwright-cli** | plugin | 浏览器自动化测试 | 否 |
| **find-skills** | plugin | 发现社区 skill | 否 |
| **MCP开发指南** | user | 构建 MCP 服务器 | 否 |
| **MCP管理器** | user | mcporter CLI 管理 MCP | 否 |
| **github** | user | gh CLI 操作 GitHub | 隐式（通过 mcp__github__*）|
| **tdd** | user | TDD 红绿重构 | 否 |
| **全栈开发** | user | 前后端集成 | 否 |
| **去AI味** | user | 去 AI 生成痕迹 | 否 |
| **反蒸馏** | user | 反蒸馏防御 | 否 |
| **女娲** | user | 输入主题生成人物 skill | 否 |
| **book2skill** | user | 书→skill | 否 |
| **darwin-skill** | user | skill 自动优化 | 否 |
| **OpenClaw记忆迁移** | user | OpenClaw → WorkBuddy 迁移 | 否 |
| **WorkBuddy资产迁移** | user | 国内/海外版切换 | 否 |
| **skill-vetter** | user | skill 安全审查 | 否 |

> **本项目实际触发 Skill 极少**（0 次直接触发）。Agent 主要靠 execute_command / file ops / Task(code-explorer)。

---

## 4. MCP 组件关系

### 4.1 已配置的 MCP 服务（来自 `~/.codebuddy/mcp.json`）

| Server | 命令 | 工具集 | 用途 |
|---|---|---|---|
| **github** | `docker run ... ghcr.io/github/github-mcp-server` | repos, issues, pull_requests, actions, code_search | PR/Issue/搜索代码 |

### 4.2 通过 `invoke_integration` 接入的集成

| ID | 类型 | 工具 |
|---|---|---|
| `tcb` | CloudBase (deploy, database) | - |
| `eop` | EdgeOne Makers (deploy) | - |
| `supabase` | database | - |
| `cloudStudio` | deploy | cloud_studio_deploy_sandbox / list_deployments / delete_deployment |
| `lighthouse` | Tencent Lighthouse (deploy) | - |

### 4.3 通用工具

| 工具 | 角色 |
|---|---|
| `web_search` | 实时搜索（被反 AI 训练 cutoff 限制时）|
| `web_fetch` | URL → markdown（用于读 llama.cpp / PyPI 等文档）|
| `RAG_search` | 知识库检索 |
| `preview_url` | 本地起服务后预览 web 页面（用于 dashboard.html）|
| `open_result_view` | 展示最终结果文件 |
| `connect_cloud_service` | 拿 IDE 端凭据 |

### 4.4 数据流转路径

```
用户意图
  ↓
主 Agent (MiniMax-M3)
  ├── execute_command ──→ bash / python scripts ──→ 文件系统 / ollama / GPU
  ├── read_file / write_to_file / replace_in_file ──→ 本地仓库
  ├── search_content / search_file ──→ ripgrep / glob
  ├── lsp ──→ LSP server (pyright 等)
  ├── task (code-explorer) ──→ 子 Agent 返回摘要
  ├── mcp__github__* ──→ Docker → github-mcp-server → github.com
  ├── invoke_integration + call_integration ──→ cloudStudio / tcb / supabase
  ├── web_fetch ──→ HTTP → markdown
  ├── use_skill ──→ plugin/manager/user skill scripts
  └── open_result_view / preview_url ──→ 把结果展示给用户
```

---

## 5. 端到端数据流（从输入到输出）

### 5.1 数据流全景图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  输入层                                      │
│                                                                             │
│  baostock API ──────┐                                                        │
│  akshare API  ──────┼─→ download_adj_factor_b.py / _bj.py / download_bj_daily│
│  Eastmoney ─────────┘                  ↓                                    │
│                                       data/adj_factor/ + data/parquet/daily/│
│                                       (5565 + 8035 文件)                     │
│                                             ↓                               │
│  通联数据 / 东方财富 ──→ download_universe.py → data/all_a_stocks.json     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  分析层                                      │
│                                                                             │
│  find_double_up_c.py (v1 不复权) ──┐                                        │
│                                       ├──→ data/double_up/double_up_2020    │
│  find_double_up_c_v2.py (v3 前复权) ┤   _2026_detail.csv (22972 样本)        │
│                                       ├──→ double_up_v2_compare.csv        │
│                                       └──→ double_up_only_v1_false_pos.csv  │
│                                                                             │
│  find_d_signal_5d.py (LightGBM) ──────→ d_signal_5d_model.txt               │
│                                       → d_signal_5d_features.csv            │
│                                       → d_signal_5d_report_*.md            │
│                                                                             │
│  top10_deep_dive.py ──→ top10_deep_dive_*.md (22KB)                        │
│                  ↑                                                          │
│                  └── akshare.stock_zyjs_ths (主营业务)                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  训练层                                      │
│                                                                             │
│  Qwen3-14B (base) ──→ v5_train_single.py (LoRA r=8)  ─→ v5 ckpt-1188       │
│  Qwen3-14B (base) ──→ v6_train_single.py (LoRA r=16) ─→ v6 ckpt-984        │
│  v5 ckpt-1188 ───────→ v7_train_v3.py (8000 samples) ──→ v7 ckpt-500        │
│  tdx_v7_train.jsonl = tdx_v5_train ∪ tdx_v6_train 去重合并 (17840 条)       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  部署层                                      │
│                                                                             │
│  deploy_v5.sh: LoRA merge → save_pretrained (28GB) → GGUF f16 (29.5GB)    │
│              → Modelfile-v5 → ollama create qwen3-14b-tdx-v5               │
│                                                                             │
│  deploy_v6.sh: 同结构 → qwen3-14b-tdx-v6                                   │
│  v7: v5 ckpt-1188 + tdx_v7_train → v7 adapter（**未部署到 Ollama**）        │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  评估层                                      │
│                                                                             │
│  v6_eval.py        ─→ v6 vs v4 fast 一致率 21/30 (70%)                      │
│  v5_v6_compare.py  ─→ v5 vs v6 相似度 12% / 字段 5/5 vs 4/5                │
│                                                                             │
│  gen_dashboard.py  ─→ training_dashboard.html (174KB · 内嵌 loss 图)        │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  输出层                                      │
│                                                                             │
│  - data/double_up/*.csv / *.md × 9                                          │
│  - data/double_up/training_dashboard.html                                  │
│  - DELIVERY.md (§12 全章节 = 587 行)                                       │
│  - Ollama: qwen3-14b-tdx-v5 / v6 / fast (3 个推理端点)                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 关键数据格式与位置

```
tdx-data-feed/
├── data/
│   ├── all_a_stocks.json              # 5565 只 A 股清单
│   ├── adj_factor/                    # 5565 .parquet (date, adj_factor, qfq_close, raw_close, code)
│   ├── parquet/
│   │   ├── daily/                     # 8035 .parquet (sh/sz/bj 前缀)
│   │   ├── finance.parquet            # 1,667,841 行 财务
│   │   └── xdxr.parquet               # 299 行 股本变动
│   ├── v4/                            # 4733 条 v4 fast 训练数据
│   ├── v5_train/, v6_train/           # 原始 v5/v6 数据副本
│   ├── double_up/                     # 9 个报告 + 5 个 csv
│   ├── distill/                       # 蒸馏数据 (空)
│   └── v4/                            # 见上
├── train/
│   ├── data/
│   │   ├── tdx_v5_train.jsonl         # 12656 条
│   │   ├── tdx_v6_train.jsonl         # 5238 条
│   │   ├── tdx_v7_train.jsonl         # 17840 条 (v5+v6 去重合并)
│   │   └── dataset_info.json
│   └── output/
│       ├── qwen3-14b-tdx-v5-simple/checkpoint-1188/
│       ├── qwen3-14b-tdx-v6-gpu1/checkpoint-984/
│       └── qwen3-14b-tdx-v7/{checkpoint-200, 400, 500}/
├── training/config/tdx_v5_lora_simple.yaml
├── scripts/                           # 13 个分析/采集脚本
├── v5/scripts/                        # 15+ 部署/评估脚本
├── systemd/tdxfeed-sync.timer         # 数据同步
├── docs/                              # 2 个治理文档
├── DELIVERY.md                        # §12 完整章节
└── .codebuddy/automations/v5-daemon/  # 自动续训
```

---

## 6. 依赖关系与触发逻辑

### 6.1 任务依赖图（DAG）

```
                   [baostock/akshare 网络]
                            │
                            ▼
            ┌─────── download_adj_factor_*.py ──────┐
            │                                         │
            ▼                                         ▼
   data/adj_factor/ (5565)              data/parquet/daily/ (8035)
            │                                         │
            └─────────────┬───────────────────────────┘
                          ▼
                  find_double_up_c.py (v1)
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
   find_double_up_c_v2.py          top10_deep_dive.py
   (v3 + 偏差分析)                    │
            │                          │
            │                          └─→ akshare stock_zyjs_ths
            ▼                                   │
   data/double_up/v3 csv                         ▼
            │                          top10_deep_dive_*.md
            ▼
   find_d_signal_5d.py (LightGBM)
            │
            ▼
   d_signal_5d_model.txt + report

   [并行链：模型训练]
   Qwen3-14B ─→ v5_train_single.py ─→ v5 ckpt-1188
                              │
                              ▼
   tdx_v7_train ──→ v7_train_v3.py ─→ v7 ckpt-500
                              │
                              ▼
                  deploy_v5.sh / deploy_v6.sh
                              │
                              ▼
                  ollama: v5/v6 models
                              │
                              ▼
                  v6_eval.py + v5_v6_compare.py
                              │
                              ▼
                  gen_dashboard.py → training_dashboard.html

   [汇聚输出]
   所有 CSV/MD ──→ DELIVERY.md §12 (人工 + Agent 协作写入)
```

### 6.2 触发方式对照表

| 任务 | 自动触发 | 手动触发 | 触发器 |
|---|---|---|---|
| 行情同步 | ✅ | - | systemd timer `tdxfeed-sync.timer` (18:00 daily) |
| v5 训练续训 | ✅ | - | `.codebuddy/automations/v5-daemon/` |
| 训练健康检查 | ✅ | - | systemd timer `trainsh-health.timer` (5min) |
| 方案 B 下载 | - | ✅ | 用户指令（一次性大规模爬取）|
| v6/v7 训练 | - | ✅ | 用户指令 |
| deploy | - | ✅ | 用户指令（修 GGUF 工具链后才成功）|
| 翻倍筛选 v1/v3 | - | ✅ | 用户指令 |
| 方案 D LightGBM | - | ✅ | 用户指令 |
| v6_eval / v5_v6_compare | - | ✅ | 用户指令（启动后跑 ~30min）|
| Dashboard 重新生成 | - | ✅ | 用户指令（读取最新 csv）|

### 6.3 错误恢复机制

| 失败 | 修复 |
|---|---|
| `convert_hf_to_gguf.py` 404 (14 字节) | 从 GitHub raw 单文件下载替换 + 升级 venv gguf 0.19.0 → master |
| `llama.cpp` 多文件 broken | 从 GitHub zip 解压 → 覆盖 `conversion/` 目录 |
| baostock hang 北交所 920XXX | skip 920XXX（baostock 无数据）|
| v5/v6 deploy 写 28GB OOM | `device_map="cpu", low_cpu_mem_usage=True` |
| Modelfile chat-style 生成 1 token | 改 fast 风格 `TEMPLATE """{{ .Prompt }}"""` + stop tokens |
| ollama HTTP 500 | v6_eval 加 3 次 retry + sleep 5/10/15s |
| v7 训练 OOM (v6_eval 占 GPU) | curl ollama `keep_alive:0` 卸载 → 重启 v7 |
| v8 LoRA fusion rank 不匹配 (v5=8, v6=16) | **未解决**（需升 v5 rank=16 重训，或做 merged-model souping 70GB 内存）|
| v7 wrapper 用错 `resume_from_checkpoint` | 改用 `adapter_name_or_path` (避免 skip epoch) |

---

## 7. 速查表

### 7.1 模型对比

| 模型 | rank | alpha | 数据 | eval_loss | 部署 |
|---|---|---|---|---|---|
| qwen3-14b-tdx-fast (v4) | 8 | 16 | 4733 | 0.23 | ✅ Ollama |
| qwen3-14b-tdx-v5 | 8 | 16 | 12656 | **0.1958** | ✅ Ollama |
| qwen3-14b-tdx-v6 | 16 | 32 | 5238 | 0.2883 | ✅ Ollama |
| qwen3-14b-tdx-v7 | 8 | 16 | 8000(v5续训) | **0.1914** | ❌ 仅 LoRA adapter |
| qwen3-14b-tdx-v8 (fusion) | - | - | - | - | ❌ 未生成 |

### 7.2 关键文件路径

```
数据：
  /home/jiuben/tdx-data-feed/data/all_a_stocks.json     # 5565 只清单
  /home/jiuben/tdx-data-feed/data/parquet/daily/*.parquet  # 8035 日 K
  /home/jiuben/tdx-data-feed/data/adj_factor/*.parquet    # 5565 复权因子

模型：
  /home/jiuben/models/Qwen3-14B/                          # base
  /home/jiuben/models/Qwen3-14B-tdx-v5-merged/            # v5 合并
  /home/jiuben/models/Qwen3-14B-tdx-v6-merged/            # v6 合并
  /home/jiuben/models/Qwen3-14B-tdx-v5-merged-f16.gguf    # v5 GGUF 29.5G
  /home/jiuben/models/Qwen3-14B-tdx-v6-merged-f16.gguf    # v6 GGUF 29.5G
  /home/jiuben/models/Modelfile-v5 / Modelfile-v6         # Ollama 配置
  /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v7/  # v7 LoRA

报告：
  /home/jiuben/tdx-data-feed/data/double_up/*.md  × 9
  /home/jiuben/tdx-data-feed/data/double_up/training_dashboard.html  # 174KB
  /home/jiuben/tdx-data-feed/DELIVERY.md         # 587 行含 §12

脚本：
  /home/jiuben/tdx-data-feed/scripts/*.py       # 13 个
  /home/jiuben/tdx-data-feed/v5/scripts/*.py     # 15 个

系统：
  /home/jiuben/tdx-data-feed/systemd/tdxfeed-sync.{timer,service}
  /home/jiuben/tdx-data-feed/.codebuddy/automations/v5-daemon/
  /home/jiuben/.codebuddy/mcp.json               # github MCP

备份：
  /home/jiuben/backups/train_output_orphan_20260925_0830.tar.gz  # 4.5G / 134 文件
```

### 7.3 关键数字

| 指标 | 值 |
|---|---|
| 全 A 翻倍样本 (v3) | **22972** 样本 / **1942** 只 / 命中率 **24.2%** |
| 方案 D Test AUC | **0.8144** |
| 阈值 0.7 精度 | **83.15%** |
| 阈值 0.8 精度 | **89.40%** |
| v6_eval 一致率 | **21/30 (70%)** |
| v5_v6_compare 相似度 | **12%** |
| v5 token 数 vs v6 | 257 vs 111 |
| v7 eval_loss | **0.1914** (三模型最低) |
| Top 10 妖股北交所占比 | 4/10 = 40% |
| 北交所全量贡献 | 344 只 × 1397 行/只 = 48 万行 |

### 7.4 当前未完成项

- [ ] **v7 部署到 Ollama**：adapter 已训好，但需跑 `deploy_v5.sh` 同款流程（merge → GGUF → Modelfile → ollama create）
- [ ] **v8 LoRA fusion**：rank 不兼容（v5=8, v6=16），需 model souping 或重训
- [ ] **方案 D 上线**：模型 AUC 0.81 已可生产化，但缺 `predict.py`（每日全市场预测）
- [ ] **方案 E**：识别"翻倍当日信号"（已是翻倍则跟随买入的策略）
- [ ] **方案 F**：行业/概念识别（akhare `stock_board_industry_cons_em` 接口断连，需换源）
- [ ] **DELIVERY.md §12.11+ v7**：已加 v7 章节，但 v8/方案 D 上线章节待补

---

## 8. 一句话总结

**`tdx-data-feed` 是一个 `通联数据 → 本地 parquet → baostock/akshare 复权因子 → LightGBM 翻倍样本挖掘 → 妖股基因识别 → Qwen3-14B LoRA 微调 → Ollama 部署 → A 股量化对话` 的完整闭环**，主 Agent 通过 execute_command / 文件操作 / 子 Agent（code-explorer）/ MCP（github）四类原语驱动，主流程靠手动触发，长期任务靠 systemd timer + CodeBuddy automations。

> 本文档定位：工程自描述 + 新成员 onboarding 速查表。如有变动，请同步更新本文件。