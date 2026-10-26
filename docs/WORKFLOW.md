# tdx-data-feed 工作流全景文档

> 生成：2026-09-27 · 最近更新：**2026-10-26**（对齐 daban 默认模型、ETF/北交所下线、方案E 实盘入口退出、daban 试验仓票据线、复权扩展层、监控栈与 systemd 调度现状）
> 范围：tdx-data-feed 完整系统（数据采集 → 特征/样本 → LightGBM 信号 → 方案E 回测 → 盘中委托单 → 告警巡检全链路）
>
> 阅读路径：第 1 章建立心智模型 → 第 2-3 章按数据流向读 → 第 4-6 章按组件维度查 → 第 7 章是速查表 → 第 8 章是变更记录。
> **生产预测默认 = `daban`（小仓位 Top5）；大仓位 = `vpnl_huber_stage1`（Top20）。LLM 线已于 2026-10-25 归档退出评估入口，战略止损，不再投入。**
> 配套快照：`docs/WORKFLOW_SNAPSHOT_2026-10-26.md`（当日实况，含与本文的 9 项偏差说明）

---

## 0. 系统目标（一句话）

把沪深 A 股 **5,221 只**股票的历史 K 线 + 复权因子 + 财务数据 + 行业/题材标签，喂给两条引擎：

- **量化信号引擎（LightGBM，生产必需）**：方案 D（打板次日信号，`scripts/predict.py` 每日全市场预测）+ 方案 E（翻倍跟随，`plan_e_backtest.py` / `plan_e_journal.py`）+ PnL Huber 回归（`train_pnl_huber_*.py`）
- **可对话模型（Ollama，Qwen3-14B LoRA，**研究/可解释性**）**：v5/v6/v7/v7r，经 `deploy_v5/v6/v7.sh`（merge → GGUF → ollama create）部署；**10-25 起评估入口已归档，战略上已止损，不再投入**

输出四类产品：**结构化样本**（翻倍样本/预警信号，CSV+MD）、**每日预测**（`data/predictions/`）、**方案E 研究结论**（`data/plan_e/`）、**盘中人工委托单**（`data/daban/tickets/`）。

> 2026-10-21 起 **ETF 与北交所数据已从项目移除**（含脚本下线与 `make data-bj` 删除），本项目现为**纯沪深 A 股口径**。

---

## 1. 软件程序角色矩阵

### 1.1 数据采集层（9 个）

| 脚本 | 角色 | 输入 | 输出 | 触发方式 |
|---|---|---|---|---|
| `tdxfeed/history_paging.py` | **腾讯日K 分页深取适配器**（2026-10-26 新增）：把"单次返回条数上限"变成可判异常 | 腾讯接口 | 历史日K 分页 | 由 `full_sync` / 回填调用 |
| `tdxfeed/symbols.py` | **A 股 symbol 口径**（2026-10-26 新增）：统一代码 → 数据源 symbol | 代码 | 规范化 symbol | 被审计/回填调用 |
| `scripts/audit_daily_history_depth.py` | **日线深度只读审计**（2026-10-26 新增）：输出"谁需要回填"的**可判分类** | parquet/daily | 审计报告（实测免探测 747 只）| 手动 |
| `scripts/backfill_daily_history.py` + `tdxfeed/backfill_history.py` | **幂等回填 runner**（2026-10-26 新增）：续跑单权威 = done 集；连续失败熔断 | 审计结论 | 补齐后的 parquet | 手动 / 续跑 |
| `tdxfeed/adj_factor_history.py` | **后复权因子反推**（2026-10-26 新增）：从 hfq / 未复权两条序列反推，**只写 `data/adj_factor_ext/`** | hfq + raw | `data/adj_factor_ext/*.parquet`（**当前 0 文件，未回填**）| 手动 |
| `tdxfeed/amount_fix.py` | **成交额缺口估算**（2026-10-26 新增并接入写入路径）：Sina 日K 无 amount 时用 `close × volume(手) × 100` 估算，**只补缺口不覆盖真值**；`writers.merge_write` / `incremental_update` 写入时自动调用 | 日线 df | 同 df（补 amount 列）| 写入路径自动 |
| `scripts/backfill_daily_amount.py` | 成交额历史批量回填（**默认 dry-run**，落盘须显式 `--apply`；写前备份到 `data/parquet/daily_amount_bak/`）| daily parquet | 补齐后的 parquet + `.day` | 手动 |
| `scripts/download_universe.py` | 沪深 A 股代码清单/补全 | pytdx 枚举 + Sina 日 K | `data/all_a_stocks.json` | 手动 |
| `scripts/analyze_one.py` | 单只股多因子分析（技术面）| parquet/daily + adj_factor | dict（落库） | 单股触发 |
| `tdxfeed/full_sync.py` | **实际生产全量同步**（systemd 调用）：Sina 源日K + akshare 清单 + 财务/复权增量（**2026-10-26 起接入腾讯源分页深取**）| Sina/akshare/baostock/腾讯 | `data/parquet/daily/*.parquet`（5,236）+ `sh\|sz/lday/*.day`（增量双写）| `tdxfeed-sync.timer` 每晚 18:00 |
| `tdxfeed/cli.py` | `sync` 子命令：单标的拉取双写（**仅手动单只，非全量**）| Sina/akshare/tencent | 同上 | 手动 |
| `scripts/akshare_industry_loader.py` | 申万/同花顺行业分类加载（股票→行业映射）| akshare `stock_industry_clf_hist_sw` | `data/industry/sw_*.csv`（335 三级）| 手动 |
| `scripts/akshare_concept_loader.py` | 同花顺(THS)题材/概念板块列表（名称+代码）加载与查询 | akshare `stock_board_concept_name_ths` | `data/concept/ths_concept_list.csv`（375 板块）| 手动（7 天缓存）|
| `scripts/tdx_concept_loader.py` | 通达信(tdx)个股→概念归属（解析 `block_gn/hy/fg/zs/wg.dat`，独立于 THS）| 本地通达信 `T0002/block/` | `data/concept/tdx_{gn,hy,...}.csv` | 手动（⚠️ 需用户放置标准 `block_gn.dat`）|

**已下线 / 弃用（勿再调用）**：`download_adj_factor_bj.py`、`download_bj_daily.py`、`download_etf_bond.py`、`backfill_adj_etf.py`（2026-10-21 北交所/ETF 下线，仅留弃用提示）；`download_akshare_bj.py`、`min_sync2.py`（已不存在）；复权与财务现由 `full_sync` 内置 `writers` / `fin_parquet` 产出。`scripts/download_adj_factor_b.py` 仍存在于仓库，但**生产路径为 `full_sync` 内置**。

### 1.2 训练层（2 条线，生产只需 LightGBM）

| 线 | 脚本/配置 | 基础 | 数据 | 输出 | 状态 |
|---|---|---|---|---|---|
| **A 生产信号（LightGBM, CPU）** | `scripts/train_daban_model.py` | — | parquet/daily + features_5d（29 维）| `data/double_up/d_signal_5d_model_daban.txt` | ✅ **生产默认（small）** |
| | `scripts/find_d_signal_5d.py` | — | 21 维 Polars 精选特征 | `d_signal_5d_model_vpnl_huber_stage1.txt` | ✅ 大仓位（full） |
| **B LLM 研究（Qwen3-14B QLoRA）** | `training/config/tdx_v5_lora_simple.yaml` 等 | Qwen3-14B | `train/data/tdx_v{5,6,7}_train.jsonl` | `train/output/qwen3-14b-tdx-v*` | ⛔ **已止损 · 10-25 评估入口归档** |

> LLM 线连续两代 mode collapse（v9 ckpt-200 起、v9r ckpt-800/1000），`run_v4_eval.py` 与 `tdx_v9r` 配置已移入 `_archive/` 并标注废弃。**仅用于可解释性/对话式分析（如 `kline-analysis` 技能），不得当作生产预测器。**

### 1.3 部署层（2 个 shell + 1 个 helper）

| 脚本 | 角色 | 步骤 |
|---|---|---|
| `v5/scripts/deploy_v5.sh` | v5 全流程部署 | ①merge LoRA→28GB · ②GGUF f16 转换 · ③写 Modelfile · ④ollama create |
| `v5/scripts/deploy_v6.sh` | v6 全流程部署（结构与 v5 一致）| 同上 |
| `scripts/merge_v5_v6_lora.py` | LoRA 权重融合（**v5=rank8 vs v6=rank16，shape 不兼容，未完成**）| v5+v6 safetensors → v8 |

### 1.4 分析层（6 个核心脚本 + 3 个试验仓脚本）

| 脚本 | 角色 | 输入 | 输出 |
|---|---|---|---|
| `scripts/find_double_up_c.py` | 方案 C v1：**20 日内翻倍**筛选（不复权）| parquet/daily 5,236（脚本已排除 `.v1.bak`）| `data/double_up/double_up_2020_2026_detail.csv`（**17,582 样本**，2026-10-24 重算）+ `double_up_2020_2026_by_code.csv` |
| `scripts/find_double_up_c_v2.py` | 方案 C v2/v3：前复权价翻倍 + v1 对比 | parquet/daily + adj_factor | v3 csv + 假阳性/漏判分析 + report MD |
| `scripts/top10_deep_dive.py` | Top 10 妖股深度复盘 | v1 by_code + akshare stock_zyjs_ths | 22KB MD（涨停节奏/连板/成交量/行业）|
| `scripts/find_d_signal_5d.py` | 方案 D：翻倍前 5 日信号识别（LightGBM）| v1 detail + parquet | AUC 0.8144 模型 + 特征重要性报告 |
| `scripts/predict.py` | **方案 D 上线**：每日全市场 LightGBM 推理；默认 small=`daban`（29 维，次日涨停分类，Top5），full=`vpnl_huber_stage1`（21 维，Top20）| parquet/daily + features_5d | `data/predictions/predict_YYYYMMDD_HHMMSS.{csv,md}` + `predict_top{5,20}_*.csv` | `predict-daily.timer`（18:05）|
| `scripts/plan_e_backtest.py` | 方案 E：翻倍次日跟随策略回测（诚实成交口径；最新数字看 `data/plan_e/plan_e_backtest_v2_latest.md`，2026-10-24 报告 112 笔：胜率 33.9% / 均收益 +1.16%/笔，四个检验都无法拒绝「无超额收益」；`--random-timing-trials` 的随机择时基线另测择时，入场日与蒙无异）| double_up + parquet/daily | `data/plan_e/plan_e_*.csv\|md` | 手动 / `--compare` |
| `scripts/plan_e_journal.py` | 方案 E 交易跟踪（journal.csv 实盘 / journal_sim.csv 模拟分离）；2026-10-24 起实盘入口停用，`--mode buy/sell` 仅保留人工记账 | predict + plan_e 信号 | `data/plan_e/journal_*.csv\|md` | `plan-e-journal.timer`（2026-10-24 晚重开，产出仍是研究口径） |
| `scripts/train_pnl_huber_*.py` | PnL Huber 回归（33/35 维，Test R² 0.166→0.278）| 行业/市场聚合特征 | PnL 模型（合并 v9 后继）| 手动 |
| **`scripts/daban_entry.py`** | **daban 试验仓盘中委托单**（2026-10-25 新增，10-26 已启用）：每 5 分钟产一份**人工委托单文本**（横幅 + 五条自动停止线；同日同概念限注 2 笔）| 实时行情 + 生产模型信号 | `data/daban/tickets/ticket_YYYYMMDD.{md,json}`（`daban-ticket.timer`）|
| `scripts/daban_paper.py` / `scripts/daban_orderbook_logger.py` | 试验仓账本 / 委托与盘口记录 | 委托单 + 盘口 | `data/daban/` 各类 json/csv |
| **执行研究线（研究/诊断，不下单）**：`tdxfeed/daban/{paper,orderbook_log,seal_classifier,risk_gateway,fill_model,execution_backtest,topk_pnl}.py` + `scripts/daban_{min60_features,event_table,seal_research,exec_research,paper,orderbook_logger,intraday_ic,relay3_robustness,filter_oos,min60_exec,formula1_screen,formula1_validate,band_check,limit_topk_eval}.py` | 把「模型概率」拆成**能不能成交 → 成交后能不能封 → 封了赚不赚**三层逐层证伪 | 日线+分钟线+生产模型(只读) | `data/daban/`（event_table / seal_classifier / execution_backtest / paper / orderbook）+ `docs/DABAN_*.md` | 手动 |

### 1.5 评估层（2 个）

| 脚本 | 角色 | 输入 | 输出 |
|---|---|---|---|
| `v5/scripts/v6_eval.py` | v6 vs v4 fast（30 只核心股）| ollama /api/generate | 一致率 + 平均相似度报告 |
| `v5/scripts/v5_v6_compare.py` | v5 vs v6（同一 prompt diff）| 同上 | 字段完整度 + 风格差异报告 |

### 1.6 监控与编排（systemd 单元清单）

| 脚本 | 角色 | 触发 |
|---|---|---|
| `scripts/train_health.sh` | 训练进程健康检查（ckpt 记录、孤儿跳过；无训练会话时空闲退出）| systemd timer 每 5min |
| `scripts/plot_loss.py` | loss 曲线可视化 | 手动 `--all` 自动扫所有 v* 目录 |
| `scripts/gen_dashboard.py` | 综合 HTML 仪表盘 | 手动（包含所有数据快照）|
| `systemd/tdxfeed-sync.timer` | 行情同步（`tdxfeed.full_sync`，每晚 18:00）；失败走 `OnFailure=onfailure-alert@…` 推送 | systemd timer |
| `systemd/predict-daily.timer` | 方案 D 每日预测（收盘后，含 `precheck_freshness.sh` 数据新鲜度守卫）；对 sync 是 `Requires=`（同步失败则整轮不跑），失败同样推送 | systemd timer |
| `systemd/plan-e-journal.timer` | 方案 E 每日信号清单归档（18:10，After predict-daily）——**2026-10-24 晚重开**：直接复用 08:45 那份盘前清单归档 `signals_YYYYMMDD.csv`，产出仍是研究口径、不给下单建议；读不到今日清单时不跑 CLI 兜底（晚间用的是收盘数据）| systemd timer |
| `systemd/plan-e-today.timer` | 方案 E 盘前今日清单（08:45 跑 `plan_e_backtest.py --today`）——**2026-10-24 晚重开**（当天上午曾随「退出实盘入口」一起停用）；退出实盘入口这件事不变，`--mode daily` 依旧不算拆单量 | systemd timer |
| `systemd/plan-e-double-up.timer` | 方案 E 翻倍样本重建（18:30，排在 18:00 sync 与 18:10 journal 之后）——**2026-10-24 新增**：跑 `scripts/find_double_up_c.py` 重算翻倍样本 CSV（08:45 那次「近 7 日翻倍首次触发」预筛的候选池）；含 `precheck_freshness.sh` 与 `OnFailure`，脚本对零命中/某市场段归零非零退出并保留上一版；10-24 实测 16s 扫完 5,236 只 parquet，读失败 0 | systemd timer |
| **`systemd/daban-ticket.timer`** | **daban 试验仓：工作日盘中每 5 分钟产一份人工委托单**（`scripts/daban_entry.py`；**本单元不下单**）| 09-11、13-14 每 5 min · **enabled + active**（10-25 新增）|
| `systemd/plan-e-today.timer` | 方案 E 盘前今日清单（08:45 跑 `plan_e_backtest.py --today`）——**2026-10-24 晚重开**（当天上午曾随「退出实盘入口」一起停用）；退出实盘入口这件事不变，`--mode daily` 依旧不算拆单量 | systemd timer |
| `systemd/plan-e-double-up.timer` | 方案 E 翻倍样本重建（18:30，排在 18:00 sync 与 18:10 journal 之后）——**2026-10-24 新增**：跑 `scripts/find_double_up_c.py` 重算 `data/double_up/double_up_2020_2026_detail.csv`，那正是 08:45 那次「近 7 日翻倍首次触发」预筛的候选池；此前只能手工 `make analyze-double-up`，样本 `peak_date` 停在 2026-09-22，新触发日进不了清单。含 `precheck_freshness.sh` 与 `OnFailure`，脚本对零命中/某市场段归零非零退出并保留上一版；2026-10-24 实测 16s 扫完 5236 只 parquet，读失败 0 | systemd timer |
| `systemd/plan-e-monitor.timer` | 方案 E 持仓监控（18:40）——**2026-10-24 已 disable**：实盘账本为空（无 positions.json，journal.csv 只有表头）| systemd timer（停用） |
| `systemd/plan-e-weekly.timer` | 方案 E 周报汇总（周五 20:00）——**2026-10-24 已 disable**（无实盘流水可汇总）| systemd timer（停用） |
| `systemd/plan-e-real-refresh.timer` | 方案 E 实盘样本回灌调参（周日 21:00）——**从未启用**，2026-10-24 一并确认停用（输入是同一份空账本）| systemd timer（停用） |
| `systemd/canary-health.timer` | 金丝雀健康检查（数据/模型/timer/obsidian 漂移，每 5min）；它自己是告警员，所以也挂了 `OnFailure`（靠脚本 1h 冷却压重复）| systemd timer |
| `systemd/onfailure-alert@.service` | 不是 timer：`OnFailure=` 模板单元，把失败单元名拼成 alertmanager 载荷 POST 到 127.0.0.1:5002 的 pushplus 桥（同单元默认 1 小时冷却；`--dry-run` 只看载荷不推送）| 由上面的单元触发 |
| `systemd/obsidian-sync.path` | Obsidian vault 源↔部署零漂移监控 | systemd path |
| `.codebuddy/automations/*/memory.md` | 4 个自动化目录仅保留记忆，无 `automation.toml`，**不参与调度** | - |

**监控栈（系统级，2026-10-22 20:40 起 active+enabled）**：`prometheus` / `alertmanager` / `grafana-server`(:3000) / `pushplus_bridge`(:5002)；Prometheus 5 个抓取目标全 up（prometheus / alertmanager / node / canary / math-sympy-http），当前 0 条 firing 规则。

> 仓库里的 `systemd/pushplus-bridge.service`（user 级）**仍是 disabled**；在用的是 `/etc/systemd/system/pushplus_bridge.service`（system 级，以 jiuben 身份跑 `v5/scripts/pushplus_bridge.py`）。`onfailure-alert@` 和 Alertmanager 都往 :5002 投。

### 1.7 数据转换 / 辅助（5+ 个）

- `scripts/distill_outputs.py`、`scripts/batch_distill.py`、`scripts/build_distill_skeleton.py`、`scripts/convert_distill.py` — 蒸馏数据预处理
- `scripts/finalize_eval.py`、`scripts/redo_finalize.py` — 评估结果定稿
- `scripts/test_bjs_akshare.py` — 北交所 akshare 接口冒烟测试（数据已下线，脚本仅存档）
- `scripts/canary_health_check.py` — 金丝雀健康检查（7 维）；日志轮转由 `tdx-clean-logs.timer` + systemd 负责

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
- **真实定时任务全部在 systemd user timer**：通用那五是 `tdxfeed-sync` / `predict-daily` / `canary-health` / `trainsh-health` / `tdx-clean-logs`（外加 `obsidian-sync.path`），方案 E 侧另有三只 armed：`plan-e-today` / `plan-e-journal` / `plan-e-double-up`，**打板侧另有 `daban-ticket`**（工作日 09-11 / 13-14 每 5 分钟产人工委托单，10-25 新增，已 enabled + active）。方案 E 的六只单元（上面三只 + `plan-e-monitor` / `plan-e-weekly` / `plan-e-real-refresh`，最后一只从未启用过）文件都在库里：2026-10-24 上午五只一起 disable，当晚 `plan-e-today` + `plan-e-journal` 重开（自动产出的仍只是研究清单，退出实盘入口的决定不变），同日补上 `plan-e-double-up`（18:30 重建 08:45 那次预筛要读的翻倍样本 CSV），monitor / weekly / real-refresh 维持停用——它们的输入是那份空账本。`make timer-enable` 启用通用三只 + 这三只 plan-e timer 与 obsidian path，`trainsh-health` / `tdx-clean-logs` 靠持久化的 enable 状态活着。
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

**运行时技能（`skills/*.yaml`，9 个，由 `agents/orchestrator.py` 路由）**：

| Skill | LLM 后端 | 说明 |
|---|---|---|
| `kline-analysis` | `qwen3-14b-tdx`（transformers + peft）| K 线分析；LLM 不可用时自动降级 indicator-match |
| `financial-summary` | `qwen3-7b`（档位标注）| 本地财务 + finance-deep 按需扩展 |
| `news-summary` | `qwen3-7b`（档位标注）| tdx-news + vault |
| `adj-factor` | `none` | 纯工具，生产 CPU 计算依赖 |
| `indicator-match` | `none` | 纯规则 + 计算（与 LightGBM 预测无关，仅 GPU 降级）|
| `backtest` | — | tdx-backtest 纯工具回测 |
| `financial-statement` | — | 三大报表纯工具查询 |
| `theory-reference` | `none` | 财经七书讲义纯检索，限定 vault `09-理论讲义` 目录，不消耗 GPU |
| **`daban`** | `none` | **纯工具**：全市场 29 维打分 → T+1 收盘涨停概率 Top-N（唯一工具 `mcp:tdx-backtest.daban_top_picks`，约 3-4 秒）；⚠️ **只作排序研究、不是买入指令**（诚实成交口径 -0.78%/笔，`overall_go=false`）|

> Agent 主要靠 execute_command / file ops / Task(code-explorer)；运行时技能由 `agents/orchestrator.py` 意图路由分发（新增 `daban` 意图：打板/连板/首板/次日涨停/涨停候选/接力板）。

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
| **tdx-backtest** | `mcp/servers/backtest.py` | `list_strategies` / `run_backtest` / `run_signal_backtest` / **`daban_top_picks`** | 单股回测（前复权、T+1、费用滑点）+ **全市场 daban Top-N 打分**（与 `predict.py` 同源算 29 维） |

> 这 6 个 server 不注册到 IDE `.mcp.json`，只由 `skills/*.yaml` 的 `mcp:` 步骤按需拉起；vault 索引由 `scripts/build_vault_index.py` 构建（当前 6,923 chunks / 5,668 notes）。`--embed` 的向量不进 JSONL，而是落成 sidecar `data/vault_store.vectors.npy` + `.vectors.json`（含 `rows_hash`）；加载时行数或 `rows_hash` 对不上就退回词法，不会在错位的行上算相似度。索引同时收 vault 下的 PDF（PyMuPDF，`--no-pdf` 跳过）。
>
> vault 中的一级目录 `09-理论讲义`（75 篇笔记，由 `scripts/import_theory_notes.py` 从财经七书讲义拆分生成）只供 `theory-reference` 技能检索；个股/新闻类技能调用 `search` 时必须传 `exclude_category=09-理论讲义`，否则讲义正文会挤占 top-k。
>
> 讲义笔记 frontmatter 带 `related_skills`（按书名映射，如计量经济学 → `backtest`/`indicator-match`），构建索引时进 `meta`，`theory-reference` 返回的每条 hit 都带上，runtime 再去重汇总到顶层 `related_skills`，CLI 据此提示"可接技能"，把教材理论接到实盘数据上。

> ⚠️ **数学服务已迁独立虚拟环境 `venv-math/`**（2026-10-24）：主 venv 的 `antlr4 4.11` 与 `omegaconf 4.9.*` 存在死锁，数学 MCP 服务（`math_sympy_http`）改由 `venv-math` 承载；`make verify-math` 的校验 harness 与 stdio 注册也改用该环境，并已作为漂移守卫入库。**不要在主 venv 里跑数学服务。**

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
│  akshare API  ──────┼─→ full_sync.py（+ 腾讯日K分页深取 history_paging）     │
│  腾讯 / Eastmoney ──┘                  ↓                                    │
│                                       data/adj_factor/ + data/parquet/daily/│
│                                       (5,221 复权 + 5,236 日K parquet)       │
│                                             ↓                               │
│  pytdx / akshare 清单 ──→ download_universe.py → data/all_a_stocks.json    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                            加工 / 补历史层（2026-10-26 新）                 │
│  audit_daily_history_depth ──→ 谁需要回填（可判分类, 免探测 747 只）         │
│        ↓ backfill_daily_history（幂等, done 集为续跑单权威, 连续失败熔断）     │
│  adj_factor_history ──→ data/adj_factor_ext/（后复权, 只写扩展目录）         │
└─────────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  分析层                                      │
│                                                                             │
│  find_double_up_c.py (v1 不复权) ──┐                                        │
│                                       ├──→ data/double_up/double_up_2020    │
│  find_double_up_c_v2.py (v3 前复权) ┤   _2026_detail.csv (17,582 样本)       │
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
│  - data/plan_e/plan_e_*.{csv,md} + journal_*.csv（方案 E 回测/归档）       │
│  - data/daban/tickets/ticket_*.{md,json}（试验仓人工委托单, 只产单不下单） │
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
│   ├── adj_factor_ext/                # 后复权因子（10-26 新建, 当前 0 文件, 未回填）
│   ├── parquet/
│   │   ├── daily/                     # 5,236 .parquet（个股 5,228 + 指数 8；另 5,232 个 .v1.bak 备份）
│   │   ├── finance.parquet            # 1,667,841 行 财务
│   │   └── xdxr.parquet               # 299 行 股本变动
│   ├── v4/                            # 4733 条 v4 fast 训练数据
│   ├── v5_train/, v6_train/           # 原始 v5/v6 数据副本
│   ├── double_up/                     # 翻倍样本 17,582 行 + 9 个报告 + 5 个 csv
│   ├── daban/                         # 试验仓：tickets/ 委托单 + 各类评估 json
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
├── training/config/                   # LLaMA-Factory YAML（评估入口 10-25 已归档至 _archive/）
├── scripts/                           # 99 py + 23 sh
├── v5/scripts/                        # 15+ 部署/评估脚本
├── systemd/                           # 9 timer + 1 path + onfailure-alert@
├── venv/  venv-math/                  # 主环境 / 独立数学环境
├── docs/                              # 治理与实况文档
├── DELIVERY.md                        # §12 完整章节
└── .codebuddy/automations/v5-daemon/  # 仅 memory.md，不参与调度
```

---

## 6. 依赖关系与触发逻辑

### 6.1 任务依赖图（DAG）

```
              [akshare / Sina / 腾讯 / baostock 网络]
                            │
                            ▼
            ┌────────── tdxfeed.full_sync ──────────┐
            │                                        │
            ▼                                        ▼
   data/adj_factor/ (5,221)              data/parquet/daily/ (5,236)
            │                                         │
            │  [10-26 支线]                          │
            │  adj_factor_history → adj_factor_ext/  │
            │  audit_daily_history_depth             │
            │        ↓ backfill_daily_history（幂等） │
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

   [决策层 · 生产 · 纯 CPU]
   predict.py (18:05, Requires= tdxfeed-sync) ──→ data/predictions/predict_*.{csv,md}
   plan_e_backtest.py --today (08:45) ──→ data/plan_e/plan_e_today.csv
   plan_e_journal.py (18:10) ──→ data/plan_e/live/journal.csv（只归档, 不建仓）
   daban_entry.py (盘中每 5 min) ──→ data/daban/tickets/ticket_*.{md,json}（只产单, 人工下单）

   [研究支线 · 执行研究线（不参与生产决策）]
   min60_features → event_table → seal_research → exec_research
     ──→ data/daban/event_table_min60_*.parquet
     ──→ data/daban/seal_classifier_*.json   （Stage1 gate.passed = false）
     ──→ data/daban/execution_backtest_*.json（overall_go = false ⇒ 不进纸盘/资金）
   得到结论：成交率 46.6%，baseline 平均净收益 -1.30%/笔（毛 -1.15%）
             ⇒ 负期望来自入场时点，不是交易成本；Stage1 test AUC 0.52（接近随机）
   详见 docs/DABAN_EXECUTION_RESEARCH_LINE.md

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
| 训练健康检查 | ✅ | - | systemd timer `trainsh-health.timer` (5min) |
| 翻倍样本重建 | ✅ | ✅ | `plan-e-double-up.timer`（18:30）/ `make analyze-double-up` |
| 方案 D 每日预测（predict.py）| ✅ | - | systemd `predict-daily.timer`（收盘后，依赖 tdxfeed-sync）|
| 方案 E 盘前清单 | ✅ | ✅ | `plan-e-today.timer`（08:45 工作日）/ `make plan-e-today` |
| 方案 E 归档 | ✅ | ✅ | `plan-e-journal.timer`（18:10）/ `make journal` |
| **daban 试验仓委托单** | ✅ | ✅ | `daban-ticket.timer`（工作日盘中每 5 min）/ `scripts/daban_entry.py` |
| 日线深度审计 / 幂等回填 | - | ✅ | `scripts/audit_daily_history_depth.py` / `scripts/backfill_daily_history.py` |
| 后复权回填 | - | ✅ | `tdxfeed/adj_factor_history.py` → `data/adj_factor_ext/` |
| 方案 E 回测对比 | - | ✅ | `make plan-e-compare` / `scripts/plan_e_backtest.py --compare` |
| 生产模型训练 | - | ✅ | `scripts/train_daban_model.py`（⚠️ 不带 `--tag` 会覆盖生产模型）|
| **执行研究线**（Stage 0/1/2）| - | ✅ | `scripts/daban_min60_features.py` → `daban_event_table.py` → `daban_seal_research.py` → `daban_exec_research.py`（详见 `docs/DABAN_EXECUTION_RESEARCH_LINE.md`）|
| 盘口采集 / 纸盘 | - | ✅ | `scripts/daban_orderbook_logger.py` / `scripts/daban_paper.py`（**研究用，不下单**）|
| v5/v6/v7 训练、deploy | - | ⛔ | **已冻结**（LLM 路线两代坍缩，止损不重启）|
| v6_eval / v5_v6_compare | - | ⛔ | 已归档（`v5/scripts/`，仅存档）|
| Dashboard 重新生成 | - | ✅ | `make analyze-dashboard` |

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
| **腾讯日K 单次返回条数上限**（10-26）| `tdxfeed/history_paging.py` 分页深取，把上限变成**可判异常**；volume 单位=手（去掉多除的 100）|
| **腾讯源 WAF 501**（10-26）| 深取通道换镜像域 + 退避；实测并发阈值已写进脚本 |
| **`OnFailure=` 写在 `[Service]`**（10-25）| systemd 会**整段忽略** → 必须挪回 `[Unit]` |
| **主 venv 跑数学服务**（10-24）| antlr4 4.11 ↔ omgaconf 4.9.* 死锁 → 改用独立 `venv-math`；`make verify-math` 守卫 |
| 回填中断 | 幂等 runner：done 集为续跑单权威；连续失败熔断 |
| 样本为 0 / 某市场段归零 | `plan-e-double-up` 非零退出并保留上一版，不产出坏数据 |
| 方案 E 实盘/周报无输入 | monitor / weekly / real-refresh 保持 disable（实盘账本为空）|

---

## 7. 速查表

### 7.1 模型对比

| 模型 | 类型 | 维度 | 关键指标 | 部署/用途 | 状态 |
|---|---|---|---|---|---|
| **daban (LightGBM)** | 二分类 | 29 维（26 基础 + 3 涨停/连板）| OOS AUC **0.8147**；Top5 命中率 19.94~24.52% | ✅ **方案 D 默认（small, Top5）** | **生产** |
| **vpnl_huber_stage1 (LightGBM)** | Huber 回归 | 阶段1 因子 21 维 | OOS AUC 0.7580 / Sp 0.3475 | ✅ 大仓位（full，Top20） | 生产（回退）|
| d_signal_5d_model（早期）| 分类 | 18 维 | AUC 0.8144 | `--model prod` 备选 | 存档 |
| qwen3-14b-tdx-fast (v4) | LoRA r8/α16 | 4733 条 | eval_loss 0.23 | Ollama | 研究，**非生产** |
| qwen3-14b-tdx-v5 | LoRA r8/α16 | 12656 条 | eval_loss 0.1958 | Ollama | 研究，**非生产** |
| qwen3-14b-tdx-v6 | LoRA r16/α32 | 5238 条 | eval_loss 0.2883 | Ollama | 研究，**非生产** |
| qwen3-14b-tdx-v7 / v7r / v7r1115 | LoRA r8~16 | 8000(v5续训) | eval_loss 0.1914 | Ollama 端点 | 研究/可解释性，**非生产** |
| qwen3-14b-tdx-v8 (fusion) | LoRA fusion | - | - | ❌ rank 不兼容未生成 | 悬空，**不建议再做** |
| **qwen3-14b-tdx-v9 / v9r** | LoRA | 50748 条 | direction_acc 0.0202 / 0.0085~0.0333，spearman=0 | ❌ 连续两代 mode collapse | **已止损 · 10-25 归档** |

> **口径提醒**：daban 与 stage1 是**成品对比**（特征集不同），非受控实验；daban 训练标签为「次日收盘涨停」，天然偏好连板/一字板，已用 T 日开盘即涨停过滤缓解（Top5 不可交易占比 60% → 0%）。

### 7.2 关键文件路径

```
数据：
  /home/jiuben/tdx-data-feed/data/all_a_stocks.json     # 5,221 只清单
  /home/jiuben/tdx-data-feed/data/parquet/daily/*.parquet  # 5,236 日 K（个股 5,228 + 指数 8；不含 .v1.bak）
  /home/jiuben/tdx-data-feed/data/adj_factor/*.parquet    # 5,221 复权因子
  /home/jiuben/tdx-data-feed/data/adj_factor_ext/         # 后复权因子（10-26 新建, 0 文件未回填）
  /home/jiuben/tdx-data-feed/data/double_up/double_up_2020_2026_detail.csv  # 17,582 翻倍样本
  /home/jiuben/tdx-data-feed/data/daban/tickets/          # 试验仓人工委托单

模型：
  /home/jiuben/tdx-data-feed/data/double_up/d_signal_5d_model_daban.txt          # 生产默认(small)
  /home/jiuben/tdx-data-feed/data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt  # 大仓位(full)
  /home/jiuben/models/Qwen3-14B/                          # LLM base（研究线）
  /home/jiuben/models/Qwen3-14B-tdx-v9-merged/            # v9 合并（已失败，勿用）
  /home/jiuben/models/Qwen3-14B-v7r-merged-q8_0.gguf      # v7r GGUF（15G，生产端点）
  /home/jiuben/models/Modelfile-v7r-q8                    # Ollama 配置
  /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v7/         # v7 LoRA
  /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v7-resume/  # v7r 续训 LoRA（checkpoint-1115）

报告：
  /home/jiuben/tdx-data-feed/data/double_up/*.md  × 9
  /home/jiuben/tdx-data-feed/data/double_up/training_dashboard.html  # 174KB
  /home/jiuben/tdx-data-feed/data/predictions/predict_latest.md      # 方案 D 最新
  /home/jiuben/tdx-data-feed/data/plan_e/plan_e_backtest_v2_latest.md # 方案 E 最新
  /home/jiuben/tdx-data-feed/docs/WORKFLOW_SNAPSHOT_2026-10-26.md    # 当日实况快照
  /home/jiuben/tdx-data-feed/DELIVERY.md         # 587 行含 §12

脚本：
  /home/jiuben/tdx-data-feed/scripts/*.py       # 99 个
  /home/jiuben/tdx-data-feed/v5/scripts/*.py     # 15 个
  /home/jiuben/tdx-data-feed/tdxfeed/*.py       # 26 个（采集/特征/复权历史）

系统：
  /home/jiuben/tdx-data-feed/systemd/            # 9 timer + 1 path + onfailure-alert@
  /home/jiuben/.config/systemd/user/              # 已安装的 user timer
  /home/jiuben/tdx-data-feed/venv/  venv-math/    # 主环境 / 独立数学环境
  /home/jiuben/tdx-data-feed/.codebuddy/.mcp.json # IDE MCP（github + codebuddy-integration）

备份：
  /home/jiuben/backups/train_output_orphan_20260925_0830.tar.gz  # 4.5G / 134 文件
```

### 7.3 关键数字

| 指标 | 值 |
|---|---|
| 日 K parquet | **5,236**（个股 5,228 + 指数 8；另 5,232 个 `.v1.bak`）|
| 复权因子 | **5,221** |
| 沪深 A 股清单 | **5,221** |
| 全 A 翻倍样本 | **17,582**（10-24 重算；旧口径 22,972 因北交所/ETF 下线已失效）|
| 方案 D 候选池 | 4,923 只（10-23 预测）|
| 方案 D ≥0.7 高分 | 803 只（10-23）|
| daban OOS AUC | **0.8147**（vs stage1 0.7580，CI 不重叠）|
| Top5 命中率 | 2025 **24.52%** / 2026 **21.70%**（自然率 1.29~1.53%）|
| 方案 E v2（10-24, n=112）| 胜率 **33.9%** / 均收益 **+1.16%** / 夏普 0.12 / 索提诺 0.07 / 最大回撤 -23.63% / Calmar 0.02 |
| 方案 E 统计结论 | **95% CI 跨零，四检验均无法拒绝「无超额收益」**；随机择时基线与入场日无异 → **研究口径**|
| 翻倍样本免探测数 | 747 只（10-26 日线深度审计）|
| canary 维度 | 7（ollama / tdxfeed_sync / data_files / disk / timers / gpu / obsidian_sync）|
| 运行中定时器 | 10（含 3 个 5min 级：canary / trainsh / **daban-ticket**）|
| 执行研究 Stage 0 事件表 | **21,963** 触板事件 / 256 交易日 / 3,742 只（2025-09-05~2026-09-24）|
| 执行研究 Stage 1 封板分类 | 验证 AUC 0.543 → **测试 AUC 0.522**（接近随机）；`gate.passed=false` |
| 执行研究 Stage 2 成交回测 | 基础成交率 **46.6%**；baseline 平均净收益 **-1.30%/笔**、胜率 32.4%（毛 -1.15%）|
| 执行研究 分化 | 封住收盘 **+1.24%** vs 未封住 **-2.89%**；`overall_go=false` |
| 旧 LLM 参考值 | v7 eval_loss 0.1914；v6_eval 一致率 21/30 (70%)（**已归档，仅存档**）|

### 7.4 当前未完成项

- [x] **方案 D 上线**：`scripts/predict.py` 已每日生产运行（`predict-daily.timer` 18:05），默认 `daban` + Top5 + 阈值0.7；大仓位 `vpnl_huber_stage1` + Top20
- [x] **方案 E**：回测 + 归档链路落地（`plan-e-journal.timer` 18:10）；**实盘入口已于 10-24 退出**，monitor/weekly/real-refresh 停用
- [x] **daban 试验仓**：盘中委托单自动化（`daban-ticket.timer` 已 enabled+active，产出 `data/daban/tickets/`）
- [x] **监控栈**：prometheus / alertmanager / grafana / pushplus 桥（系统级）已 active；`OnFailure` 失败推送链已接
- [x] **方案 F（行业/题材）**：`akshare_industry_loader.py`（申万 335 三级）+ `akshare_concept_loader.py`（THS 375 板块列表）已落地；⚠️ THS 概念**仅名称+代码列表**，成分股明细需另接 Eastmoney `stock_board_concept_cons_em`（不同后端，未纳入）
- [x] **v7 部署到 Ollama**：已完成，v7r 续训作为研究端点（**非生产预测器**）
- [ ] **`adj_factor_ext/` 回填**（10-26 代码已落地，**0 文件**）：后复权序列未生成，任何依赖后复权的下游结论目前**未验证**
- [ ] **试验仓持仓回流**：`daban_paper.py` 尚未与委托单成交闭环，"下单建议 → 实际成交 → 归因"不可审计
- [ ] **执行研究线代码治理**：重复实现抽取（`net_return`/`_auc`/`load_universe`/markdown 助手）、硬编码 `20261022` 参数化、`tdxfeed/daban/` 缺 logging、研究脚本 84 处 `except Exception` 吞错、超大函数拆分（`build_report` 265 行等）—— 清单见 `docs/DABAN_EXECUTION_RESEARCH_LINE.md` §6
- [ ] **执行研究线门禁复评**：Stage 1 `gate.passed=false` → Stage 2 仅诊断；**若要复评必须先满足 precision≥0.80 且 test AUC 明显高于 0.5**，否则维持 `overall_go=false`
- [ ] **`daban-ticket` 运行边界确认**：午休 11:30-13:00 是否产单、尾盘 14:00 后是否覆盖
- [ ] **v8 LoRA fusion**：rank 不兼容（v5=8, v6=16）—— **LLM 线已冻结，不建议再做**
- [ ] **PnL Huber 后继模型**：替换 v8 生产环境（LLM 线冻结后优先级下降）
- [ ] **路线图**：详见 `docs/ROADMAP_2026-10-16.md`——① v7r→v9 量化迭代（**随 LLM 线止损关闭**）② 方案 D→E 信号联调 ③ docs 治理（✅ 已完成）

---

### 7.5 部署形态说明（2026-10-26 复核）

- **生产形态 = `venv` 虚拟环境 + 用户级 systemd timer**（唯一实际运行方式）：`tdxfeed-sync` → `predict-daily` 顺序编排（predict 对 sync 是 `Requires=`，同步失败就不出榜），`canary-health` 每 5min 巡检；方案 E 那条 `→ plan-e-journal` 下游链路 2026-10-24 上午整批 disable、当晚 `plan-e-today` + `plan-e-journal` 重开（只恢复自动算研究清单，不恢复建仓入口）。同一批（2026-10-24）给 `tdxfeed-sync` / `predict-daily` / `canary-health` 挂了 `OnFailure=onfailure-alert@<单元>.service` → `scripts/onfailure_alert.py`，把「单元挂了」变成微信推送；此前这些失败只落在 `logs/*.err` 里。
- **数学服务是例外**：独立 `venv-math`（antlr4 4.11 ↔ omegaconf 4.9.* 死锁），`make verify-math` 校验，**不要在主 venv 跑**。
- **`docker-compose.yml` 是目标架构（未实际部署）**：仓库里的 compose 栈（Ollama/PostgreSQL/Redis/Prometheus/Grafana + exporters）当前**未启用**；实际跑的监控栈见下条，是宿主机上的系统级 systemd 单元，不是容器。
- **监控栈已在跑（2026-10-24 实测，推翻 2026-10-16 的「维持未部署」）**：`prometheus` / `alertmanager` / `grafana-server` / `pushplus_bridge` 四个**系统级**单元均 active+enabled，2026-10-22 20:40~20:41 起；Prometheus 5 个抓取目标全 up（`prometheus`/`alertmanager`/`node`/`canary`/`math-sympy-http`），当前 0 条 firing 规则；Grafana 在 :3000。当时「不部署」的理由（GPU 被 Ollama 占满、内存紧）没有复查记录。
- **告警桥有两个同名东西，别搞混（2026-10-24 核实）**：在用的是系统级 `/etc/systemd/system/pushplus_bridge.service`（以 jiuben 身份跑 `v5/scripts/pushplus_bridge.py`，:5002，`/health` 返回 token 已配置）；2026-10-16 那次「停用」的是仓库里 `systemd/pushplus-bridge.service` 这个 **user 级**单元，至今 disabled——`docs/pushplus_bridge_status_2026-10-16.md` 里「端口 :5002 关闭」只对那次操作负责，现在 :5002 是通的。`onfailure-alert@` 和 Alertmanager 都往这个 :5002 投。
- **Makefile ≠ 生产口径（已对齐）**：`make data-sync` 实为单标的 `tdxfeed.cli sync`（无参默认 `sh600000`，**不会拉全市场**）；全市场增量同步由 `make full-sync`（`tdxfeed.full_sync`，即 `tdxfeed-sync.timer` 每晚 18:00 调用的生产入口）负责。现已在 Makefile 新增 `full-sync` 目标并加注释澄清分工；改调度须同步两边，避免行为漂移。
- **systemd 漂移铁律**：凡改 `systemd/` 下文件，提交后必须 `make timer-install` + `daemon-reload`，否则加载旧文件（被 canary 持续检测告警）。

---

## 8. 变更记录（2026-10-21 ~ 10-26）

| 日期 | 变更 | 影响面 |
|---|---|---|
| 10-21 | **ETF + 北交所数据移除**：脚本下线（`download_etf_bond` / `download_bj_daily` / `download_adj_factor_bj`），`make data-bj` 删除 | 项目口径变为**纯沪深 A 股**；日K/复权/样本数全线变化 |
| 10-21 | MCP / vault / ops 修复 + `feat(daban)` | — |
| 10-22 | `docs(versioning)` | — |
| 10-22 | **监控栈上线**（系统级 prometheus/alertmanager/grafana/pushplus，5 目标全 up，0 firing）| 观测能力从"只有 canary"升级为完整栈 |
| 10-24 | **方案E 实盘入口退出**：monitor/weekly/real-refresh 三只 disable（实盘账本为空）；当晚 today(08:45)+journal(18:10) 重开但**只产研究清单**；新增 `plan-e-double-up`(18:30) | 方案E 定位回到**研究口径** |
| 10-24 | **数学服务迁 `venv-math`**（antlr4 4.11 ↔ omegaconf 4.9.* 死锁）| 新增独立环境 + `make verify-math` 漂移守卫 |
| 10-24 | 新增「回测口径自检」Skill；`--random-timing-trials` 随机择时基线 | 回测结论可引用单一出处 |
| 10-25 | **daban 试验仓委托单**：`daban-ticket.timer` + `daban_entry.py` + `daban_paper.py` + `daban_orderbook_logger.py`（横幅 + 五条自动停止线 + 同日同概念限注 2 笔）| 打板链路新增**盘中产单**环节（仍**不下单**）|
| 10-25 | **LLM 评估入口归档**：`run_v4_eval.py` + `tdx_v9r` 配置移入 `_archive/` 标废弃 | 止损从"决策"落到"代码结构" |
| 10-25 | `OnFailure=` 修复：必须写在 `[Unit]`（写 `[Service]` 被整段忽略）| 失败推送链可靠性 |
| 10-25 | 新浪 L1 34 段字段布局改正（8/9 为成交量/成交额，档位为"量,价"）；主板 ST 涨跌幅按交易日分档（2026-07-06 起与普通股并轨）| 涨停判定口径修正 |
| 10-26 | **腾讯日K 分页深取适配器**（`tdxfeed/history_paging.py`）+ 镜像域 + WAF 501 退避 + volume 单位=手 | 历史深度补齐能力 |
| 10-26 | **日线深度只读审计**（`audit_daily_history_depth.py` + `tdxfeed/symbols.py`）："谁需要回填"变可判分类（免探测 747 只）| 回填从"猜"变"有清单" |
| 10-26 | **幂等回填 runner**（`backfill_daily_history.py` + `tdxfeed/backfill_history.py`）：done 集为续跑单权威 + 连续失败熔断 | 回填可中断可续跑 |
| 10-26 | **后复权因子反推**（`tdxfeed/adj_factor_history.py`）→ `data/adj_factor_ext/`（**代码已落地，0 文件未回填**）| 派生价格在读侧现算，raw 仍是落盘权威 |
| 10-26 | **执行研究线入库**（7 次提交：`e5d60f0`/`c0b37dd`/`c9e2e01`/`3baee07`/`b3f1274`/`1b19086`/`7c341f6`）：核心模块 7 个 + 研究脚本 19 个 + 证据报告 7 份 + 专章 `docs/DABAN_EXECUTION_RESEARCH_LINE.md` | 研究线**只读生产数据、不下单**；结论 Stage1 gate false、Stage2 `overall_go=false` |
| 10-26 | **成交额回填接入生产写路径**（`writers.merge_write` / `incremental_update` → `fill_missing_amount`）；批量回填改**默认 dry-run + 写前备份** | 新写入自动带估算成交额；历史回填需显式 `--apply` |
| 10-26 | **daban 纯工具技能**入库（`skills/daban.yaml` + `mcp:tdx-backtest.daban_top_picks` + 意图路由）| ⚠️ 技能描述与工具 warnings 均标注「只作排序研究、不是买入指令」 |
| 10-26 | pre-commit 新增 `no-zero-byte-files` 钩子；14 处 `zip()` 补 `strict=True`；lint 归零 | 0 字节文件不得入库；`ruff` 在新代码上 0 违规 |

> 详细逐日流水见 `docs/` 各专项报告；当日实况见 `docs/WORKFLOW_SNAPSHOT_2026-10-26.md`。

---

## 9. 一句话总结

**`tdx-data-feed` 是一个 `akshare/Sina/腾讯 清单与行情 → 本地 parquet → 翻倍样本与 29 维特征 → LightGBM 打板信号（方案D）→ 翻倍跟随研究结论（方案E）→ 盘中人工委托单（试验仓）→ 金丝雀+推送告警` 的完整闭环**，主 Agent 通过 execute_command / 文件操作 / 子 Agent（code-explorer）/ MCP（github + 运行时 6 服务）四类原语驱动，主流程靠 systemd user timer，**唯一下单动作保留在人工**；Qwen3-14B LoRA 已于 10-25 归档退出，保留为可解释性对话端点。

> 本文档定位：工程自描述 + 新成员 onboarding 速查表。如有变动，请同步更新本文件（并参考 `docs/WORKFLOW_SNAPSHOT_<date>.md` 留痕）。
