# 工作流整体审查报告 v2 (2026-10-16 16:00)

> **时效提示**：本文写于 2026-10-16；其中 `Writers.write_parquet` / `write_day` 的描述已在 2026-10-28 的写入口收编中失效，现状见 `AGENTS.md` 与 `tdxfeed/parquet_io.py`。其余内容同样是 2026-10-16 的快照，不是现状规格。

> 范围：`tdx-data-feed` 全栈（数据采集 → 量化策略 → 模型训练/部署 → 监控编排）
> 方法：基于 `Makefile` / `config.yaml` / `requirements.txt` / `systemd/*` / `tdxfeed/*` / `deploy/*` / `scripts/*` / `.codebuddy/` 实际内容的**第二轮**重新审查。
> 相对 v1 (`docs/WORKFLOW_REVIEW_2026-10-16.md`) 的增量：3 个新 timer + 1 个新 service、1 个新后端脚本 (`tdx_concept_loader`)、Makefile `full-sync` 目标、WORKFLOW.md 已回填、监控栈资源复核完成。
> 生成者：CodeBuddy（架构审查）· 文档版本：review-2026-10-16-v2

---

## 0. 阅读路径

| 章节 | 用途 |
|---|---|
| §1 一句话定位 | 心智模型 |
| §2 角色矩阵 | 软件/工具/依赖分层与职责 |
| §3 文件地图 | 关键文件与目录布局 |
| §4 端到端数据流 | 输入→输出 ASCII 全景 |
| §5 调度与编排 | 10 timer + 1 path + 1 service 的实际运行口径 |
| §6 链路详解 | 5 条主链路的步骤、调用顺序、约束 |
| §7 DAG | 任务依赖与并行/串行 |
| §8 约束与注意 | 12 条关键陷阱 |
| §9 关键数字 | 规模/性能速查 |
| §10 v1 → v2 增量 | 本次审查相对 v1 的变更 |
| §11 风险提示 | 模型/数据/资金 |
| §12 下一步建议 | 可执行优化项 |
| 附录 A | Makefile 目标分类 / 附录 B systemd 完整清单 |

---

## 1. 一句话定位

`tdx-data-feed` 是一个 **A 股数据底座 + 双引擎（LightGBM 翻倍信号 / Qwen3-14B LoRA 量化对话）+ 双题材后端（THS 概念列表 / 通达信个股→概念）+ systemd 定时编排** 的本地量化研究平台。日常生产跑在 **`venv` 虚拟环境 + 用户级 systemd timer** 上；`docker-compose.yml` 描述的是一套**尚未实际部署**的容器化目标架构（含 Ollama/PG/Redis/Prometheus/Grafana，6 服务）。

---

## 2. 软件 / 平台 / 工具 / 依赖分层矩阵

| 层 | 组件 | 角色与在本项目的作用 | 关键文件 / 入口 |
|---|---|---|---|
| **运行时** | Python 3.14 + `venv` | 唯一执行环境；py3.14 无 cp314 的 flash-attn wheel，训练回退 SDPA | `Makefile` `VENV` |
| **包管理** | `requirements.txt` | 9 类共 30+ 依赖（采集/训练/转换/量化/Ollama/Web/DB/监控/因子） | `requirements.txt` |
| **运行配置** | `config.yaml` | 仅 `output_root` + `obsidian_vault` 被实际读取；其余字段为遗留/未接入 | `config.yaml` + `tdxfeed/cli.py` |
| **数据采集** | akshare / baostock / pytdx / Sina / Tencent | 行情/财务/复权多源；`tdxfeed/sources/` 统一抽象 `fetch_bars` | `tdxfeed/sources/*.py` |
| **存储** | pandas + pyarrow (parquet) | 日 K / 复权因子 / 财务 落地为 parquet（schema 冻结，date 实为 int64 YYYYMMDD） | `tdxfeed/writers.py` |
| **题材/行业** | **双后端**（详见 §6.4）| THS（akshare 概念列表）+ 通达信（个股→概念归属） | `scripts/akshare_concept_loader.py` + `scripts/tdx_concept_loader.py` |
| **量化引擎** | LightGBM + scikit-learn + scipy | 方案 D 信号模型（AUC 0.81）、PnL Huber 回归、回测/模拟 | `scripts/predict.py`、`plan_e_backtest.py` |
| **因子工程** | polars + akquant + optuna + plotly | 阶段1 因子表达式引擎、特征选择超参优化 | `requirements.txt` 阶段1 段 + `tdxfeed/factors_polars.py` |
| **LLM 训练** | torch2.11+cu128 / transformers / peft / LLaMA-Factory / bitsandbytes / deepspeed | Qwen3-14B LoRA/QLoRA 微调（4bit nf4, rank8/16）；`compute_dtype` 不可写 | `training/config/*.yaml`、`v5/scripts/*` |
| **模型转换** | gguf (master 覆盖) + sentencepiece | LoRA merge → GGUF f16 → Ollama | `requirements.txt` 注释、`v5/scripts/deploy_*.sh` |
| **推理服务** | Ollama (本地 `:11434`) | 部署 `qwen3-14b-tdx-v5/v6/v7r` 等端点；生产优选 **v7r** | `v5/scripts/deploy_*.sh` |
| **编排** | systemd (user) | **10 timer + 1 path + 1 simple service** 驱动 | `systemd/*` |
| **健康检查** | `canary_health_check.py` | 6 项金丝雀（数据/模型/timer/obsidian 漂移） | `scripts/canary_health_check.py` |
| **告警桥** | `pushplus-bridge` | PushPlus HTTP 网桥（监听 :5002，转发到微信） | `systemd/pushplus-bridge.service`、`v5/scripts/pushplus_bridge.py` |
| **知识库同步** | Obsidian + `obsidian-sync.path` | 源↔vault 零漂移监控（数学笔记库 + 项目库） | `systemd/obsidian-sync.*` + `scripts/sync-to-obsidian.sh` |
| **日志/归档** | `tdx-clean-logs` + `archive-obsidian-annual` | 自动清理 7 天前日志；Obsidian 年度报告归档 | `systemd/tdx-clean-logs.*` + `archive-obsidian-annual.*` |
| **监控栈(容器)** | Prometheus + Grafana + exporters | 指标/可视化（仅 docker 栈，当前**未部署**——资源复核 GPU~96%/RAM 紧张） | `docker-compose.yml` + `deploy/{prometheus,grafana}/` |
| **CLI 网关** | `Makefile` | 统一开发入口（install/data-sync/full-sync/train/deploy/timer/canary） | `Makefile` |
| **子 Agent / 技能** | `agents/` + `skills/` + `.codebuddy/` | 跨会话记忆（每日 + MEMORY.md）、项目级 agent、量化技能 | `.codebuddy/{agents,skills,memory,automations}/` |

**依赖关键约束**（实测，见 `MEMORY.md`）：
- `antlr4-python3-runtime==4.9.3` 被 LLaMA-Factory 强绑，升 4.11/4.13 会让其崩溃。
- `gguf` PyPI 0.19.0 缺 DFLASH → 须用 llama.cpp master 覆盖 `site-packages/gguf`。
- `flash-attn` 无 cp314 wheel 且无 nvcc → 训练回退 SDPA。
- `torchvision` ABI 错配 → `pip install --force-reinstall --no-deps torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128` 修复。

---

## 3. 目录与关键文件地图

```
tdx-data-feed/
├── config.yaml              # output_root / obsidian_vault 实际读取；其余遗留
├── Makefile                 # 统一 CLI（data-sync 单标的 / full-sync 全量 / train / deploy / timer / canary）
├── requirements.txt         # 9 类依赖
├── docker-compose.yml       # 容器化目标架构（6 服务，未部署）
├── Dockerfile
├── tdxfeed/                 # 数据采集核心包
│   ├── cli.py               # sync / status / vault-doc / full / verify（sync 为单标的）
│   ├── full_sync.py         # ★ 实际全量同步入口（systemd 调用）
│   ├── sources/             # Sina/Akshare/Tencent/pytdx/mootdx 适配器
│   ├── writers.py           # 增量双写 parquet + .day（非破坏）
│   ├── fin_parquet.py       # 财务数据增量
│   ├── features_5d.py       # 18/26/35 维特征单一数据源（predict.py 动态取列）
│   ├── factors_polars.py    # 阶段1 因子迁移（21 维 Polars）
│   └── stage1_wide_table.py # 阶段1 宽表
├── scripts/                 # 90+ 入口脚本（INDEX.md 分类 8 段）
│   ├── predict.py           # 方案 D 每日全市场预测（LightGBM，含 --web Gradio UI）
│   ├── plan_e_backtest.py   # 方案 E v1/v2 翻倍跟随回测
│   ├── plan_e_journal.py    # 方案 E 实盘/模拟跟踪（journal.csv / journal_sim.csv 分离）
│   ├── plan_e_v3_early_backtest.py / plan_e_v3_live_track.py  # v3 早盘
│   ├── monitor_plan_e_live.py  # ★ 真实 PnL 跟踪（plan-e-monitor 调用）
│   ├── train_pnl_proxy.py / train_pnl_huber_market.py / train_pnl_huber_industry.py
│   ├── akshare_industry_loader.py
│   ├── akshare_concept_loader.py    # ★ 同花顺概念列表
│   ├── tdx_concept_loader.py        # ★ 通达信个股→概念（v2 新增）
│   ├── bench_v567.py                # v5/v6/v7 推理对标（Bleu/Rouge/P95）
│   ├── build_industry_features.py / build_market_features.py
│   ├── canary_health_check.py / precheck_freshness.sh
│   ├── install-obsidian-plugins.sh / install-math-templates.sh
│   ├── archive_obsidian_annual.sh
│   └── INDEX.md             # 8 段（W1 归档索引）
├── training/config/         # 17 个 LLaMA-Factory yaml（v4→v9）
├── v5/                      # 早期 v5 LoRA 工作区（部署/评估/审计/MCP/pushplus_bridge）
├── data/                    # ★ 数据湖
│   ├── all_a_stocks.json    # 5565 只 A 股清单
│   ├── parquet/daily/       # ~8035 日 K（sh/sz/bj 前缀）
│   ├── parquet/min5/15/60/  # 分钟线
│   ├── adj_factor/          # 5565 复权因子
│   ├── hfq_factor/          # 5221 后复权因子
│   ├── double_up/           # 翻倍样本 + 模型 + 报告
│   ├── predictions/         # 方案 D 每日预测 146 文件
│   ├── plan_e/              # 方案 E 回测/实盘 305 文件
│   ├── industry/            # ★ 申万行业（335 三级）
│   ├── concept/             # ★ 概念数据（ths_concept_list.csv 375 + tdx_*.csv 占位）
│   ├── factors/ sentiment/ valuation/  # 因子/情绪/估值
│   └── .state/manifest.json # 增量同步状态
├── models/                  # 合并模型 + GGUF（~28GB/个，不入 git）
├── systemd/                 # ★ 实际生产编排（10 timer + 1 path + 1 simple service）
├── deploy/                  # 容器化子项目（grafana/postgres/prometheus/scripts/ + README）
├── docs/                    # 47 设计文档（WORKFLOW.md / PLAN_E.md / WORKFLOW_REVIEW_*.md）
└── .codebuddy/              # agents / skills / memory(18) / automations / rules / plans
```

---

## 4. 端到端数据流（输入 → 输出）

```
┌─ 输入层（外部源）────────────────────────────────────────────────────┐
│ Sina HTTP(日K主力) │ akshare(清单/行业/概念/财务) │ baostock(复权)  │
│ pytdx(兜底)        │ 通达信 block_*.dat(概念归属,本地,需用户供给)   │
└───────────────────────────────────┬──────────────────────────────────┘
                                     │  tdxfeed.full_sync (SinaSource 主, akshare 清单, 增量双写)
                                     ▼
┌─ 存储层（data/，parquet 冻结 schema, date=int64 YYYYMMDD）────────────┐
│ parquet/daily/*.parquet │ adj_factor/*.parquet │ all_a_stocks.json   │
└───────────────────────────────────┬──────────────────────────────────┘
            ┌────────────────────────┼────────────────────────┬──────────────┐
            ▼                        ▼                         ▼              ▼
┌─ 量化链路 ─────────┐   ┌─ 行业/题材链路 ──────┐   ┌─ 训练链路 ─────────────┐ ┌─ 监控/告警 ──┐
│ find_double_up_c   │   │ akshare_industry_   │   │ Qwen3-14B(base)        │ │ canary-health│
│ (翻倍样本 22972)   │   │ loader (申万 335)    │   │ → LLaMA-Factory yaml   │ │ pushplus-    │
│   ↓                │   │ akshare_concept_     │   │ → LoRA ckpt            │ │ bridge       │
│ find_d_signal_5d   │   │ loader (THS 375)     │   │   ↓                   │ │ obsidian-sync│
│ (LightGBM AUC0.81) │   │ tdx_concept_loader   │   │ deploy_*.sh            │ │ tdx-clean-   │
│   ↓                │   │ (通达信 个股→概念)    │   │ (merge→GGUF→Modelfile)│ │ logs         │
│ predict.py         │   │   ↓                  │   │   ↓                   │ │ plan-e-      │
│ (每日全市场 Top50) │   │ build_*_features     │   │ Ollama 端点            │ │ monitor      │
│   ↓                │   │ (行业/市场聚合)       │   │ (v5/v6/v7/v7r)         │ └──────┬───────┘
│ plan_e_backtest/   │   │   ↓                  │   └───────────┬────────────┘        │
│ journal/sim_live/  │   │ train_pnl_huber_*    │              │                      │
│ monitor_plan_e_live│   │ (33/35 维回归)       │              │                      │
└─────────┬──────────┘   └─────────┬────────────┘             │                      │
          └───────────┬────────────┘                           │                      │
                      ▼                                        ▼                      ▼
┌─ 输出层 ───────────────────────────────────────────────────────────────────────────┐
│ data/predictions/*.csv|md │ data/plan_e/*.csv|md │ data/industry|concept/*.csv    │
│ data/double_up/*.csv|md   │ models/*.gguf       │ Ollama 推理端点                  │
│ DELIVERY.md / docs/ 报告  │ 微信(PushPlus)       │ Obsidian vault 双向同步         │
└────────────────────────────────────────────────────────────────────────────────────┘
                                │  (可选, 容器栈, 当前未部署)
                                ▼
                  Prometheus ← Ollama/PG/Redis exporters ← Grafana 仪表板
                  (deploy/{prometheus,grafana}/, docker compose up -d)
```

---

## 5. 调度与编排（实际生产口径）

**关键发现**：日常生产由 **用户级 systemd** 驱动，`Makefile` 是开发入口；`make data-sync` 仅同步单只 sh600000，`make full-sync` 才是全量（=systemd `tdxfeed-sync.service` 实际跑的入口）。

### 5.1 完整 systemd 清单（v2 校正：共 10 timer + 1 path + 1 simple service）

| 单元 | 触发 | ExecStart | 守卫 / 备注 |
|---|---|---|---|
| `tdxfeed-sync.{timer,service,path}` | 每日约 18:00 / `.state/manual-sync-trigger` | `venv/bin/python -m tdxfeed.full_sync` | `After=network-online.target`；path 仅手动 |
| `predict-daily.{timer,service}` | 收盘后 | `scripts/precheck_freshness.sh` → `predict.py --top 50 --threshold 0.7 --workers 8` | `After=tdxfeed-sync.service`；`ExecStartPre` 新鲜度守卫 |
| `plan-e-journal.{timer,service}` | 每日约 18:10 | `scripts/plan_e_journal.py --mode daily` | `After=predict-daily.service` |
| `plan-e-weekly.{timer,service}` | 周五 20:00 | `plan_e_journal.py --mode weekly` | — |
| `plan-e-monitor.{timer,service}` | **v2 新发现** | `scripts/monitor_plan_e_live.py --emit auto` | 真实 PnL 跟踪与偏差分析 |
| `plan-e-real-refresh.{timer,service}` | 周日 21:00 | `plan_e_real_refresh.sh` | 实盘样本回灌 |
| `canary-health.{timer,service}` | 每 5 min | `scripts/canary_health_check.py --quiet` | `Nice=15` |
| `tdx-clean-logs.{timer,service}` | **v2 新发现** | `/usr/bin/make -C /home/jiuben/tdx-data-feed clean-logs` | `find -mtime +7 -delete`；`Nice=10`；无需 venv |
| `archive-obsidian-annual.{timer,service}` | **v2 新发现** | `/bin/bash scripts/archive_obsidian_annual.sh` | Obsidian 年度报告归档 |
| `obsidian-sync.{path,service}` | 文件变化触发 | vault 源↔部署 零漂移校验 | inotify 路径监视 |
| `pushplus-bridge.service` *(simple, 无 timer)* | **v2 新发现 / 2026-10-16 停用** | `v5/scripts/pushplus_bridge.py --host 127.0.0.1 --port 5002` | `EnvironmentFile=v5/scripts/.pushplus.env`；`Restart=on-failure`；告警 HTTP→微信桥 ⚠️ **零调用方已停用**，详见 `docs/pushplus_bridge_status_2026-10-16.md` |

**关键路径链**：`tdxfeed-sync` → `predict-daily`(`After=`) → `plan-e-journal`(`After=`)；`obsidian-sync.path` 独立；`pushplus-bridge` 常驻；`canary` / `tdx-clean-logs` 周期性；`plan-e-monitor` 跟踪实盘。

### 5.2 systemd 漂移铁律

凡改 `systemd/` 下任何文件，提交后必须 `make timer-install`（cp 到 `~/.config/systemd/user/`）+ `daemon-reload`；对 `.path` 需 `restart <单元>.path` 重建 inotify；对 `start-limit-hit` 需 `reset-failed`。漂移与限流被 `make canary` 持续检测告警。

---

## 6. 主链路详解

### 6.1 数据同步链路（`tdxfeed.full_sync`）
- **入口**：`python -m tdxfeed.full_sync`（systemd `tdxfeed-sync.service` 实际调用）。
- **清单**：`ak.stock_info_a_code_name()`（5562 只，白天可用）优先；pytdx 分页重试兜底；失败回退 `core_stocks` + 指数 + 样例。
- **日 K**：`SinaSource.fetch_bars` 主力（白天含 amount）；`Writers.write_day` + `write_parquet` **增量双写**（amount 取 max、去重 keep last、mergesort 稳定，非破坏）。
- **财务**：`fin_parquet.incremental` 核心 300  finance/xdxr 断点续传。
- **指数**：`download_indices`（通达信主 + Sina 兜底），以 `sh000001` 等前缀写入同目录。
- **Makefile 边界**：`make data-sync` ≠ `make full-sync` ≠ `tdxfeed.full_sync` 入口（前者 `tdxfeed.cli sync` 单标的，后两者等价）。

### 6.2 量化策略链路（方案 D + E + monitor）
- **方案 D**：`predict.py` 读 parquet → `features_5d.compute_window_features`（18/26/35 维，向后兼容 `model.feature_name()`）→ LightGBM 预测 → `data/predictions/predict_YYYYMMDD_HHMMSS.{csv,md}`。默认模型 `vpnl_huber_stage1`（21 维 Polars，OOS Sp 0.3475）。
- **方案 E**：`plan_e_backtest.py`（翻倍次日涨停+量比跟随，v2 胜率 50%/均 10.13%）→ `plan_e_journal.py`（实盘 `journal.csv` 与模拟 `journal_sim.csv` 严格分离）→ `monitor_plan_e_live.py`（真实 PnL 跟踪与偏差，plan-e-monitor 调用）→ `plan_e_real_refresh.sh`（周日 21:00 回灌实盘样本，plan-e-real-refresh 调用）→ `--real` 消费 `real_trades.csv`。
- **v3 早盘**：`plan_e_v3_early_backtest.py` / `plan_e_v3_live_track.py` 早盘信号变体。
- **PnL Huber 系列**：`train_pnl_proxy.py`（`--objective huber/quantile` + `--loss-weight`）→ `train_pnl_huber_market.py`（35 维）→ `train_pnl_huber_industry.py`（33 维，含行业），Test R² 0.166 → 0.278（+67%）。

### 6.3 训练 / 部署链路
- **训练**：`training/scripts/train_lora.py training/config/tdx_vN_*.yaml`（LLaMA-Factory 0.9.5）。实测单卡 QLoRA 峰值 9061MiB/16G 安全、~12.5s/step；`compute_dtype` **不可写**（自动推导，否则 `ValueError`）；`do_train:true` 必填（漏写静默 exit 0）。
- **续训坑**：从 checkpoint resume 在 0/6006 崩（`pythonInterface.cpp` CUDA 非法参数）→ 移走 `optimizer.pt/scheduler.pt/rng_state_*.pth` 保留 `adapter_model.safetensors` 全新优化器续训。
- **部署**：`v5/scripts/deploy_v5/v6/v7.sh` = ①LoRA merge(28GB) → ②GGUF f16(29.5GB) → ③Modelfile → ④`ollama create`。生产优选 **v7r**（v5 续训 rank16/alpha32）端点。
- **v8 fusion 未完成**：v5(rank8) 与 v6(rank16) shape 不兼容。
- **v9 后继**（已记 §7.4 待办）：PnL Huber + akshare SW 组合替换 v8。

### 6.4 题材 / 行业数据链路（双后端）
- **行业**：`akshare_industry_loader.py`（`stock_industry_clf_hist_sw` + `sw_index_*_info`）→ `data/industry/sw_*.csv`（335 三级，训练覆盖 81.5%）。
- **题材后端 A — THS**：`akshare_concept_loader.py`（`stock_board_concept_name_ths`）→ `data/concept/ths_concept_list.csv`（375 板块），含 3 次指数退避(1/4/9s)重试 + 7 天缓存 + 按名称/代码查询/筛选 API。
  - ⚠️ **THS 限制**：akshare 1.18.94 的 THS 概念**仅有名称+代码列表接口**；`stock_board_concept_info_ths` 返回板块汇总而非成分股，故**不含成分股明细**。
- **题材后端 B — 通达信（v2 新增）**：`tdx_concept_loader.py` 解析 `block_gn/hy/fg/zs/wg.dat` → `data/concept/tdx_{gn,hy,...}.csv`（长表 `code,concept_name,concept_type`），独立于 THS。
  - ⚠️ **通达信后端约束**：本机两个通达信实例(`tdxcfv`)均**无标准 `block_gn.dat` 概念分类文件**——`hq_cache/*block.dat` 为专有格式（pytdx 解析失败）。后端已用合成标准格式文件验证解析/查询逻辑正确，**真实数据需用户将 `block_gn.dat` 放到 `$TDX_BLOCK_DIR` 或通达信 `T0002/block/` 后再 `fetch` 即可生效**。
- **CLI**（两个 loader）：`fetch` / `list` / `query` / `search`；THS 还支持 `--force` 强制刷新。

### 6.5 监控 / 告警 / 维护 链路
- **`canary-health`**：6 项金丝雀（数据/模型/timer/obsidian 漂移），每 5 min，结果 `logs/canary.{out,err}`。
- **`pushplus-bridge`**：常驻 HTTP 桥（:5002），Alertmanager/canary 等告警可经此转微信。token 走 `v5/scripts/.pushplus.env`（轮换后 `restart pushplus-bridge.service` 即生效）。
- **`obsidian-sync.path`**：源↔vault 零漂移监控（数学笔记库 + 项目库），文件变化 inotify 触发校验。
- **`tdx-clean-logs`**：每日清理 7 天前日志（`make clean-logs`）。
- **`archive-obsidian-annual`**：年度 Obsidian 报告归档到 `08-报告/archive/YYYY.zip`。
- **`plan-e-monitor`**：真实 PnL 跟踪（`monitor_plan_e_live.py --emit auto`），实盘运行偏差。
- **监控栈(容器)**：Prometheus/Grafana — **未部署**。资源复核：2×RTX 5060Ti(16G) 已被 Ollama 占满(~96%)，宿主机空闲内存仅 1.4G（13G 可回收缓存）。按"避免与本地 Ollama 争抢资源"约束维持未部署；docker 29.7.2 已就绪。

---

## 7. DAG（任务依赖图）

```
[外部行情源] ──full_sync──▶ data/parquet + adj_factor + .state/manifest.json
                                      │
            ┌─────────────────────────┼─────────────────────────┬──────────────┐
            ▼                         ▼                         ▼              ▼
     find_double_up_c        akshare_industry_loader     Qwen3-14B (base)   监控/告警
     (翻倍样本 22972)        akshare_concept_loader       LLaMA-Factory     canary(5m)
            │                tdx_concept_loader           (LoRA ckpt)       pushplus-bridge
            ▼                         │                         │          obsidian-sync
     find_d_signal_5d                 │                         │          tdx-clean-logs
     (LightGBM AUC0.81)               │                         │          archive-obsidian-annual
            │                         │                         │          plan-e-monitor
            │                         │                         ▼          [systemd 漂移铁律]
            │                         │                  deploy_*.sh       modify systemd/ → make timer-install
            │                         │                  (merge→GGUF)        + daemon-reload
            │                         │                         │            + restart .path
            │                         │                         │            + reset-failed
            │                         │                         ▼
            │                         │                  Ollama 端点
            │                         │                  (v5/v6/v7/v7r)
            │                         │                         │
            │                         ▼                         │
            │                  build_*_features                │
            │                  train_pnl_huber_*               │
            │                  (阶段1 Polars 21 维)            │
            │                         │                         │
            │                         │                         │
            ▼                         │                         │
       predict.py ◀───────────────────┴─────────────────────────┘
       (default: vpnl_huber_stage1)
            │
            ▼
       plan_e_backtest ─── journal (实盘+模拟分离) ─── monitor_plan_e_live
                                  │
                                  ├──→ plan_e_real_refresh (周日 21:00 回灌)
                                  └──→ real_trades.csv

[定时] tdxfeed-sync → predict-daily(After) → plan-e-journal(After)
       plan-e-weekly(周五20:00)  plan-e-monitor(实盘跟踪)  plan-e-real-refresh(周日21:00)
       canary-health(5min)  tdx-clean-logs(每日)  archive-obsidian-annual(年度)
[路径] obsidian-sync.path (vault 文件变化 inotify)
[常驻] pushplus-bridge.service (HTTP 告警桥)
[可选/未部署] Prometheus ← Ollama/PG/Redis exporters ← Grafana (deploy/{prometheus,grafana}/)
```

---

## 8. 约束与注意事项（重点）

1. **Makefile ≠ 生产口径（已对齐）**：`make data-sync` = 单标的 `tdxfeed.cli sync`（无参默认 `sh600000`）；`make full-sync` = `tdxfeed.full_sync` = `tdxfeed-sync.service` 实际入口。已在 Makefile 顶部 + `data-sync` 处加注释澄清。
2. **docker-compose 未部署**：6 服务栈（tdx-app/ollama/postgres/redis/prometheus/grafana）**未启用**；生产是 venv+systemd。资源复核 GPU~96%/RAM 紧张维持未部署。
3. **数据 schema 冻结**：`parquet/daily` 列名/类型/顺序不可改；`date` 实际是 **int64 YYYYMMDD**（非 date32），读取须先 `int→datetime`，否则解析成 1970。变更须走 `tdx-data-schema-v2.md` 流程。
4. **双写非破坏**：`writers.py` 增量合并（amount 取 max、去重 keep last、mergesort 稳定），禁止删改历史文件。
5. **akshare 后端不稳定**：东财 `stock_zh_a_hist` 被阻断，日 K 须用 Sina 后端；THS 概念无成分股明细（限制在 docstring）。
6. **通达信后端数据依赖**：本机 `tdxcfv` 不含标准 `block_gn.dat`，需用户提供方可填充；后端本身已验证可用。
7. **Python 3.14 陷阱**：`antlr4==4.9.3` 强绑、`gguf` 须 master 覆盖、`flash-attn` 回退 SDPA、`torchvision` ABI 错配须 `pip install --force-reinstall --no-deps torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128`。
8. **模型版本兼容**：v5(r8)↔v6(r16) LoRA 不可直接融合；v7r 为生产优选；`predict.py` 用 `model.feature_name()` 动态取列以兼容 18/26/35 维。
9. **大文件不入 git**：GGUF/parquet 仅本地；`reset-data`/`backup` 须谨慎。
10. **实盘/模拟隔离**：`plan_e_journal` 实盘 `journal.csv`(source='real') 与模拟 `journal_sim.csv` 严格分离；`--real` 仅消费实盘来源。
11. **systemd 漂移铁律**：见 §5.2。
12. **宿主机疑似对 ML 长任务发 SIGTERM**（根因未明，非 OOM）：后台长任务务必 `setsid`+`nohup`+`disown` 完全 detach；`llm_signal_plan_e.py gen` 支持 `--resume` 断点续跑。

---

## 9. 关键数字（规模与性能）

| 指标 | 值 |
|---|---:|
| A 股清单 | 5,565 只 |
| 日 K parquet | ~8,035（含 sh/sz/bj） |
| 复权因子 | 5,565（前）+ 5,221（后） |
| 分钟线 | min5/min15/min60（market-60d + core-1y） |
| 翻倍样本（方案 C v3） | 22,972 样本 / 1,942 只 |
| 方案 D Test AUC | 0.8144（阈值 0.7 精度 83.15%） |
| 方案 E v2 胜率 / 均收益 | 50.0% / 10.13% |
| PnL Huber+Market R² | 0.278（较 MSE 0.166 +67%） |
| 阶段1 Polars 模型 | OOS Sp 0.3475（21 维，默认推理） |
| 题材后端 A（THS） | 375 个板块（仅名称+代码） |
| 题材后端 B（通达信） | 模块就绪，本机无标准 block_gn.dat → 真实数据待用户供给 |
| 申万三级行业 | 335 个（训练覆盖 81.5%） |
| Ollama 端点 | v5/v6/v7/v7r/v7r1115/fast（v7r 生产优选） |
| 模型体积 | 合并 ~28GB / GGUF ~29.5GB（各） |
| GPU | 2×RTX 5060Ti 16G（Ollama 占用 ~96%） |
| 宿主机 RAM | 30G，已用 16G，空闲 1.4G（13G 可回收缓存） |
| systemd 单元 | 10 timer + 1 path + 1 simple service |
| docker 栈（未部署） | 6 服务：tdx-app/ollama/postgres/redis/prometheus/grafana |
| 项目脚本 | 90+（scripts/INDEX.md 分 8 段） |
| docs/ | 47 设计文档 |

---

## 10. v1 → v2 增量（本次审查的变更）

| 维度 | v1 (14:30) | v2 (16:00) | 状态 |
|---|---|---|---|
| 题材后端 | 仅 THS 概念列表 | **THS + 通达信（双后端）**，`tdx_concept_loader.py` 上线 | ✅ 新增 |
| Makefile | `data-sync` 与 `full_sync` 模糊 | 新增 `make full-sync` 目标 + 注释澄清 `data-sync` 单标的语义 | ✅ 已对齐 |
| WORKFLOW.md | v1 标注 7 处过时 | 已回填 10 处修正 + 新增 §7.5 部署形态说明 + 监控复核 | ✅ 已完成 |
| 监控栈 | 标"未部署，未复核资源" | 资源复核完成（GPU~96%/RAM 紧张），结论记入 §7.5 | ✅ 已复核 |
| systemd 单元 | 7 timer + 1 path | **10 timer + 1 path + 1 simple service**（v1 漏 3 timer + 1 service） | ✅ 补全 |
| 推送通道 | v1 未提 | 新发现 `pushplus-bridge.service`（HTTP 告警桥） | ✅ 新增 |
| 归档/清理 | v1 未提 | 新发现 `tdx-clean-logs` / `archive-obsidian-annual` | ✅ 新增 |
| 方案 E 监控 | v1 模糊 | 明确 `plan-e-monitor` 跟踪真实 PnL 与偏差（`monitor_plan_e_live.py`） | ✅ 补全 |
| v9 后继 | 记忆后续工作 | 仍在 §7.4 待办，**未完成** | ⏳ 进行中 |
| 数据规模 | 8035/5565/22972/375 | 同上（无新增） | — 持平 |

---

## ⚠️ 风险提示

1. **历史回测不代表未来收益**；翻倍/跟随策略属激进策略，建议小资金启动。
2. **akshare 接口稳定性差**：行业/概念数据须依赖本地缓存 + 重试（已实现），接口长期失效会导致特征缺失。
3. **容器化监控栈（Prometheus/Grafana）当前未部署**：生产可观测性依赖 `canary` 健康检查与日志，覆盖有限。
4. **北交所（920XXX）流动性低**：滑点可能放大；回测已用 `--bj-slippage` 压力测试。
5. **模型基于 2020–2026 数据训练**：2026 年新模式可能失效，需持续重训与验证。
6. **ML 长任务 SIGTERM 风险**：根因未明，后台任务必须 `setsid`+`nohup`+`disown` 完全 detach。
7. **通达信后端需用户提供 block_gn.dat**：否则题材成分股仍仅 THS 列表可查。

---

## 🔧 下一步建议

1. **用户供给 `block_gn.dat`**：导出通达信 PC 端 `T0002/block/block_gn.dat` 等放到项目或设 `TDX_BLOCK_DIR`，跑 `python -m scripts.tdx_concept_loader fetch` 立即生成真实个股→概念归属。
2. **v7r 生产化（v9 后继）**：PnL Huber + akshare SW 组合作为 v9 模型替换 v8（已在 §7.4 待办）；建议先做回测对比 v7r/v8/PnL+SW 三者的 OOS Sp，再决策。
3. **量化方案 D→E 信号联调**：当前两方案独立运行，可做"predict.py Top50 → plan_e_backtest 子集回测 → journal 入库"的端到端联调，验证信号-执行闭环。
4. **告警接入 pushplus-bridge**：canary 与 `monitor_plan_e_live.py` 的 PnL 偏差已具备告警条件，可接到 pushplus-bridge（:5002）转微信。
5. **监控栈按需评估**：当 GPU 资源宽松（如拆出 1 张 5060Ti 给容器）或增加显存时再启 Prometheus/Grafana；当前维持 canary+日志。
6. **docs/ 治理**：WORKFLOW.md 已回填，建议加 `docs/INDEX.md`（或扩展 README）作为文档目录的入口索引，避免再次过时。

---

## 附录 A：Makefile 目标分类（v2）

| 类别 | 目标 |
|---|---|
| 安装/环境 | `install` / `venv` / `setup` / `update` |
| 代码质量 | `lint` / `lint-all` / `lint-pipeline` / `fix` / `fix-unsafe` / `format` / `format-all` / `typecheck` |
| 测试 | `test` / `test-cov` / `test-quick` / `sop-quick` / `sop-full` / `audit` |
| 数据采集 | `data-universe` / **`data-sync`*(单标的)*** / **`full-sync`*(全量,生产)*** / `data-sync-symbol` / `data-adj` / `data-bj` / `data-watch` / `data-status` |
| 量化分析 | `analyze-double-up` / `analyze-double-up-v2` / `analyze-signal-d` / `analyze-top10` / `analyze-dashboard` / `analyze-all` |
| 模型训练 | `train-v5` / `train-v6` / `train-v7` / `train-all` / `train-plot` / `train-monitor` |
| 模型部署 | `deploy-v5` / `deploy-v6` / `deploy-v7` / `deploy-all` / `deploy-list` / `deploy-test` / `deploy-rm` |
| 应用层 | `predict` / `predict-top` / `predict-demo` / `predict-codes` / `plan-e` / `plan-e-v1` / `plan-e-compare` / `plan-e-today` / `journal*` / `simulate` |
| 自动化 | `timer-install` / `timer-enable` / `timer-disable` / `timer-status` / `timer-logs` |
| 监控 | `canary` / `canary-quiet` / `status` / `verify-obsidian*` / `health` / `verify-math` / `precommit-verify` / `logs` / `logs-follow` |
| 维护 | `clean` / `clean-predictions` / `clean-logs` / `backup` / `restore` / `reset-data` |
| 流程 | `pipeline` / `quickstart` / `ci` / `help-*` |

## 附录 B：完整 systemd 单元清单

```
systemd/
├── tdxfeed-sync.{timer,service,path}   ★ 主数据同步（生产入口）
├── predict-daily.{timer,service}       ★ 方案 D 每日预测
├── plan-e-journal.{timer,service}      ★ 方案 E 每日归档
├── plan-e-weekly.{timer,service}         方案 E 周报
├── plan-e-monitor.{timer,service}        方案 E 真实 PnL 跟踪（v2 新增）
├── plan-e-real-refresh.{timer,service}   方案 E 实盘样本回灌
├── canary-health.{timer,service}         金丝雀巡检（5min）
├── tdx-clean-logs.{timer,service}        日志清理（v2 新增）
├── archive-obsidian-annual.{timer,service}  Obsidian 年度归档（v2 新增）
├── obsidian-sync.{path,service}          vault 零漂移监控
└── pushplus-bridge.service               告警 HTTP→微信桥（v2 新增, simple）
```

---

生成时间：2026-10-16 16:00:00
生成者：CodeBuddy（架构审查）
文档版本：review-2026-10-16-v2
数据日期：2026-10-16（基于 workspace 实际文件与配置）
