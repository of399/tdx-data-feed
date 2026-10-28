# 工作流整体审查报告 (2026-10-16 14:30)

> **时效提示**：本文写于 2026-10-16；其中 `Writers.write_parquet` / `write_day` 的描述已在 2026-10-28 的写入口收编中失效，现状见 `AGENTS.md` 与 `tdxfeed/parquet_io.py`。其余内容同样是 2026-10-16 的快照，不是现状规格。

> 范围：`tdx-data-feed` 全栈（数据采集 → 量化策略 → 模型训练/部署 → 监控编排）
> 方法：基于 `Makefile`、`config.yaml`、`requirements.txt`、`systemd/*`、`tdxfeed/*`、`docker-compose.yml`、`scripts/*` 实际内容的重新审查（非沿用旧 `docs/WORKFLOW.md`）
> 生成者：CodeBuddy（架构审查）· 文档版本：review-2026-10-16

---

## 1. 一句话定位

`tdx-data-feed` 是一个 **A 股数据底座 + 双引擎（LightGBM 翻倍信号 / Qwen3-14B LoRA 量化对话）+ systemd 定时编排** 的本地量化研究平台。日常生产跑在 **`venv` 虚拟环境 + 用户级 systemd timer** 上；`docker-compose.yml` 描述的是一套**尚未实际部署**的容器化（含 Ollama/PG/Redis/Prometheus/Grafana）目标架构。

---

## 2. 软件 / 平台 / 工具 / 依赖分层矩阵

| 层 | 组件 | 角色与在本项目的作用 | 关键文件 / 入口 |
|---|---|---|---|
| **运行时** | Python 3.14 + `venv` | 唯一执行环境（注意：py3.14 无 cp314 的 flash-attn wheel，训练回退 SDPA） | `Makefile` `VENV` |
| **包管理** | `requirements.txt` | 63 行依赖，分 9 类（采集/训练/转换/量化/Ollama/Web/DB/监控/图表/因子） | `requirements.txt` |
| **数据采集** | akshare / baostock / pytdx / Sina / Tencent | 行情与基础数据来源；`tdxfeed/sources/` 统一抽象 `fetch_bars` | `tdxfeed/sources/*.py` |
| **存储** | pandas + pyarrow (parquet) | 日 K / 复权因子 / 财务 落地为 parquet（schema 冻结，不可改列） | `tdxfeed/writers.py` |
| **量化引擎** | LightGBM + scikit-learn + scipy | 方案 D 信号模型（AUC 0.81）、PnL Huber 回归、回测/模拟 | `scripts/predict.py`、`plan_e_backtest.py`、`plan_e_sim_live.py` |
| **因子工程** | polars + akquant + optuna + plotly | 阶段1 因子表达式引擎、特征选择超参优化 | `requirements.txt`（阶段1 段） |
| **LLM 训练** | torch2.11+cu128 / transformers / peft / LLaMA-Factory / bitsandbytes / deepspeed | Qwen3-14B LoRA/QLoRA 微调（4bit nf4, rank8/16） | `training/config/*.yaml`、`v5/scripts/*` |
| **模型转换** | gguf (master 覆盖) + sentencepiece | LoRA merge → GGUF f16 → Ollama | `requirements.txt` 注释 |
| **推理服务** | Ollama (本地 `:11434`) | 部署 `qwen3-14b-tdx-v5/v6/v7r` 对话端点 | `deploy_v5/v6/v7.sh` |
| **编排** | systemd (user) | 5 个 timer + 1 个 path 单元定时驱动 | `systemd/*.timer` `*.service` `*.path` |
| **健康检查** | `canary_health_check.py` | 6 项金丝雀（数据/模型/timer/obsidian 漂移） | `scripts/canary_health_check.py` |
| **知识库同步** | Obsidian + `obsidian-sync.path` | 源↔vault 零漂移监控（数学笔记库） | `systemd/obsidian-sync.*` |
| **监控栈(容器)** | Prometheus + Grafana + exporters + node-exporter | 指标/可视化（仅 docker 栈，当前未启用） | `docker-compose.yml` |
| **CLI 网关** | `Makefile` | 统一开发入口（install/data/analyze/train/deploy/timer/canary） | `Makefile` |
| **子 Agent / 技能** | `agents/` + `skills/` + `.codebuddy/` | 跨会话记忆、项目级 agent、量化技能 | `agents/*.py`、`skills/*.yaml` |

**依赖关键约束**（实测，见 `MEMORY.md`）：
- `antlr4-python3-runtime==4.9.3` 被 LLaMA-Factory 强绑，升 4.11/4.13 会让其崩溃。
- `gguf` PyPI 0.19.0 缺 DFLASH → 须用 llama.cpp master 覆盖 `site-packages/gguf`。
- `flash-attn` 无 cp314 wheel 且无 nvcc → 训练回退 SDPA。

---

## 3. 目录与关键文件地图

```
tdx-data-feed/
├── config.yaml              # output_root / obsidian_vault；多数字段为遗留未接入
├── Makefile                 # 统一 CLI（install/data/train/deploy/timer/canary）
├── requirements.txt         # 9 类依赖
├── docker-compose.yml       # 容器化目标架构（未实际部署）
├── tdxfeed/                 # 数据采集核心包
│   ├── cli.py               # sync/status/vault-doc 子命令（单标的，非全量）
│   ├── full_sync.py         # ★ 实际全量同步入口（systemd 调用）
│   ├── sources/             # Sina/Akshare/Tencent/pytdx/mootdx 适配器
│   ├── writers.py           # 增量双写 parquet + .day（非破坏）
│   ├── fin_parquet.py       # 财务数据增量
│   └── features_5d.py       # 18/26/35 维特征单一数据源
├── scripts/                 # 80+ 入口脚本
│   ├── predict.py           # 方案 D 每日全市场预测（LightGBM）
│   ├── plan_e_backtest.py   # 方案 E 翻倍跟随回测
│   ├── plan_e_journal.py    # 方案 E 实盘/模拟跟踪（journal.csv / journal_sim.csv）
│   ├── plan_e_sim_live.py   # 模拟实盘（720 笔/6 年）
│   ├── akshare_industry_loader.py  # 申万行业分类
│   ├── akshare_concept_loader.py   # ★ 同花顺题材/概念（新增）
│   ├── build_industry_features.py / build_market_features.py
│   ├── train_pnl_proxy.py / train_pnl_huber_*.py  # PnL Huber 系列
│   ├── canary_health_check.py / precheck_freshness.sh
│   └── INDEX.md             # 脚本分类索引
├── training/config/         # 17 个 LLaMA-Factory yaml（v4→v9）
├── v5/                      # 早期 v5 LoRA 工作区（68 脚本/评估/审计/MCP）
├── data/                    # ★ 数据湖
│   ├── all_a_stocks.json    # 5565 只 A 股清单
│   ├── parquet/daily/       # ~8035 日 K（sh/sz/bj 前缀）+ min5/15/60 层
│   ├── adj_factor/          # 5565 复权因子
│   ├── hfq_factor/          # 5221 后复权因子
│   ├── double_up/           # 翻倍样本 + 模型 + 报告
│   ├── predictions/         # 方案 D 每日预测 146 文件
│   ├── plan_e/              # 方案 E 回测/实盘 305 文件
│   ├── industry/ concept/   # ★ 行业/题材映射
│   └── factors/ sentiment/ valuation/  # 因子/情绪/估值
├── models/                  # 合并模型 + GGUF（~28GB/个，不入 git）
├── systemd/                 # ★ 实际生产编排（timer/service/path）
└── .codebuddy/             # agents / skills / memory / automations
```

---

## 4. 端到端数据流（输入 → 输出）

```
┌─ 输入层（外部源）──────────────────────────────────────────────┐
│ Sina HTTP(日K主力) │ akshare(清单/行业/概念/财务) │ baostock(复权) │ pytdx(兜底) │
└───────────────────────────────────┬──────────────────────────┘
                                     │  tdxfeed.full_sync (SinaSource 主, akshare 清单)
                                     ▼
┌─ 存储层（data/，parquet 冻结 schema）─────────────────────────┐
│ parquet/daily/*.parquet │ adj_factor/*.parquet │ all_a_stocks.json │
└───────────────────────────────────┬──────────────────────────┘
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                         ▼
┌─ 量化链路 ─────────┐   ┌─ 行业/题材链路 ──────┐   ┌─ 训练链路 ─────────────┐
│ find_double_up_c   │   │ akshare_industry_   │   │ Qwen3-14B(base)        │
│ (翻倍样本 22972)   │   │ loader / concept_   │   │ → LLaMA-Factory yaml   │
│   ↓                │   │ loader (CSV)       │   │ → LoRA ckpt            │
│ find_d_signal_5d   │   │   ↓                │   │   ↓                   │
│ (LightGBM AUC0.81) │   │ build_*_features   │   │ deploy_*.sh            │
│   ↓                │   │ (行业/市场聚合)     │   │ (merge→GGUF→Modelfile) │
│ predict.py         │   │   ↓                │   │   ↓                   │
│ (每日全市场 Top50) │   │ train_pnl_huber_*  │   │ Ollama 端点            │
│   ↓                │   │ (33/35 维回归)     │   └───────────┬────────────┘
│ plan_e_backtest /  │   └─────────┬──────────┘              │
│ journal / sim_live │             │                         │
└─────────┬──────────┘             │                         │
          └───────────┬────────────┘                         │
                      ▼                                      ▼
┌─ 输出层 ─────────────────────────────────────────────────────┐
│ data/predictions/*.csv|md │ data/plan_e/*.csv|md │          │
│ data/industry|concept/*.csv│ models/*.gguf │ Ollama 推理端点 │
│ DELIVERY.md / docs/ 报告                                    │
└─────────────────────────────────────────────────────────────┘
                      │
                      ▼  (可选, 容器栈)
        Prometheus ← Ollama/PG/Redis exporters ← Grafana 仪表板
```

---

## 5. 调度与编排（实际运行口径，非 Makefile）

**关键发现**：日常生产由 **用户级 systemd** 驱动，命令与 `Makefile` 中的 `make data-sync`（`tdxfeed.cli sync` 单标的）**不一致**。`make` 是开发入口，systemd 才是生产口径。

| Timer | 时间 | 实际 ExecStart（来自 `systemd/*.service`） | 守卫 |
|---|---|---|---|
| `tdxfeed-sync.timer` | 每日（约 18:00） | `venv/bin/python -m tdxfeed.full_sync` | — |
| `predict-daily.timer` | 收盘后（约 18:05/30） | `scripts/predict.py --top 50 --threshold 0.7 --workers 8` | `precheck_freshness.sh` |
| `plan-e-journal.timer` | 约 18:10 | `scripts/plan_e_journal.py --mode daily` | `precheck_freshness.sh` |
| `plan-e-weekly.timer` | 周五 20:00 | `plan_e_journal.py --mode weekly` | — |
| `plan-e-real-refresh.timer` | 周日 21:00 | `plan_e_real_refresh.sh` | — |
| `canary-health.timer` | 每 5 min | `canary_health_check.py` | — |
| `obsidian-sync.path` | 文件变更触发 | vault 源↔部署 零漂移校验 | — |

依赖链：`tdxfeed-sync` → `predict-daily`(After) → `plan-e-journal`(After)，保证「先同步数据、再预测、再归档信号」的顺序。

---

## 6. 四条主链路详解

### 6.1 数据同步链路（`tdxfeed.full_sync`）
- **清单**：`ak.stock_info_a_code_name()`（5562 只，白天可用）优先；pytdx 分页重试兜底；失败回退 `core_stocks` + 指数 + 样例。
- **日 K**：`SinaSource.fetch_bars` 主力（白天含 amount）；`Writers.write_day` + `write_parquet` **增量双写**（amount 取 max、去重 keep last，非破坏）。
- **财务**：`fin_parquet.incremental` 核心 300  finance/xdxr 断点续传。
- **指数**：`download_indices`（通达信主 + Sina 兜底），以 `sh000001` 等前缀写入同目录。

### 6.2 量化策略链路（方案 D + E）
- **方案 D**：`predict.py` 读 parquet → `features_5d.compute_window_features`（18/26/35 维，向后兼容 `model.feature_name()`）→ LightGBM 预测 → `data/predictions/`。
- **方案 E**：`plan_e_backtest.py`（翻倍次日涨停+量比跟随，v2 胜率 50%/均 10.13%）→ `plan_e_journal.py`（实盘 `journal.csv` 与模拟 `journal_sim.csv` 分离）→ `plan_e_real_samples.py` 回灌实盘样本 → `--real` 消费 `real_trades.csv`。
- **PnL Huber 系列**：`train_pnl_proxy.py`（支持 `--objective huber/quantile` + `--loss-weight`）→ `train_pnl_huber_market/industry.py` → 35/33 维模型，Test R² 0.166→0.278。

### 6.3 训练 / 部署链路
- **训练**：`training/scripts/train_lora.py training/config/tdx_vN_*.yaml`（LLaMA-Factory）。实测单卡 QLoRA 峰值 9061MiB/16G 安全、~12.5s/step、`compute_dtype` **不可**写（自动推导，否则启动即 `ValueError`）。
- **部署**：`deploy_v5/v6/v7.sh` = ①LoRA merge(28GB) → ②GGUF f16(29.5GB) → ③Modelfile → ④`ollama create`。生产优选 **v7r（续训）** 端点。
- **约束**：v5(rank8) 与 v6(rank16) shape 不兼容，v8 fusion 未完成。

### 6.4 题材 / 行业数据链路（含本次新增）
- **行业**：`akshare_industry_loader.py`（`stock_industry_clf_hist_sw` + `sw_index_*_info`）→ `data/industry/sw_*.csv`（335 三级，训练覆盖 81.5%）。
- **题材**：`akshare_concept_loader.py`（`stock_board_concept_name_ths`）→ `data/concept/ths_concept_list.csv`（375 板块），含 3 次指数退避(1/4/9s)重试 + 7 天缓存 + 按名称/代码查询/筛选 API。
- **限制**：akshare 1.18.94 的 THS 概念**仅有名称+代码列表接口**；`stock_board_concept_info_ths` 返回板块汇总而非成分股，故**不含成分股明细**（已在 docstring 标注）。

---

## 7. 调用顺序与依赖图（DAG 摘要）

```
[外部行情源] ──full_sync──▶ data/parquet + adj_factor
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
     find_double_up_c      akshare_industry/      Qwen3-14B
     (翻倍样本)            concept_loader         (base)
            │                     │                     │
            ▼                     ▼                     ▼
     find_d_signal_5d      build_*_features      LLaMA-Factory
     (LightGBM)                  │               (LoRA ckpt)
            │                     ▼                     │
            │              train_pnl_huber_*            ▼
            │                     │               deploy_*.sh
            ▼                     ▼                     │
       predict.py ◀──────────────┘               Ollama 端点
            │
            ▼
       plan_e_backtest / journal / sim_live
            │
            ▼
       输出：predictions/ plan_e/ industry/ concept/ models/ Ollama

[定时] tdxfeed-sync → predict-daily → plan-e-journal → (weekly/real-refresh)
[监控] canary-health(5min) + obsidian-sync(path) + (docker: prometheus/grafana)
```

---

## 8. 约束与注意事项（重点）

1. **Makefile ≠ 生产口径**：`make data-sync` 跑的是单标的 `tdxfeed.cli sync`，生产靠 systemd 的 `tdxfeed.full_sync`。改调度须同步两边，否则行为漂移。
2. **docker-compose 未实际部署**：容器栈（PG/Redis/Prometheus/Grafana）目前**未启用**；生产是 venv+systemd。Prometheus/Grafana 监控当下不存在。
3. **数据 schema 冻结**：`parquet/daily` 列名/类型/顺序不可改；`date` 实际是 **int64 YYYYMMDD**（非 date32），读取须先 `int→datetime`，否则解析成 1970。
4. **双写非破坏**：`writers.py` 增量合并（amount 取 max、去重 keep last），禁止删改历史文件。
5. **akshare 后端不稳定**：东财 `stock_zh_a_hist` 被阻断，日 K 须用 Sina 后端；概念成分股 THS 接口缺失。
6. **Python 3.14 陷阱**：`antlr4==4.9.3` 强绑、`gguf` 须 master 覆盖、`flash-attn` 回退 SDPA。
7. **模型版本兼容**：v5(r8)↔v6(r16) LoRA 不可直接融合；v7r 为生产优选；predict.py 用 `model.feature_name()` 动态取列以兼容 18/26/35 维。
8. **大文件不入 git**：GGUF/parquet 仅本地；`reset-data`/`backup` 须谨慎（可能清掉全部数据）。
9. **实盘/模拟隔离**：`plan_e_journal` 的实盘 `journal.csv`(source='real') 与模拟 `journal_sim.csv` 严格分离，回灌只认实盘来源。
10. **systemd 漂移铁律**：凡改 `systemd/` 下文件，提交后必须 `make timer-install` + `daemon-reload`，否则加载旧文件（被 canary 持续检测告警）。

---

## 9. 关键数字（规模与性能）

| 指标 | 值 |
|---|---:|
| A 股清单 | 5,565 只 |
| 日 K parquet | ~8,035（含 sh/sz/bj） |
| 复权因子 | 5,565（前）+ 5,221（后） |
| 翻倍样本（方案 C v3） | 22,972 样本 / 1,942 只 |
| 方案 D Test AUC | 0.8144（阈值 0.7 精度 83.15%） |
| 方案 E v2 胜率 / 均收益 | 50.0% / 10.13% |
| PnL Huber+Market R² | 0.278（较 MSE 0.166 +67%） |
| 题材板块（THS） | 375 个 |
| 申万三级行业 | 335 个（训练覆盖 81.5%） |
| 模型体积 | 合并 ~28GB / GGUF ~29.5GB（各） |
| systemd 单元 | 5 timer + 1 path + 配套 service |

---

## 10. 与 `docs/WORKFLOW.md` 的差异（该文档已过时）

| 项 | 旧文档(2026-09-27) | 当前实际 |
|---|---|---|
| `predict.py` | 标注「缺 predict.py（未上线）」 | **已存在并每日生产运行** |
| 题材/概念 | 方案 F「接口断连待换源」 | **`akshare_concept_loader.py` 已落地（THS 列表）** |
| 行业 | 未接入 | **`akshare_industry_loader.py` + PnL Huber+SW 已训** |
| 部署形态 | 描述为 venv+systemd（模糊） | 确认 venv+systemd 为唯一生产；docker 未启用 |
| 模型 | v5/v6/v7 LoRA | 增加 v7r 生产优选 + PnL Huber 系列 |
| `tdxfeed.cli sync` | 作为主要同步 | 实为 `full_sync`，cli 仅单标的 |

> 建议：将本审查结论回填 `docs/WORKFLOW.md`，或在其顶部加「详见 `docs/WORKFLOW_REVIEW_2026-10-16.md`」指向。

---

## ⚠️ 风险提示

1. 历史回测不代表未来收益；翻倍/跟随策略属激进策略，建议小资金启动。
2. akshare 接口稳定性差，行业/概念数据须依赖本地缓存 + 重试（已实现），接口长期失效会导致特征缺失。
3. 容器化监控栈（Prometheus/Grafana）当前未部署，生产可观测性依赖 `canary` 健康检查与日志，覆盖有限。
4. 北交所（920XXX）流动性低，滑点可能放大；回测已用 `--bj-slippage` 压力测试。
5. 模型基于 2020–2026 数据训练，2026 年新模式可能失效，需持续重训与验证。

---

## 🔧 下一步建议

1. **回填文档**：用本报告更新 `docs/WORKFLOW.md`，消除 7 处过时陈述。
2. **对齐 Makefile 与 systemd**：将 `make data-sync` 指向 `full_sync`，或加注释明确二者分工。
3. **补齐题材成分股**（如需个股→概念归属）：接东方财富 `stock_board_concept_cons_em`（不同后端，独立于 THS 模块）。
4. **启用监控栈**（可选）：若需 Prometheus/Grafana，先验证 docker 资源（GPU/内存）再部署，避免与本地 Ollama 争抢。
5. **v7r 生产化**：将 PnL Huber+akshare SW 作为 v9 后继模型替换 v8 生产环境（记忆中已列为后续工作）。

---
生成时间：2026-10-16 14:30:00
生成者：CodeBuddy（架构审查）
文档版本：review-2026-10-16
数据日期：2026-10-16（基于 workspace 实际文件与配置）
