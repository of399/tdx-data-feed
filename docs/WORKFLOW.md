# tdx-data-feed 工作流全景文档

> 生成：2026-09-27 · 最近更新：2026-10-21（对齐 daban 默认模型、5,235 日K、ETF/北交所下线、MCP 三服务与 systemd 调度现状）
> 范围：tdx-data-feed 完整系统（含数据采集 → LoRA 微调 → 妖股识别 → 模型评估 → 量化信号 → 报告产出全链路）
>
> 阅读路径：第 1 章建立心智模型 → 第 2-3 章按数据流向读 → 第 4-6 章按组件维度查 → 第 7 章是速查表。生产预测默认已切至 `daban`（小仓位 Top5），大仓位为 `vpnl_huber_stage1`（Top20）。

---

## 0. 系统目标（一句话）

把沪深 A 股 5,221 只股票的历史 K 线 + 复权因子 + 财务数据 + 行业/题材标签，喂给两条引擎：
- **量化信号引擎（LightGBM）**：方案 D（翻倍前 5 日信号，`scripts/predict.py` 每日全市场预测）+ 方案 E（翻倍跟随，`plan_e_backtest.py` / `plan_e_journal.py`）+ PnL Huber 回归（`train_pnl_huber_*.py`）
- **可对话模型（Ollama，Qwen3-14B LoRA）**：v5/v6/v7（**v7r 续训为生产优选**），经 `deploy_v5/v6/v7.sh`（merge → GGUF → ollama create）部署

输出三类产品：**结构化样本**（翻倍样本/预警信号，CSV+MD）、**每日预测**（`data/predictions/`）、**可对话模型**（Ollama 端点）。

---

## 1. 软件程序角色矩阵

### 1.1 数据采集层（6 个）

| 脚本 | 角色 | 输入 | 输出 | 触发方式 |
|---|---|---|---|---|
| `scripts/download_adj_factor_b.py` | A 股全市场复权因子（沪深）| baostock API | `data/adj_factor/{code}.parquet` × 5221 | 手动 / watchdog |
| `scripts/download_adj_factor_bj.py` | **已下线**（北交所数据 2026-10-21 移除）| — | — | — |
| `scripts/download_bj_daily.py` | **已下线**（北交所数据 2026-10-21 移除）| — | — | — |
| `scripts/download_adj_factor_watch.py` | 复权因子下载的 watchdog | - | 自动重启挂死的子进程 | 手动启动 |
| `scripts/download_universe.py` | 沪深 A 股代码清单/补全 | pytdx 枚举 + Sina 日 K | `data/all_a_stocks.json` | 手动 |
| `scripts/analyze_one.py` | 单只股多因子分析（技术面）| parquet/daily + adj_factor | dict（落库） | 单股触发 |
| `tdxfeed/full_sync.py` | **实际生产全量同步**（systemd 调用）：Sina 源日K + akshare 清单 + 财务增量 | Sina/akshare/baostock | `data/parquet/daily/*.parquet` + `sh\|sz/lday/*.day`（增量双写）| `tdxfeed-sync.timer` 每晚 18:00 |
| `tdxfeed/cli.py` | `sync` 子命令：单标的拉取双写（**仅手动单只，非全量**）| Sina/akshare/tencent | 同上 | 手动 |
| `scripts/akshare_industry_loader.py` | 申万/同花顺行业分类加载（股票→行业映射）| akshare `stock_industry_clf_hist_sw` | `data/industry/sw_*.csv`（335 三级）| 手动 |
| `scripts/akshare_concept_loader.py` | 同花顺(THS)题材/概念板块列表（名称+代码）加载与查询 | akshare `stock_board_concept_name_ths` | `data/concept/ths_concept_list.csv`（375 板块）| 手动（7 天缓存）|
| `scripts/tdx_concept_loader.py` | 通达信(tdx)个股→概念归属（解析 `block_gn/hy/fg/zs/wg.dat`，独立于 THS）| 本地通达信 `T0002/block/` | `data/concept/tdx_{gn,hy,...}.csv` | 手动（⚠️ 需用户放置标准 `block_gn.dat`）|

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
| `scripts/find_double_up_c.py` | 方案 C v1：**20 日内翻倍**筛选（不复权）| parquet/daily 5,235（脚本已排除 `.v1.bak`）| `data/double_up/double_up_2020_2026_detail.csv` (22972 样本) + `double_up_2020_2026_by_code.csv` |
| `scripts/find_double_up_c_v2.py` | 方案 C v2/v3：前复权价翻倍 + v1 对比 | parquet/daily + adj_factor | v3 csv + 假阳性/漏判分析 + report MD |
| `scripts/top10_deep_dive.py` | Top 10 妖股深度复盘 | v1 by_code + akshare stock_zyjs_ths | 22KB MD（涨停节奏/连板/成交量/行业）|
| `scripts/find_d_signal_5d.py` | 方案 D：翻倍前 5 日信号识别（LightGBM）| v1 detail + parquet | AUC 0.8144 模型 + 特征重要性报告 |
| `scripts/predict.py` | **方案 D 上线**：每日全市场 LightGBM 推理；默认 small=`daban`（29 维，次日涨停分类，Top5），full=`vpnl_huber_stage1`（21 维，Top20）| parquet/daily + features_5d | `data/predictions/predict_YYYYMMDD_HHMMSS.{csv,md}` + `predict_top{5,20}_*.csv` | `predict-daily.timer`（18:05）|
| `scripts/plan_e_backtest.py` | 方案 E：翻倍次日跟随策略回测（v2 最新报告胜率 35.3% / 均 3.16%，含成本）| double_up + parquet/daily | `data/plan_e/plan_e_*.csv\|md` | 手动 / `--compare` |
| `scripts/plan_e_journal.py` | 方案 E 实盘/模拟跟踪（journal.csv 实盘 / journal_sim.csv 模拟分离）| predict + plan_e 信号 | `data/plan_e/journal_*.csv\|md` | `plan-e-journal.timer` |
| `scripts/train_pnl_huber_*.py` | PnL Huber 回归（33/35 维，Test R² 0.166→0.278）| 行业/市场聚合特征 | PnL 模型（合并 v9 后继）| 手动 |

### 1.5 评估层（2 个）

| 脚本 | 角色 | 输入 | 输出 |
|---|---|---|---|
| `v5/scripts/v6_eval.py` | v6 vs v4 fast（30 只核心股）| ollama /api/generate | 一致率 + 平均相似度报告 |
| `v5/scripts/v5_v6_compare.py` | v5 vs v6（同一 prompt diff）| 同上 | 字段完整度 + 风格差异报告 |

### 1.6 监控与编排（5 个）

| 脚本 | 角色 | 触发 |
|---|---|---|
| `scripts/train_health.sh` | 训练进程健康检查（ckpt 记录、孤儿跳过；无训练会话时空闲退出）| systemd timer 每 5min |
| `scripts/plot_loss.py` | loss 曲线可视化 | 手动 `--all` 自动扫所有 v* 目录 |
| `scripts/gen_dashboard.py` | 综合 HTML 仪表盘 | 手动（包含所有数据快照）|
| `systemd/tdxfeed-sync.timer` | 行情同步（`tdxfeed.full_sync`，每晚 18:00）| systemd timer |
| `systemd/predict-daily.timer` | 方案 D 每日预测（收盘后，含 `precheck_freshness.sh` 数据新鲜度守卫）| systemd timer |
| `systemd/plan-e-journal.timer` | 方案 E 每日实盘信号归档（18:10，After predict-daily）| systemd timer |
| `systemd/plan-e-monitor.timer` | 方案 E 持仓监控（18:40）| systemd timer |
| `systemd/plan-e-weekly.timer` | 方案 E 周报汇总（周五 20:00）| systemd timer |
| `systemd/canary-health.timer` | 金丝雀健康检查（数据/模型/timer/obsidian 漂移，每 5min）| systemd timer |
| `systemd/obsidian-sync.path` | Obsidian vault 源↔部署零漂移监控 | systemd path |
| `.codebuddy/automations/*/memory.md` | 4 个自动化目录仅保留记忆，无 `automation.toml`，**不参与调度** | - |

### 1.7 数据转换 / 辅助（5+ 个）

- `scripts/distill_outputs.py`、`scripts/batch_distill.py`、`scripts/build_distill_skeleton.py`、`scripts/convert_distill.py` — 蒸馏数据预处理
- `scripts/finalize_eval.py`、`scripts/redo_finalize.py` — 评估结果定稿
- `scripts/test_bjs_akshare.py` — 北交所 akshare 接口冒烟测试（数据已下线，脚本仅存档）
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

- **现状**：`.codebuddy/automations/` 下有 4 个目录（`v4` / `v5-daemon` / `v8-retrain-review` / `v9-training-watch`），每个只有 `memory.md`，**没有 `automation.toml`**，因此不会被 CodeBuddy 调度。
- **真实定时任务全部在 systemd user timer**：`tdxfeed-sync` / `predict-daily` / `plan-e-journal` / `plan-e-monitor` / `plan-e-weekly` / `canary-health` / `trainsh-health` / `tdx-clean-logs` 等。
- 若未来要恢复 CodeBuddy automation，必须补 `automation.toml` 并在 IDE 中启用；仅 `memory.md` 不构成自动化。

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

### 4.1 IDE 级 MCP 服务（`.codebuddy/.mcp.json`）

| Server | 命令 | 工具集 | 用途 |
|---|---|---|---|
| **github** | `docker run ... ghcr.io/github/github-mcp-server` | repos, issues, pull_requests, actions, code_search | PR/Issue/搜索代码 |
| **codebuddy-integration** | IDE 内置集成 server | CloudBase / Supabase / CloudStudio / EdgeOne / Lighthouse | 云服务集成 |

### 4.2 技能运行时 MCP 服务（`agents/runtime.py` 拉起）

| Server | 脚本 | 工具 | 用途 |
|---|---|---|---|
| **tdx-parquet-reader** | `mcp/servers/parquet_reader.py` | `list_codes` / `read_daily_kline` / `read_xdxr` / `read_finance` | 读本地 parquet（日K/复权/财务） |
| **tdx-calc** | `mcp/servers/calc.py` | `calc_ma` / `calc_macd` / `calc_kdj` / `calc_rsi` / `calc_boll` / `calc_adj_factor` | 技术指标计算（NaN 已清洗） |
| **tdx-vault** | `mcp/servers/vault.py` | `search` / `add_doc` / `list_cats` | Obsidian/StockVault 检索（lexical + 可选向量）；`search` 支持 `category` / `path_prefix` / `exclude_category` 目录过滤 |
| **tdx-news** | `mcp/servers/news.py` | `search_stock_news` / `search_announcements` / `market_news` | 实时新闻/公告/市场要闻（akshare，5 分钟缓存） |
| **tdx-finance-deep** | `mcp/servers/finance_deep.py` | `get_metrics` / `get_statement` / `list_statement_fields` | 三大报表与扩展指标（本地 + akshare 7 天缓存） |
| **tdx-backtest** | `mcp/servers/backtest.py` | `list_strategies` / `run_backtest` / `run_signal_backtest` | 单股回测（前复权、T+1、费用滑点） |

> 这 6 个 server 不注册到 IDE `.mcp.json`，只由 `skills/*.yaml` 的 `mcp:` 步骤按需拉起；vault 索引由 `scripts/build_vault_index.py` 构建（当前 6,923 chunks / 5,668 notes）。`--embed` 的向量不进 JSONL，而是落成 sidecar `data/vault_store.vectors.npy` + `.vectors.json`（含 `rows_hash`）；加载时行数或 `rows_hash` 对不上就退回词法，不会在错位的行上算相似度。索引同时收 vault 下的 PDF（PyMuPDF，`--no-pdf` 跳过）。
>
> vault 中的一级目录 `09-理论讲义`（75 篇笔记，由 `scripts/import_theory_notes.py` 从财经七书讲义拆分生成）只供 `theory-reference` 技能检索；个股/新闻类技能调用 `search` 时必须传 `exclude_category=09-理论讲义`，否则讲义正文会挤占 top-k。
>
> 讲义笔记 frontmatter 带 `related_skills`（按书名映射，如计量经济学 → `backtest`/`indicator-match`），构建索引时进 `meta`，`theory-reference` 返回的每条 hit 都带上，runtime 再去重汇总到顶层 `related_skills`，CLI 据此提示"可接技能"，把教材理论接到实盘数据上。

#### 4.2.1 意图路由（`agents/orchestrator.py`）

`route()` 按顺序匹配 `INTENT_PATTERNS`，命中即返回；`theory-reference` 走前置独立判定，不进该列表。

| 顺序 | 意图 | 触发条件 | 必需参数 |
|---|---|---|---|
| 0 | **theory-reference** | 查询中**不含 6 位股票代码**（`CODE_RE`，前后不能再有数字）**且**命中 `THEORY_PATTERN`（什么是/定义/原理/公式/推导/讲义/第 N 章/七书书名/异方差等术语） | `query`（书名可选，`extract_book` 只在命中**唯一**一本书时收窄，跨书比较一律 `all`） |
| 1 | financial-summary | 财务/营收/净利润/ROE/EPS/PE/PB/报表/现金流 | `code` |
| 2 | adj-factor | 复权/除权除息/送转/分红 | `code` |
| 3 | news-summary | 新闻/公告/政策/舆情/利好利空 | `code` |
| 4 | indicator-match | 指标/形态/金叉/均线/MACD/KDJ/RSI | `code` |
| 5 | kline-analysis | 含 `sh|sz|bj` 前缀代码 | `code`（LLM 不可用时自动降级 indicator-match） |
| 兜底 | indicator-match | 以上都不匹配 | `code` |

> 「不含代码」是 theory-reference 的硬前提：否则 `sh600519 的护城河是什么` 会被讲义抢走。日期串（如 `20261021`）因 `CODE_RE` 的前后数字断言不会被误判成代码。

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
主 Agent
  ├── execute_command ──→ bash / python scripts ──→ 文件系统 / ollama / GPU
  ├── read_file / write_to_file / replace_in_file ──→ 本地仓库
  ├── search_content / search_file ──→ ripgrep / glob
  ├── lsp ──→ LSP server (pyright 等)
  ├── task (code-explorer) ──→ 子 Agent 返回摘要
  ├── mcp__github__* ──→ Docker → github-mcp-server → github.com
  ├── codebuddy-integration ──→ cloudStudio / tcb / supabase
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
│  akshare API  ──────┼─→ download_adj_factor_b.py / full_sync.py            │
│  Eastmoney ─────────┘                  ↓                                    │
│                                       data/adj_factor/ + data/parquet/daily/│
│                                       (5,221 复权 + 5,235 日K parquet)       │
│                                             ↓                               │
│  pytdx / akshare 清单 ──→ download_universe.py → data/all_a_stocks.json    │
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
│  find_d_signal_5d.py (LightGBM) ──────→ d_signal_5d_model_stage1/daban.txt   │
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
│  - data/double_up/*.csv / *.md × 9 + training_dashboard.html               │
│  - data/predictions/predict_*.{csv,md}（方案 D 每日全市场预测）            │
│  - data/plan_e/plan_e_*.{csv,md} + journal_*.csv（方案 E 回测/实盘）       │
│  - data/industry/sw_*.csv + data/concept/ths_concept_list.csv（行业/题材） │
│  - DELIVERY.md（§12 全章节）                                               │
│  - Ollama: qwen3-14b-tdx-v5/v6/v7/v7r/v7r1115/fast（v7r 为生产优选）      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 关键数据格式与位置

```
tdx-data-feed/
├── data/
│   ├── all_a_stocks.json              # 5,221 只沪深 A 股清单
│   ├── adj_factor/                    # 5,221 .parquet (date, adj_factor, qfq_close, raw_close, code)
│   ├── parquet/
│   │   ├── daily/                     # 5,235 .parquet (5,221 沪深个股 + 14 指数；另 5,232 个 .v1.bak 备份)
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
│       ├── qwen3-14b-tdx-v7/{checkpoint-200, 400, 500}/
│       └── qwen3-14b-tdx-v7-resume/{checkpoint-800, 1000, 1115}/
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
            ┌─────── download_adj_factor_b.py ──────┐
            │                                        │
            ▼                                        ▼
   data/adj_factor/ (5,221)              data/parquet/daily/ (5,235)
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
| v5 训练续训 | - | ✅ | `.codebuddy/automations/v5-daemon/` 仅记忆，未配置 `automation.toml` |
| 训练健康检查 | ✅ | - | systemd timer `trainsh-health.timer` (5min) |
| 方案 B 下载 | - | ✅ | 用户指令（一次性大规模爬取）|
| v6/v7 训练 | - | ✅ | 用户指令 |
| deploy | - | ✅ | 用户指令（修 GGUF 工具链后才成功）|
| 翻倍筛选 v1/v3 | - | ✅ | 用户指令 |
| 方案 D 每日预测（predict.py）| ✅ | - | systemd `predict-daily.timer`（收盘后，依赖 tdxfeed-sync）|
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
| qwen3-14b-tdx-v7 | 8 | 16 | 8000(v5续训) | **0.1914** | ✅ 已部署（v7r 续训为生产优选）|
| qwen3-14b-tdx-v7r | 16 | 32 | v7 续训 | - | ✅ Ollama 生产端点 |
| qwen3-14b-tdx-v8 (fusion) | - | - | - | - | ❌ rank 不兼容未生成 |
| **daban (LightGBM)** | - | - | 29 维（26 基础 + 3 涨停）| Top5 命中率 24.52% | ✅ **方案 D 默认（small）** |
| vpnl_huber_stage1 (LightGBM) | - | - | 阶段1 因子 21 维 | OOS Sp 0.3475 | ✅ 大仓位（full，Top20） |

### 7.2 关键文件路径

```
数据：
  /home/jiuben/tdx-data-feed/data/all_a_stocks.json     # 5,221 只清单
  /home/jiuben/tdx-data-feed/data/parquet/daily/*.parquet  # 5,235 日 K（不含 .v1.bak）
  /home/jiuben/tdx-data-feed/data/adj_factor/*.parquet    # 5,221 复权因子

模型：
  /home/jiuben/models/Qwen3-14B/                          # base
  /home/jiuben/models/Qwen3-14B-tdx-v9-merged/            # v9 合并（当前唯一 HF merged 目录）
  /home/jiuben/models/Qwen3-14B-v7r-merged-q8_0.gguf      # v7r GGUF（15G，生产端点）
  /home/jiuben/models/Modelfile-v7r-q8                    # Ollama 配置
  /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v7/         # v7 LoRA
  /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v7-resume/  # v7r 续训 LoRA（checkpoint-1115）

报告：
  /home/jiuben/tdx-data-feed/data/double_up/*.md  × 9
  /home/jiuben/tdx-data-feed/data/double_up/training_dashboard.html  # 174KB
  /home/jiuben/tdx-data-feed/DELIVERY.md         # 587 行含 §12

脚本：
  /home/jiuben/tdx-data-feed/scripts/*.py       # 13 个
  /home/jiuben/tdx-data-feed/v5/scripts/*.py     # 15 个

系统：
  /home/jiuben/tdx-data-feed/systemd/tdxfeed-sync.{timer,service}
  /home/jiuben/tdx-data-feed/systemd/predict-daily.{timer,service}
  /home/jiuben/.config/systemd/user/              # 已安装的 user timer
  /home/jiuben/tdx-data-feed/.codebuddy/.mcp.json # IDE MCP（github + codebuddy-integration）

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
| Top 10 妖股北交所占比（历史复盘） | 4/10 = 40% |
| 北交所全量贡献（历史复盘，数据已下线） | 344 只 × 1397 行/只 = 48 万行 |

### 7.4 当前未完成项

- [x] **v7 部署到 Ollama**：已完成，并新增 v7r 续训作为生产优选端点
- [ ] **v8 LoRA fusion**：rank 不兼容（v5=8, v6=16），需 model souping 或重训（仍未解决）
- [x] **方案 D 上线**：`scripts/predict.py` 已每日生产运行（`predict-daily.timer` 18:05），默认 `daban` + Top5 + 阈值0.7；大仓位 `vpnl_huber_stage1` + Top20
- [x] **方案 E**：翻倍跟随策略已落地（`plan_e_backtest.py` 回测 + `plan_e_journal.py` 实盘/模拟跟踪）
- [x] **方案 F（行业/题材）**：`akshare_industry_loader.py`（申万 335 三级）+ `akshare_concept_loader.py`（THS 375 板块列表）已落地；⚠️ THS 概念**仅名称+代码列表**，成分股明细需另接 Eastmoney `stock_board_concept_cons_em`（不同后端，未纳入）
- [ ] **PnL Huber + akshare SW 部署为 v9 后继**：替换 v8 生产环境（记忆中列为后续工作）
- [x] **DELIVERY.md v7/v8/方案 D 章节**：已补（本次审查回填）
- [ ] **路线图（4 项 PENDING）**：详见 `docs/ROADMAP_2026-10-16.md`——① v7r→v9 量化迭代 ② 方案 D→E 信号联调 ③ 监控栈按需评估 ④ docs 治理（✅ 已完成）

---

## 7.5 部署形态说明（2026-10-16 审查补正）

- **生产形态 = `venv` 虚拟环境 + 用户级 systemd timer**（唯一实际运行方式）：`tdxfeed-sync` → `predict-daily` → `plan-e-journal` 顺序编排，`canary-health` 每 5min 巡检。
- **`docker-compose.yml` 是目标架构（未实际部署）**：含 Ollama/PostgreSQL/Redis/Prometheus/Grafana + exporters，但当前生产**未启用**容器栈；Prometheus/Grafana 监控当下不存在，可观测性依赖 `canary` 健康检查与日志。
- **监控栈(Prometheus/Grafana)维持未部署（2026-10-16 复核）**：资源核查显示 2×RTX 5060Ti(16G) 已被 Ollama 占满(~96% 利用率)，宿主机空闲内存仅 1.4G（13G 可回收缓存）。按"避免与本地 Ollama 争抢资源"约束，**暂不部署**；docker 29.7.2 已就绪，待 GPU/内存宽松时再启。
- **告警桥(pushplus-bridge)已停用（2026-10-16）**：0 调用方空转，`systemctl --user disable --now` 后 inactive；端口 :5002 关闭；3 个文件（systemd 单元/桥脚本/token）保留供回滚。详见 `docs/pushplus_bridge_status_2026-10-16.md`。
- **Makefile ≠ 生产口径（已对齐）**：`make data-sync` 实为单标的 `tdxfeed.cli sync`（无参默认 `sh600000`，**不会拉全市场**）；全市场增量同步由 `make full-sync`（`tdxfeed.full_sync`，即 `tdxfeed-sync.timer` 每晚 18:00 调用的生产入口）负责。现已在 Makefile 新增 `full-sync` 目标并加注释澄清分工；改调度须同步两边，避免行为漂移。
- **systemd 漂移铁律**：凡改 `systemd/` 下文件，提交后必须 `make timer-install` + `daemon-reload`，否则加载旧文件（被 canary 持续检测告警）。

---

## 8. 一句话总结

**`tdx-data-feed` 是一个 `akshare/pytdx 清单 → 本地 parquet → akshare/Sina 行情 → LightGBM 翻倍/打板信号挖掘 → 妖股基因识别 → Qwen3-14B LoRA 微调 → Ollama 部署 → A 股量化对话` 的完整闭环**，主 Agent 通过 execute_command / 文件操作 / 子 Agent（code-explorer）/ MCP（github + 运行时 tdx 三服务）四类原语驱动，主流程靠手动触发，长期任务靠 systemd user timer。

> 本文档定位：工程自描述 + 新成员 onboarding 速查表。如有变动，请同步更新本文件。
