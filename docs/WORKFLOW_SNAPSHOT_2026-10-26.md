# tdx-data-feed 工作流全景（2026-10-26 实况快照）

（2026-10-26 15:40）

> 本文件是 **2026-10-26 的实况快照**，由磁盘实测 + systemd 实况 + git 提交史核对生成。
> 与 `docs/WORKFLOW.md`（2026-10-21 版）并存；二者偏差见 §7，**以本快照为准**。
> 底稿：`docs/WORKFLOW.md`、`.codebuddy/skills/tdx-quant-workflow/SKILL.md`、`systemd/` 单元文件。

---

## 📊 核心指标

| # | 指标 | 实测值（2026-10-26 15:40） | 对比 10-20 |
|---|---|---|---|
| 1 | 生产决策栈 | **LightGBM**（`predict.py` = `lgb.Booster`），非 LLM | 不变 |
| 2 | 运行中定时器 | **10 个**（含 2 个 5min 巡检、1 个 5min 盘中票据） | +1（daban-ticket） |
| 3 | 健康状态 | `canary` **err=0 warn=0**（7 维全绿：ollama/tdxfeed_sync/data_files/disk/timers/gpu/obsidian_sync） | 全绿 |
| 4 | 日 K parquet | **5,236**（5,232 个 `.v1.bak` 备份另计） | 5,235 → 5,236 |
| 5 | 复权因子 | **5,221** | 不变（ETF/BJ 已下线） |
| 6 | 翻倍样本 | **17,582 行**（原 22,972，因 10-21 移除北交所 + 10-24 重算） | ↓（口径变更） |
| 7 | 最新方案 D 预测 | `predict_20261023_183121`（4923 只，≥0.7 共 803，Top1 = 300296 / 0.9714） | — |
| 8 | 最新方案 E 回测 | 112 样本，胜率 33.9%，均收益 +1.16%，CI 跨零 → **四检验无法拒绝"无超额收益"** | 口径细化 |
| 9 | 生产模型 | `d_signal_5d_model_daban.txt`（1.76 MB，10-19）为准；另存 8 个变体/备份 | — |
| 10 | 近 6 天提交量 | **10-21 ~ 10-26 共 58 次提交**（10-26 单日 11 次） | 数据层重构期 |

---

## 🎯 Top 10 关键组件明细

| # | 组件 | 定位 | 触发 | 产物 |
|---|---|---|---|---|
| 1 | `tdxfeed.full_sync` | 全市场行情/复权/财务同步（Sina 源 + akshare 清单） | `tdxfeed-sync.timer` 18:00 | `data/parquet/daily/` + `sh\|sz/lday/` + `data/adj_factor/` |
| 2 | `scripts/predict.py` | **方案 D 生产预测器**（29 维 LightGBM，small=daban/Top5，full=stage1/Top20） | `predict-daily.timer` 18:05（`Requires=` sync） | `data/predictions/predict_*.{csv,md}` |
| 3 | `scripts/daban_entry.py` | **daban 试验仓盘中委托单**（只产文本，不下单） | `daban-ticket.timer` 工作日 09-11/13-14 每 5 min | `data/daban/tickets/` |
| 4 | `scripts/plan_e_backtest.py` | 方案 E 回测（翻倍次日跟随，含成本/滑点/分层/随机择时基线） | 手动 / `make plan-e-compare` | `data/plan_e/plan_e_backtest_v2_*.{csv,md}` |
| 5 | `scripts/find_double_up_c.py` | 翻倍样本重建（方案 E 候选池源头） | `plan-e-double-up.timer` 18:30 | `data/double_up/double_up_2020_2026_detail.csv` |
| 6 | `scripts/plan_e_backtest.py --today` | 方案 E 盘前今日清单 | `plan-e-today.timer` 08:45 | `data/plan_e/plan_e_today.csv` |
| 7 | `scripts/plan_e_journal.py` | 方案 E 归档（**实盘入口已退出**，仅人工记账） | `plan-e-journal.timer` 18:10 | `data/plan_e/live/journal.csv` |
| 8 | `scripts/canary_health_check.py` | 金丝雀巡检（7 维）+ `OnFailure` 推送链 | `canary-health.timer` 5 min | `logs/canary.log` + 微信推送 |
| 9 | `tdxfeed/adj_factor_history.py` | **后复权因子反推**（新，只写 `adj_factor_ext/`） | 手动（回填中） | `data/adj_factor_ext/`（**当前 0 文件**） |
| 10 | `scripts/canary_health_check.py` + `onfailure_alert@.service` | 失败可观测：单元失败 → pushplus 桥（:5002）→ 微信 | 事件驱动 | 推送 |

---

## 1. 系统分层全景

```
┌─── L4 交互/展示层 ────────────────────────────────────────────┐
│  Web UI(--web) │ dashboard.html │ Ollama 端点 │ Obsidian vault │
└─── L3 决策层（生产 · 纯 CPU） ─────────────────────────────────┐
│  方案D  predict.py (LightGBM 29维)  │ 方案E  plan_e_* (规则)  │
│  daban 试验仓：委托单文本（不下单）                            │
└─── L2 数据加工层 ────────────────────────────────────────────┐
│  翻倍样本 find_double_up_c(_v2) │ 特征 features_5d (29维)     │
│  复权 adj_factor + adj_factor_ext(后复权·新建) │ 因子/行业/题材 │
└─── L1 采集层 ────────────────────────────────────────────────┐
│  Sina 日K │ akshare 清单/财务/行业/概念 │ 腾讯日K分页深取(新)   │
│  baostock 复权 │ pytdx 枚举 │ .day 双写                    │
└─── L0 基础设施层 ────────────────────────────────────────────┐
│  venv(py3.14) │ venv-math(独立) │ user systemd timer/path      │
│  prometheus+alertmanager+grafana+pushplus(系统级) │ Git       │
└──────────────────────────────────────────────────────────────┘
```

**双栈并存**：L3 决策层**只有 LightGBM + 规则**（LLM 已在 10-25 归档退出：`run_v4_eval` 与 `tdx_v9r` 配置移入 `_archive` 并标废弃）；L0 仍保留 torch/transformers/peft/llamafactory 依赖与 Ollama 服务（供 `kline-analysis` 技能与 canary 探活）。

---

## 2. 主循环时序（2026-10-26 实况）

| 时刻 | 单元 | 动作 | 依赖 |
|---|---|---|---|
| 03:00（周一） | `tdx-clean-logs.timer` | 清理旧日志 | — |
| 03:30（周一） | `trash-expunge.timer` | 清空回收站 | — |
| **08:45（工作日）** | `plan-e-today.timer` | 方案 E 盘前清单（`--today`）→ `plan_e_today.csv` | 上一交易日数据 |
| **每 5 min** | `canary-health.timer` | 7 维巡检 + `OnFailure` 推送 | — |
| **每 5 min** | `trainsh-health.timer` | 训练进程巡检（无训练则空闲退出） | — |
| **09-11/13-14 每 5 min（工作日）** | `daban-ticket.timer` | **盘中产人工委托单**（每 5 分钟一份） | 实时行情 |
| **18:00** | `tdxfeed-sync.timer` | 全市场同步（Sina/akshare/腾讯） | 网络 |
| **18:05** | `predict-daily.timer` | 方案 D 全市场预测（`Requires=` sync，失败则整轮不跑；含 `precheck_freshness.sh` 新鲜度守卫） | tdxfeed-sync |
| **18:10** | `plan-e-journal.timer` | 归档当日方案 E 信号（**只归档，不建仓**） | predict-daily |
| **18:30** | `plan-e-double-up.timer` | 重建翻倍样本 CSV（零命中/归零则非零退出并保留上一版） | tdxfeed-sync |
| 20:00（周五） | `plan-e-weekly.timer` | **已 disable**（实盘账本为空） | — |
| 18:40 | `plan-e-monitor.timer` | **已 disable**（同上） | — |
| 周日 21:00 | `plan-e-real-refresh.timer` | **已 disable**（从未启用） | — |

> **失败可观测**：2026-10-24 起 `tdxfeed-sync` / `predict-daily` / `canary-health` 挂 `OnFailure=onfailure-alert@<单元>.service` → `scripts/onfailure_alert.py` → pushplus 桥(:5002) → 微信。（10-25 修复：`OnFailure=` 必须写在 `[Unit]`，写 `[Service]` 会被 systemd 整段忽略。）

---

## 3. 阶段详解（输入 → 操作 → 输出 → 责任 → 失败处理）

### 阶段 1 · 数据采集
- **输入**：网络（Sina / akshare / baostock / 腾讯）
- **操作**：`tdxfeed.full_sync` —— akshare 取代码清单 + Sina 拉日K/指数 + 复权因子/财务增量；`merge_write` 双写 `parquet/daily/*.parquet` 与 `sh|sz/lday/*.day`；**腾讯日K 分页深取**（10-26 新增，处理单次条数上限）
- **输出**：`data/parquet/daily/`(5,236) + `data/adj_factor/`(5,221) + `data/parquet/finance.parquet`
- **责任**：tdx-data-collector Agent / 定时器
- **失败**：源站限流/WAF 501 → 深取通道换镜像域 + 退避；断点续传由 `merge_write` 保障

### 阶段 2 · 数据加工（翻倍样本 + 特征）
- **输入**：阶段 1 产物
- **操作**：`find_double_up_c.py`（不复权 v1）/ `find_double_up_c_v2.py`（前复权 v3）；`features_5d.py` 计算 29 维特征（26 基础 + 3 涨停/连板）
- **输出**：`data/double_up/double_up_2020_2026_detail.csv`(17,582)、`d_signal_5d_features.csv`
- **责任**：tdx-data-collector
- **失败**：样本为 0 或某市场段归零 → 非零退出并保留上一版（timer 不产出坏数据）

### 阶段 3 · 模型（生产 LightGBM / 研究 LLM 已归档）
- **A 生产（必需）**：`scripts/train_daban_model.py`（daban 29 维）/ `scripts/find_d_signal_5d.py`（stage1 21 维）
  - 产物：`data/double_up/d_signal_5d_model_daban.txt`（生产默认）、`..._vpnl_huber_stage1.txt`（大仓位）
  - ⚠️ 该脚本历史上无 `--output`，跑训练即覆盖生产模型 → 已加 `--tag` 防覆盖
- **B 研究（已止损 · 10-25 归档）**：`training/config/tdx_v*.yaml` + `run_v4_eval.py` 移入 `_archive` 并标废弃；连续两代 mode collapse，**不重启**
- **失败**：模型文件缺失 → `predict.py` 自动 WARN 回退 stage1（流程不中断）

### 阶段 4 · 每日预测（方案 D）
- **输入**：阶段 1-2 产物 + 生产模型
- **操作**：`predict.py`（默认 small=daban/Top5/阈值0.7；`--position-mode full` = stage1/Top20；`--model`/`TDX_MODEL`/`--position-mode` 三级优先级）
- **输出**：`data/predictions/predict_YYYYMMDD_HHMMSS.{csv,md}` + `predict_top5_*.csv` + `predict_high_*.csv`
- **责任**：tdx-predictor Agent
- **失败**：数据不新鲜 → `precheck_freshness.sh` 拦下；无模型 → 回退 stage1

### 阶段 5 · 方案 E（研究口径）
- **输入**：翻倍样本 + 日K
- **操作**：`plan_e_backtest.py`（含 `--compare/--fee/--slippage/--sensitivity/--stratify/--by-year/--walk-forward/--retune/--random-timing-trials`）；`--today` 产盘前清单
- **输出**：`data/plan_e/plan_e_backtest_v2_*.{csv,md}`、`plan_e_today.csv`、`live/journal.csv`
- **责任**：tdx-backtester Agent
- **失败**：`data/plan_e/` 不可写；`--mode` 非法

### 阶段 6 · 实盘/试验仓（**本单元不下单**）
- **输入**：方案 D 预测 + 实时行情
- **操作**：`scripts/daban_entry.py`（盘中 5 min 轮询，产**人工委托单文本**，带横幅 + 五条自动停止线；同日同概念限注 2 笔）
- **输出**：`data/daban/tickets/`
- **责任**：人工下单（AI 仅出单）
- **边界**：`plan_e_journal --mode buy/sell` 仅记账；`daban-ticket.service` 自述"本单元不下单"

### 阶段 7 · 巡检与告警
- **canary（每 5 min）**：ollama / tdxfeed_sync / data_files / disk / timers / gpu / obsidian_sync 共 7 维 → `err=0 warn=0` 即全绿
- **失败链**：单元失败 → `onfailure-alert@` → pushplus(:5002) → 微信
- **监控栈**（系统级，10-22 起 active）：prometheus / alertmanager / grafana(:3000) / pushplus_bridge，5 个抓取目标全 up，0 firing

---

## 4. 数据层重构进行时（10-26，尚未完成）

| 项 | 状态 | 说明 |
|---|---|---|
| 腾讯日K 分页深取适配器 | ✅ 落地 | 单次条数上限 → 变成可判异常；WAF 501 退避；镜像域 |
| volume 单位修正 | ✅ | 腾讯日K volume=手，去掉多除的 100 |
| 日线深度只读审计 + A股 symbol 口径 | ✅ | 把"谁需要回填"变成**可判分类**（实测免探测 747 只） |
| 幂等回填 runner | ✅ | 续跑单权威 = done 集 + 连续失败熔断 |
| 后复权因子反推 | ⚠️ **代码已落地，尚未回填** | `tdxfeed/adj_factor_history.py` → 只写 `data/adj_factor_ext/`（**当前 0 文件**） |
| 设计文档 | ✅ | raw 仍是落盘权威，派生价格在读侧现算；历史范围由 ipoDate 本地算出 |

> ⚠️ 这条线**不动已冻结的 daily schema**：新数据落在独立目录 `adj_factor_ext/`，属"可空 + 默认值"式扩展，符合 schema v2 双写流程。

---

## 5. Agent / Skill / MCP 分工（当前）

| 层 | 组件 | 职责 |
|---|---|---|
| Agent | `tdx-data-collector` / `tdx-model-trainer` / `tdx-predictor` / `tdx-backtester` | 四大角色的端到端流程封装 |
| Skill | `tdx-quant-workflow` | **全流程编排**：5 阶段命令 + 检查清单 + 失败回滚 |
| Skill | `tdx-obsidian-math-verify` / `tdx-obsidian-version-control` | vault 零漂移 / vault git 化 |
| Skill | `skills/*.yaml` 8 个 | kline-analysis（LLM）/ financial-summary / news-summary / adj-factor / indicator-match / backtest / financial-statement / theory-reference |
| MCP（IDE） | github + codebuddy-integration | PR/Issue/云服务 |
| MCP（运行时） | tdx-parquet-reader / tdx-calc / tdx-vault / tdx-news / tdx-finance-deep / tdx-backtest | 由 `skills/*.yaml` 按需拉起，不注册到 IDE |
| 路由 | `agents/orchestrator.py` | 意图路由（theory-reference 优先判定 → 6 个技能） |

---

## 6. 常用命令速查

```bash
# 生产链路
make full-sync                    # 全市场同步（= tdxfeed-sync.timer 调用的入口）
venv/bin/python scripts/predict.py --top 5 --workers 8      # 方案D 预测（生产默认）
venv/bin/python scripts/plan_e_backtest.py --compare        # 方案E 回测对比
venv/bin/python scripts/daban_entry.py                      # 试验仓委托单（手动出一份）

# 巡检
make canary                       # 7 维健康检查
make status                       # timer 状态
make timer-logs                   # 单元日志
systemctl --user list-timers --all

# 数据与研究
make analyze-double-up            # 重建翻倍样本
make analyze-signal-d             # 方案D 特征/模型
make verify-math                  # math vault 零漂移
make lint && make test            # 静态检查 + 测试

# 注意：make data-sync = 单标的同步（默认 sh600000），不等于全市场
```

---

## 📈 统计分布（本次快照的时序结构）

| 阶段 | 自动化程度 | 人工介入点 | 当前实际运行 |
|---|---|---|---|
| 采集 | 100% 自动 | 仅源站异常 | ✅ 每晚 18:00 |
| 加工 | 100% 自动 | 零命中需判读 | ✅ 18:30 |
| 训练 | 半自动 | 需人工触发 | ⚪ 按需（生产模型已就位） |
| 预测 | 100% 自动 | 异常时人工重跑 | ✅ 18:05 |
| 方案E | 80% 自动 | 参数调优人工 | ✅ 08:45 / 18:10 |
| 试验仓 | 自动产单 | **人工下单** | ✅ 盘中每 5 min |
| 巡检告警 | 100% 自动 | 仅异常 | ✅ 5 min |

> 结论：**决策与产单已全自动化，唯一下单动作保留在人工**（设计如此：`daban-ticket.service` 明确"本单元不下单"）。

---

## ⚠️ 风险提示

1. **历史回测不代表未来收益**：方案 E 最新 112 样本胜率 33.9%、均收益 +1.16%，但 **95% CI 跨零**，四个检验均无法拒绝"无超额收益"；随机择时基线与入场日无异 —— 方案 E 仍是**研究口径**，不构成下单依据。
2. **模型基于 2020-2026 数据训练**，2026 年新模式可能失效；`daban` 为二分类「次日收盘涨停」，天然偏好连板/一字板，已用 T 日开盘即涨停过滤缓解，但打板者仍可能买不进。
3. **翻倍样本口径已变**：10-21 移除北交所 + 10-24 重算，样本从 22,972 → 17,582；与历史报告（引用 22,972）**不可直接比较**。
4. **复权扩展层未落地**：`adj_factor_ext/` 仍是 0 文件，派生后复权价格尚不可用；此时任何依赖后复权序列的下游结论都应视为**未验证**。
5. **严格止损（-10%）与限额纪律**：试验仓委托单内嵌五条自动停止线，突破须人工确认，不应绕过。
6. **调度漂移**：改 `systemd/` 必须 `make timer-install` + `daemon-reload`，否则加载旧文件（canary 会持续告警）。
7. **文档与实况已有偏差**（见 §7），以本快照为准；引用旧数字需注明日期。

---

## 🔧 下一步建议

1. **刷新 `docs/WORKFLOW.md`** 到 10-26 口径（ETF/BJ 下线、实盘入口退出、daban-ticket 支线、复权扩展层、样本数 17,582、方案E 统计检验结论），避免新人按旧文档理解系统。
2. **补齐复权扩展层**：跑 `adj_factor_history` 回填 `adj_factor_ext/`，否则 10-26 的一整轮设计只停在文档层。
3. **明确试验仓的验收口径**：委托单已能自动产出，但**成交与持仓**仍靠人工；建议补 `daban_paper.py` 的持仓回流，让"下单建议 → 实际成交 → 归因"闭环可审计。
4. **确认 `daban-ticket` 的运行边界**：当前工作日 09-11/13-14 每 5 分钟运行，需确认 11:30-13:00 午休不产单、尾盘 14:00 后是否需要覆盖。
5. **保留纪律**：LLM 线已于 10-25 归档退出，不要因为"模型更强"的直觉重启 —— 连续两代 mode collapse 的证据链完整（`docs/V9R_CKPT1000_ASSESSMENT_2026-10-19.md`）。

---

## §7 与 `docs/WORKFLOW.md`（10-21 版）的偏差清单

| # | 文档旧值 | 今日实况 | 影响 |
|---|---|---|---|
| 1 | 日 K 5,235 | **5,236** | 微 |
| 2 | 翻倍样本 22,972 | **17,582** | 🔴 口径变更，引用需注明 |
| 3 | 未记载 `daban-ticket` 支线 | **已启用**，工作日盘中每 5 min 产单 | 🔴 流程新增环 |
| 4 | 未记载 `plan-e-double-up`（18:30） | 已启用 | 🟡 流程新增环 |
| 5 | 方案 E 回测口径 | **112 样本、CI 跨零、四检验无法拒绝无超额收益** | 🔴 结论口径更新 |
| 6 | 未记载复权扩展层 | `adj_factor_ext/` 代码已落地、**0 文件未回填** | 🟡 进行中 |
| 7 | LLM 评估入口在册 | **10-25 已移入 `_archive` 标废弃** | 🟡 印证止损 |
| 8 | venv 单一 | 另有 **`venv-math`** 独立环境（解 antlr4/omegaconf 死锁） | 🟡 依赖面变化 |
| 9 | 未记载监控栈 | 系统级 prometheus/alertmanager/grafana/pushplus **已 active** | 🟢 补全 |

---

## 事实边界与自评

- **实测项**：定时器启用状态与时刻（`systemctl`）、canary 7 维结果、文件计数（`find`）、模型文件清单与时间戳、最新预测/回测报告内容、git 提交史、`daban-ticket` 单元内容、Ollama 模型表。
- **未核实项**：`daban-ticket` 在 09-11/13-14 的实际产单条数与内容（未读取 `data/daban/tickets/` 样本）、盘中行情源可用性、复权回填的实际耗时与失败率。
- **置信度**：调度与数据现状 **高（0.93）**；流程结构描述 **高（0.9）**；方案 E 统计结论 **高（0.95，直接引自最新回测报告）**；"复权扩展层完成度" **中（0.6，仅见代码与 0 文件，未见回填日志）**。

---
生成时间：2026-10-26 15:40:00
生成者：CodeBuddy（工作流实况快照）
数据日期：2026-10-26（磁盘与调度实况快照）
模型版本：daban（LightGBM，生产默认）
