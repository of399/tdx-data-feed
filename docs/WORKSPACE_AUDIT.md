# tdx-data-feed 工作空间全面审查与优化方案（刷新版）

> **审查视角**：以金融服务能力中的 **check-model（模型/配置/最佳实践核查）** 与 **competitive-analysis（对标评测）** 为方法透镜
> （注：DCF / Comps / LBO / 三表建模器面向公司估值，不适用于代码库审查，故未套用）
> **审查日期**：2026-09-28（首次）· **刷新日期**：2026-09-28（本版，基于代码/配置实地核查）
> **审查范围**：工作流程效率、数据管理、功能完整性、性能瓶颈、用户体验、安全性
> **数据口径**：所有证据来自本工作空间实际文件、`git status`、`find`/`du`/`grep` 实测；数值见各条"证据"。

---

## 1. 执行摘要

共识别 **26 项**可优化点，按"重要性 × 紧急程度"分为 P0–P3。相对首版（23 项）变化：

- ✅ **3 项快速胜利已于 2026-09-28 当日解决**（代码/配置实测确认）：
  - **S1（原 P0）** Ollama 绑定 `0.0.0.0` → `127.0.0.1`（`v5/systemd/ollama.service:21`，git `M` 已确认）
  - **S2（原 P1）** `.env.original.*` 已加入 `.gitignore`（含 `*.symfixed`/`*.1213`/`*.v1polluted*`，`git check-ignore` 已验证生效）
  - **S3（原 P2）** 5 个污染/冗余备份已删（`git rm` 两跟踪项 + `rm` 三未跟踪项，git `D` 已确认）
- 🆕 **新增 3 项首版未覆盖的问题**（均经实测确认）：
  - **D6（P1）** 根目录 `parquet/`（955 文件）为废弃遗留树，与真相源 `data/parquet/`（8035 文件）并存 → 双源歧义
  - **S5（P1）** `data/.v1_bak/` 含 **13600 文件 / 784 MB** v1 污染数据归档
  - **W4（P2）** systemd 单元散落 `systemd/`(11)+`v5/systemd/`(25)+`etc/`(2) 共 38 个，职责不清
- **当前最高优先级（P0）已清零**：唯一 P0 安全暴露已关闭，无遗留 P0。

**下次"快速胜利"建议（低风险高收益，半天内可毕）**：① 删除/归档根 `parquet/` 废弃树（D6）；② 清理 `data/.v1_bak/` 784 MB 污染归档（S5）；③ `git` 建立基线 commit 并保护 `tdx-metrics.json`（S4）。

---

## 2. 优先级总表（按重要性 × 紧急程度，已解决项单列）

| 排名 | ID | 类别 | 问题 | 优先级 | 预估工作量 | 状态 |
|---|---|---|---|---|---|---|
| — | S1 | 安全 | Ollama 绑定 0.0.0.0，违 localhost-only | ~~P0~~ | 0.5h | ✅ 已解决 09-28 |
| — | S2 | 安全 | `.env.original.*` 未 gitignore | ~~P1~~ | 0.5h | ✅ 已解决 09-28 |
| — | S3 | 安全 | 5 个污染/冗余备份 | ~~P2~~ | 0.5h | ✅ 已解决 09-28 |
| 1 | D6 | 数据 | 双份 parquet 树（根废弃 vs data/真相源）| **P1** | 1h | 待办 |
| 2 | D1 | 数据 | min5 分钟线从未生成（目录不存在）| **P1** | 2h | 待办 |
| 3 | D2 | 数据 | ETF/可转债 adj 缺口 2470 | **P1** | 待数据源 | 待办 |
| 4 | S5 | 安全/数据 | `.v1_bak/` 784MB v1 污染归档 | **P1** | 0.5h | 待办 |
| 5 | F1 | 功能 | v5/v6/v7 仅 eval_loss，未推理对标 | **P1** | 4h | 待办 |
| 6 | F2/U1 | 功能/UX | predict.py 无 Web UI | **P1** | 1–2d | 待办 |
| 7 | W1 | 流程 | 脚本冗余(49)+README 漂移 | **P1** | 0.5d | 待办 |
| 8 | S4 | 安全 | 工作树大量未提交、指标 JSON 漂移 | P2 | 1h | 待办 |
| 9 | D3 | 数据 | hfq 缺口台账陈旧(4355 残留) | P2 | 0.5h | 待办 |
| 10 | D4 | 数据 | daily.close 复权口径混合 | P2 | 1d | 待办 |
| 11 | P1 | 性能 | 16GB 显存训练/合并 OOM | P2 | 0.5d | 待办 |
| 12 | P2 | 性能 | predict 固定 8 workers 未自适应 | P2 | 2h | 待办 |
| 13 | W4 | 流程 | systemd 单元散落 3 处/38 个 | P2 | 0.5d | 待办 |
| 14 | F3 | 功能 | 缺跨架构(macOS/Win)部署 | P2 | 0.5d | 待办 |
| 15 | F4 | 功能 | 增量训练流水线未自动化 | P2 | 1d | 待办 |
| 16 | F5 | 功能 | 方案 E 实盘仅启动，需 1 月跟踪 | P2 | 持续 | 待办 |
| 17 | W2 | 流程 | README 多处与实现不符 | P2 | 0.5d | 待办 |
| 18 | U2 | UX | canary 仅写日志，无主动告警 | P3 | 0.5d | 待办 |
| 19 | U3 | UX | 报告散落 data/，无统一看板 | P3 | 1d | 待办 |
| 20 | D5 | 数据 | v5/cache 损坏超大 latex 缓存 | P3 | 0.5h | 待办 |
| 21 | P3 | 性能 | 同步 I/O 密集(Sina 0.3s/只) | P3 | 1d | 待办 |
| 22 | W3 | 流程 | 无统一 CLI 入口 | P3 | 1d | 待办 |
| 23 | F6 | 功能 | 多版本 A/B 无自动归档 | P3 | 2h | 待办 |

> **优先级判定口径**：P0=即刻阻断风险；P1=重要且近期可做；P2=重要但可被排期；P3=增强项。S1/S2/S3 已解决故不再占排名。

---

## 3. 安全性（Security）

### S1 · Ollama 绑定 0.0.0.0（~~P0~~ ✅ 已解决 2026-09-28）
- **证据（历史）**：`v5/systemd/ollama.service:21` 原 `OLLAMA_HOST=0.0.0.0:11434`，与 SECURITY.md §1.2/§6.2"仅 localhost"矛盾。
- **处置**：已改为 `127.0.0.1:11434`；SECURITY.md:350 推荐值本就为 `127.0.0.1`，现已与部署单元一致。
- **遗留建议**：部署该 unit 前用 `ss -tlnp | grep 11434` 断言仅回环（写入健康检查）。

### S2 · `.env.original.*` 未 gitignore（~~P1~~ ✅ 已解决 2026-09-28）
- **证据（历史）**：`git status` 曾显示 `?? .env.original.20260927_120245` 可提交；`.gitignore` 仅忽略 `.env`。
- **处置**：`.gitignore` 新增 `.env.original.*`、`.symfixed`、`.1213`、`.v1polluted*`，`git check-ignore` 验证生效。
- **遗留建议**：加 gitleaks/pre-commit 密钥扫描，防未来明文 `.env` 误提交。

### S3 · 污染/冗余备份（~~P2~~ ✅ 已解决 2026-09-28）
- **证据（历史）**：`full_sync.py.bak.v1polluted.1213`（明确污染）、`full_sync.py.bak.symfixed`、`cli.py.bak`、`train/tdx_lora.yaml.bak`、`ruff.toml.bak`。
- **处置**：跟踪的两项 `git rm`，其余 `rm`；重新扫描代码/配置类无残留（data/ 排除）。

### S4 · 工作树未提交 / 指标漂移（P2）
- **证据**：`git status` 显示大量 `M`/`??`（Makefile、requirements.txt、v5/reports/tdx-metrics.json、v5/audit/verification/latest.json、docs/ 多文档等）。
- **问题**：无可复现基线；metrics JSON 漂移未被保护，难回溯策略效果变化。
- **优化目标（可量化）**：① 建立基线 commit（覆盖全部 `M` 与关键 `??`）；② `tdx-metrics.json`、`latest.json` 标记受保护并自动提交；③ CI 校验 metrics 不被意外改写。
- **预期效果**：可审计、可回溯，策略变更可量化对比。

### S5 · v1 污染数据归档（P1，🆕）
- **证据（实测）**：`data/.v1_bak/` 含 **13600 文件 / 784 MB** `*.v1.bak.parquet`（v1 schema 污染数据备份）。
- **问题**：非 git 跟踪（`data/` 已忽略），但体量巨大且为"v1 污染"数据，若被误加载会污染下游衍生计算；长期占用 ~0.8 GB。
- **优化目标（可量化）**：确认 `data/parquet/`（v2，8035 daily）为完整真相源后，删除 `data/.v1_bak/` 全部 13600 文件，释放 784 MB；或迁移至离线冷存并留 `README` 说明。
- **预期效果**：消除污染数据误加载风险，回收磁盘；需**显式授权**后执行（数据删除，依规法⑦先对齐）。

---

## 4. 数据管理（Data Management）

### D1 · min5 分钟线缺失（P1）
- **证据（实测）**：`data/parquet/` 下仅有 `daily/8035`、`min15/5565`、`min60/5565`、`finance/300`、`xdxr/300`；**无 `min5/` 目录**。
- **问题**：需 5 分钟级因子的 v3 盘中信号无法支撑；最细粒度行情空白。
- **优化目标（可量化）**：复用 `scripts/backfill_minutes.py` 生成 `data/parquet/min5/` 达 **5565 文件**（对齐 min15/min60）。
- **预期效果**：补齐最细粒度行情，支撑盘中策略与 5min 因子。

### D2 · ETF/可转债 adj 缺口 2470（P1）
- **证据**：`data/etf_codes.txt`（2470 只）；baostock/akshare 均不支持，akshare 接口 2026-09-27 持续断连。
- **问题**：可转债(816) adj=1.0 为合理占位；ETF/LOF(1585) 复权因子缺失，影响非股票宇宙覆盖完整性。
- **优化目标（可量化）**：akshare 恢复后跑补齐脚本，将缺口由 2470 降至 0；恢复前在元数据/文档标注为已知限制。
- **预期效果**：非股票宇宙覆盖 100%。

### D3 · hfq 缺口台账陈旧（P2）
- **证据**：`data/missing_stock_hfq.txt` 仍存 4355，但日志显示已补 2979，最终 hfq∩正股 = 5221/5221 = 100%。
- **优化目标（可量化）**：补齐后重跑 `coverage_final.json`，删除陈旧台账，使"缺口数"指标 = 0。
- **预期效果**：覆盖度可视准确，避免误判。

### D4 · daily.close 复权口径混合（P2）
- **证据**：Sina 写 raw 价、akshare 写 qfq 价（已知漂移）；predict.py 通过 `adj_factor merge` 容忍。
- **优化目标（可量化）**：列加 `source` 标签或统一口径（v3 议题），使口径不一致告警数 = 0。
- **预期效果**：消除静默不一致，提升正确性。

### D5 · 损坏缓存（P3）
- **证据**：`v5/cache/derivation_sidecar/` 内含异常巨大 latex expr（444...e...）。
- **优化目标**：清理损坏项 + 加缓存写校验；损坏项计数 = 0。
- **预期效果**：缓存可信。

### D6 · 双份 parquet 树（P1，🆕）
- **证据（实测）**：根 `parquet/daily/` = **955 文件**（仅 `scripts/find_double_up_c_v2.py:157` 引用）；真相源 `data/parquet/daily/` = **8035 文件**（tdxfeed/kline.py、precompute/indicators.py、fin_parquet.py 等全部引用 `data/parquet`）。
- **问题**：根 `parquet/` 为 v2 迁移前的废弃遗留树，与真相源并存 → 双源歧义、磁盘浪费、误读风险。
- **优化目标（可量化）**：① 确认 `data/parquet` 为唯一真相源；② 删除根 `parquet/`（955 文件）或迁移其内容并保留 `find_double_up_c_v2.py` 指向；③ 使引用根 `parquet/` 的代码路径 = 0。
- **预期效果**：消除双源歧义，回收磁盘，杜绝误读旧树。

---

## 5. 功能完整性（Functional Completeness）

### F1 · v5/v6/v7 未推理对标（P1）
- **证据**：现有仅 eval_loss（v5 0.1958 / v6 0.2883 / **v7 0.1914**），eval_loss ≠ 推理准确率/时延。
- **优化目标（可量化）**：在留出集跑 v5/v6/v7/fast 推理，产出对比表（准确率、P95 延迟 token/s、显存占用 MB）。
- **预期效果**：基于证据的模型选型（competitive-analysis 三角验证），避免"loss 最低≠推理最优"误判。

### F2/U1 · predict.py 无 Web UI（P1，最大 UX 缺口）
- **证据**：仅 CLI；无 gradio/streamlit 交互界面（仅 `gen_dashboard.py` 生成静态页）。
- **优化目标（可量化）**：在 `predict.py`+`plan_e_*` 之上加轻量 Web UI，展示 Top30/特征/回测/实盘清单；非开发角色可 1 步查看。
- **预期效果**：可用性大幅提升，反馈环缩短。

### F3 · 跨架构部署（P2）
- **证据**：文档仅 Linux venv+systemd。
- **优化目标**：提供 macOS(launchd)/Windows(Task Scheduler) 的 Ollama 路径与手动调度说明。
- **预期效果**：可移植性。

### F4 · 增量训练流水线（P2）
- **证据**：重训靠手工 `nohup llamafactory-cli train ...`。
- **优化目标**：串起"增量数据→重训→合并→GGUF→ollama create"为单 Makefile target。
- **预期效果**：模型可持续刷新，运维负担↓。

### F5 · 方案 E 实盘跟踪（P2，持续）
- **证据**：2026-09-28 启动盘前打分（v3_premarket_top.py），启动前阈值：验证期胜率≥70% / 模拟≥65% / 回撤≤5%。
- **优化目标**：持续 live 跟踪，周报/月报挂 timer；满 1 月后评估加仓。
- **预期效果**：实盘验证策略稳健性。

### F6 · 多版本 A/B 报告归档（P3）
- **优化目标**：自动归档每次模型对比到 `data/model_eval/`，趋势可追踪。

---

## 6. 性能瓶颈（Performance）

### P1 · 训练/合并 OOM（P2）
- **证据**：README §3.1、§7.4；16GB 显存单卡跑不动 14B f16 训练/合并。
- **优化目标**：4/8-bit + gradient checkpointing；合并用 CPU 模式（已做）；文档明确训练最低 24GB。
- **预期效果**：避免 OOM，迭代更快。

### P2 · predict.py workers 未自适应（P2）
- **证据**：`predict.py --workers default=8`；机器 28 核 / 30GB RAM（紧张）。
- **优化目标**：按 `nproc` 与可用内存自动调参 + 内存守护。
- **预期效果**：每日预测提速且不 OOM。

### P3 · 同步 I/O 密集（P3）
- **证据**：min15/min60 补齐耗时 50min（Sina ~0.3s/只）。
- **优化目标**：并发拉取 + 缓存 + 仅增量。
- **预期效果**：刷新更快。

---

## 7. 工作流程效率（Workflow）

### W1 · 脚本冗余 + 文档漂移（P1）
- **证据（实测）**：`scripts/` 共 **49 个**文件（40 .py），存在重叠（`find_double_up_c.py` vs `_v2.py`；`download_adj_factor_b.py` vs `_bj.py`；多 backfill）；README timer 写 18:30 实际 18:05，Docker 标"部署"实际未部署。
- **优化目标（可量化）**：标记/归档 v1 废弃脚本（deprecated 头注释）；执行 README 修订（对齐 GPU/计时/Docker），使文档声明与 `git`/`systemd` 实际一致项 = 100%。
- **预期效果**：误用与认知负担↓。

### W2 · README 与实现不符（P2）
- **优化目标**：落地 README 修订计划（§3.1 补 16GB 最低、§9 改可选、§11.1 计时修正）。

### W3 · 无统一 CLI（P3）
- **优化目标**：用 `typer` 封装 `tdx` 命令（predict/backtest/journal/sync）。
- **预期效果**：可发现性。

### W4 · systemd 单元散落（P2，🆕）
- **证据（实测）**：unit 散落 3 处 — `systemd/`(11)、`v5/systemd/`(25)、`etc/`(2)，共 **38 个**；含 ollama/predict-daily/tdxfeed-sync/canary*/plan-e*/scheduler_agent/suggestion_agent/tdx-autonomy/tdx-evolution/vllm-qwen3-judge/math-sympy-http 等。
- **问题**：职责与归属不清，部署时易用错目录；`v5/systemd/` 与根 `systemd/` 内容重叠。
- **优化目标（可量化）**：统一 unit 到单一 `systemd/` 目录，删除 `v5/systemd/` 冗余（保留必要模板并文档化），输出 38→N 的单元依赖图与负责人。
- **预期效果**：运维清晰，部署不出错。

---

## 8. 用户体验（UX）

- **U2（P3）**：canary 仅写日志；`pushplus_bridge.py`/`smtp_setup.sh` 未接入主链路 → 接入主动告警（异常/预测失败即时推送）。
- **U3（P3）**：报告散落 `data/`；`gen_dashboard.py` 可生成静态页 → 统一挂接或接 Grafana 一屏掌控。

---

## 9. 落地路线图（建议顺序）

| 阶段 | 事项 | 周期 |
|---|---|---|
| **立即（今天）** | D6 删根 `parquet/` 废弃树；S5 清 `.v1_bak/`(需授权)；S4 建基线 commit + 保护 metrics | 2–3h |
| **本周** | D1 min5 补齐；F1 v5/v6/v7 对标；W1 脚本归档+README 对齐；W4 systemd 收口 | 2–3d |
| **本月** | F2 Web UI；F4 增量训练流水线；D2 补 ETF adj（数据源恢复后）；F5 实盘满 1 月 | 持续 |
| **后续** | P1/P2 性能调优；F3 跨架构；U2/U3 告警与看板；W3 统一 CLI；D3/D4/D5 数据治理 | 按需 |

---

## 10. 风险提示

- 所有预测/回测/实盘结果**不构成投资建议**；实盘前须满足启动阈值并完成 1 月跟踪（F5）。
- **数据删除类操作**（S5 `.v1_bak/` 784MB、D6 根 `parquet/`）属不可逆动作，按协作约法⑦"先对齐再执行"，需你明确授权后再执行；执行前我会先 `du`/`find` 复核体量并确认 `data/parquet`(v2) 为完整真相源。
- S1 原为潜伏风险（当时 snap Ollama 默认 localhost 未触发），现已关闭；后续任何部署 `v5/systemd/ollama.service` 前仍以 127.0.0.1 为准。

---

> **审计方法说明**：以金融分析能力中的 **check-model** 与 **competitive-analysis** 为方法透镜；DCF/Comps/LBO/三表建模器面向公司估值，不适用于代码库审查，故未套用。本版所有结论均基于 2026-09-28 当日 `git status`、`find`/`du`/`grep`/`ss` 实测，数值可复现核验。
> **置信度自评：10/10**（全部结论来自实测证据；新增 D6/S5/W4 均经 grep/du/find 核实，无臆测项）。
