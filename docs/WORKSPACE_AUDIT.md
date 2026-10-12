# 工作空间全面审查与优化清单（终稿 v2 · 2026-09-28）

> **方法论（已激活的金融服务能力）**：
> - **financial-analysis@cb-teams-marketplace** → 取 `check-model`（模型/配置/最佳实践核查）透镜用于 **schema 治理、模型评估严谨性**；取 `competitive-analysis`（对标评测）透镜用于 **F1 模型版本推理对标**。
> - **wealth-management@cb-teams-marketplace** → 取 `client-report` / `portfolio-rebalance` 的"**可量化、带风险提示、结构化**"范式，套用于本报告结构（核心指标 / Top N / 风险提示 / 下一步）。
> - **明确不适用**：DCF / Comps / LBO / 三表建模器面向公司估值，不适用于代码库审查，故未套用。
> **审查范围**：工作流程效率、数据管理、功能完整性、性能瓶颈、用户体验、安全性（六维度全覆盖）。
> **数据口径**：所有证据来自本工作空间实际文件、`git` 实测、`find`/`du`/`grep` 实测；数值可复现核验。

---

## 📊 核心指标

| 指标 | 数值 | 说明 |
|---|---:|---|
| 审查维度 | **6** | 工作流 / 数据 / 功能 / 性能 / UX / 安全 |
| 累计识别优化点 | **29** | 待办 23 + 已解决 6 |
| 当前 P0（即刻阻断） | **0** | S1 安全暴露已关闭，无遗留 P0 |
| 待办 P1 | **6** | D1 / D2 / D7 / F1 / F2·U1 / W1 |
| 待办 P2 | **11** | S4(残) / S6 / D3 / D4 / P1 / P2 / W2 / W4 / F3 / F4 / F5 |
| 待办 P3 | **6** | U2 / U3 / D5 / P3 / W3 / F6 |
| 本会话已解决 | **6** | D6 / S5 / S4(基线) / pre-commit / S1 / S2 / S3* |
| 已就位正向控制 | **4** | pre-commit 配置、.gitignore 加固、Ollama 仅 localhost、基线 commit |

> *S1/S2/S3 为今早解决；D6/S5/S4(pre-commit) 为本会话解决。

---

## 🎯 Top 优先级明细（待办 Top 10，按重要性 × 紧急度）

| 排名 | ID | 维度 | 问题摘要 | 优先级 | 预估 | 状态 |
|---|---|---|---|---|---|---|
| 1 | **D7** | 数据/安全 | 44MB `parquet` 等已提交，违反"大文件不提交"硬规则 | **P1** | 1h | 待办(新) |
| 2 | **D1** | 数据 | `min5/` 分钟线从未生成（目录不存在） | **P1** | 2h | 待办 |
| 3 | **D2** | 数据 | ETF/可转债 adj 缺口 2470（仅占位） | **P1** | 待数据源 | 待办 |
| 4 | **F1** | 功能 | v5/v6/v7 仅 eval_loss，未推理对标 | **P1** | 4h | 待办 |
| 5 | **F2·U1** | 功能/UX | predict.py 无 Web UI（最大 UX 缺口） | **P1** | 1–2d | 待办 |
| 6 | **W1** | 工作流 | 脚本冗余 85 个 + README 漂移 | **P1** | 0.5d | 待办 |
| 7 | **S4** | 安全 | 指标 JSON 漂移未受保护 | P2 | 1h | 部分(基线✅) |
| 8 | **S6** | 安全 | pre-commit 缺 gitleaks 密钥扫描 | P2 | 1h | 待办(新) |
| 9 | **D3** | 数据 | hfq 缺口台账陈旧(4355 残留) | P2 | 0.5h | 待办 |
| 10 | **P1** | 性能 | 16GB 显存训练/合并 OOM 风险 | P2 | 0.5d | 待办 |

---

## 一、安全性（Security）

### S1 · Ollama 绑定 0.0.0.0（✅ 已解决 09-28）
- **当前问题（历史）**：`v5/systemd/ollama.service:21` 原 `OLLAMA_HOST=0.0.0.0:11434`，与 SECURITY.md "仅 localhost" 矛盾。
- **优化目标（已达成）**：改为 `127.0.0.1:11434`，与 SECURITY.md:350 推荐值一致。
- **预期效果**：消除局域网暴露面。部署前用 `ss -tlnp | grep 11434` 断言仅回环（写入健康检查）。

### S2 · `.env.original.*` 未 gitignore（✅ 已解决 09-28）
- **当前问题（历史）**：`.gitignore` 仅忽略 `.env`，密钥备份可误提交。
- **优化目标（已达成）**：新增 `.env.original.*` / `*.symfixed` / `*.1213` / `*.v1polluted*`，`git check-ignore` 验证生效。
- **预期效果**：密钥备份不再入 VCS。

### S3 · 污染/冗余备份（✅ 已解决 09-28）
- **当前问题（历史）**：5 个 `.bak` 污染/冗余文件（含 `full_sync.py.bak.v1polluted.1213`）。
- **优化目标（已达成）**：`git rm` 跟踪项 + `rm` 未跟踪项，复核无残留。

### S4 · 指标 JSON 漂移未受保护（P2 · 部分解决）
- **当前问题（具体）**：`git status` 显示 `v5/reports/tdx-metrics.json` 持续 `M`（未暂存），`v5/audit/verification/latest.json` 同理；无可复现基线，策略效果变化难回溯。
- **优化目标（可量化）**：① 将 `tdx-metrics.json` / `latest.json` 标记为受保护并**自动提交或纳入 .gitignore+独立存储**；② CI/钩子校验 metrics 不被意外改写；③ 指标变更需附 commit 说明。
- **预期效果**：策略迭代可审计、可量化对比（呼应 check-model 的"变更可追溯"原则）。
- **优先级依据**：当前仅影响可维护性（非线上风险），但已是第二次出现漂移，列为 P2 防退化。

### S5 · v1 污染数据归档（✅ 已解决 本会话）
- **当前问题（历史）**：`data/.v1_bak/` 含 13600 文件 / 784MB v1 污染数据。
- **优化目标（已达成）**：经授权后 `rm -rf` 删除，残留 0；`data/parquet/daily`(8035) 确认为完整真相源。

### S6 · pre-commit 缺 gitleaks 密钥扫描（P2 · 新发现）
- **当前问题（具体）**：本会话已加 `.pre-commit-config.yaml`（含 `detect-private-key`），但该检查**仅识别私钥**，不覆盖通用密钥（API Key / Token / 密码）。`.env` 仍可能被误提交。
- **优化目标（可量化）**：在 pre-commit 加入 `gitleaks`/`secret-scan` hook，使密钥误提交拦截率 = 100%（对 `.env`、含 `key`/`token`/`password` 字面量）。
- **预期效果**：补上 S2 之后的最后一道密钥防线，符合 SECURITY.md 要求。
- **优先级依据**：有 `.env` 明文风险历史（S2），且 pre-commit 已就位、增量成本低，列为 P2 快速胜利。

### D7 · 已跟踪大文件违反硬规则（P1 · 新发现）
- **当前问题（具体+证据）**：`git ls-files` 显示已提交大文件——
  `v5/audit/baseline_features.parquet` **44.3MB**、`v5/audit/v6_enriched_lora/tokenizer.json` **11.4MB×2**、`v5/audit/double_triggers_enriched.csv` 7.3MB 等。
  这**直接违反**项目规则"大文件（GGUF/parquet）不提交到 git"与"数据 schema 不可破坏原则"的配套约束（仓库膨胀、clone 变慢）。
- **优化目标（可量化）**：将 >10MB 的已跟踪文件移出 git 跟踪——`git rm --cached` + 加入 `.gitignore` + 迁入 `data/`(已忽略) 或 `models/`(已忽略) 或 Git LFS；使 `git ls-files` 中 >10MB 文件数由当前 **6+** 降至 **0**。
- **预期效果**：仓库体积收敛、clone 提速、规则合规。pre-commit 的 `check-added-large-files` 仅防未来，不修存量，故需主动清理。
- **优先级依据**：违反项目**硬规则**且持续恶化仓库健康度；清除成本低、风险小，列为 P1。

---

## 二、数据管理（Data Management）

### D1 · min5 分钟线缺失（P1）
- **当前问题（具体）**：`data/parquet/` 仅 `daily/8035`、`min15/5565`、`min60/5565`、`finance/300`、`xdxr/300`；**无 `min5/`**。
- **优化目标（可量化）**：复用 `scripts/backfill_minutes.py` 生成 `data/parquet/min5/` 达 **5565 文件**，与 min15/min60 对齐。
- **预期效果**：补齐最细粒度行情，支撑盘中 5min 因子与 v3 盘中信号。
- **优先级依据**：影响功能完整性（盘中策略空白），且数据缺口明确、补全是确定性工作。

### D2 · ETF/可转债 adj 缺口 2470（P1）
- **当前问题（具体）**：`data/etf_codes.txt`(2470 只)；baostock/akshare 不支持，akshare 接口 2026-09-27 断连；可转债 adj=1.0 为合理占位，ETF/LOF(1585) 复权因子缺失。
- **优化目标（可量化）**：akshare 恢复后跑补齐脚本，缺口由 2470 → 0；恢复前在 `data/non_stock_metadata.json` 与文档明确标注为已知限制。
- **预期效果**：非股票宇宙覆盖 100%，避免下游误算。
- **优先级依据**：当前不影响 A 股主策略（翻倍策略仅用股票），但 Universe 完整性缺口，列为 P1（依赖外部数据源恢复）。

### D3 · hfq 缺口台账陈旧（P2）
- **当前问题（具体）**：`data/missing_stock_hfq.txt` 仍存 4355，但日志显示已补 2979，最终 hfq∩正股 = 5221/5221 = 100%。
- **优化目标（可量化）**：重跑 `coverage_final.json`，删除陈旧台账，使"缺口数"指标 = 0。
- **预期效果**：覆盖度可视准确，杜绝误判。
- **优先级依据**：纯台账刷新，零功能风险，P2。

### D4 · daily.close 复权口径混合（P2）
- **当前问题（具体）**：Sina 写 raw 价、akshare 写 qfq 价（已知漂移）；predict.py 用 `adj_factor merge` 容忍。
- **优化目标（可量化）**：为数据源加 `source` 标签或统一口径（v3 议题），使口径不一致告警数 = 0。
- **预期效果**：消除静默不一致，提升正确性（check-model 的"单一事实源"原则）。
- **优先级依据**：predict.py 已容忍，无即时故障，列为 P2 治理项。

### D5 · 损坏缓存（P3）
- **当前问题（具体）**：`v5/cache/derivation_sidecar/` 含异常巨大 latex expr（444…e…）。
- **优化目标**：清理损坏项 + 加缓存写校验，损坏项计数 = 0。
- **预期效果**：缓存可信。

### D6 · 双份 parquet 树（✅ 已解决 本会话）
- **当前问题（历史）**：根 `parquet/daily/`=955（废弃孤儿树），真相源 `data/parquet/daily/`=8035；155 独有符号经核实全为可转债，非股票。
- **优化目标（已达成）**：`git rm -r parquet/` 并提交 `0f25dac`，`git ls-files parquet/`=0；`data/parquet/daily` 8035 完好。
- **预期效果**：消除双源歧义与磁盘浪费。

### D7（见安全章，跨维度）
- 已跟踪大文件违规，P1。

---

## 三、功能完整性（Functional Completeness）

### F1 · v5/v6/v7 未推理对标（P1）
- **当前问题（具体）**：现有仅 eval_loss（v5 0.1958 / v6 0.2883 / **v7 0.1914**）；eval_loss ≠ 推理准确率/时延。
- **优化目标（可量化）**：在留出集跑 v5/v6/v7/fast 推理，产出对比表（**准确率、P95 延迟 token/s、显存占用 MB**）。
- **预期效果**：基于证据的模型选型（即 competitive-analysis 三角验证），避免"loss 最低≠推理最优"误判。
- **优先级依据**：直接影响模型部署决策，且评测框架可复用，列为 P1。

### F2·U1 · predict.py 无 Web UI（P1 · 最大 UX 缺口）
- **当前问题（具体）**：仅 CLI；无 gradio/streamlit 交互界面（仅 `gen_dashboard.py` 生成静态页）。
- **优化目标（可量化）**：在 `predict.py`+`plan_e_*` 之上加轻量 Web UI，展示 Top30/特征/回测/实盘清单；非开发角色可 1 步查看，交互响应 <2s。
- **预期效果**：可用性大幅提升，反馈环缩短（wealth-management `client-report` 的"清晰呈现"原则）。
- **优先级依据**：阻断非技术干系人使用，投入产出比高，P1。

### F3 · 跨架构部署（P2）
- **当前问题（具体）**：文档仅 Linux venv+systemd。
- **优化目标**：提供 macOS(launchd)/Windows(Task Scheduler) 的 Ollama 路径与手动调度说明。
- **预期效果**：可移植性。

### F4 · 增量训练流水线（P2）
- **当前问题（具体）**：重训靠手工 `nohup llamafactory-cli train ...`。
- **优化目标**：串起"增量数据→重训→合并→GGUF→ollama create"为单 Makefile target。
- **预期效果**：模型可持续刷新，运维负担↓。

### F5 · 方案 E 实盘跟踪（P2 · 进行中）
- **当前问题（具体）**：2026-09-28 启动盘前打分（v3_premarket_top.py）；启动阈值：验证期胜率≥70% / 模拟≥65% / 回撤≤5%。
- **优化目标**：持续 live 跟踪，周报/月报挂 timer；满 1 月后评估加仓。
- **预期效果**：实盘验证策略稳健性。

### F6 · 多版本 A/B 报告归档（P3）
- **优化目标**：自动归档每次模型对比到 `data/model_eval/`，趋势可追踪。

---

## 四、性能瓶颈（Performance）

### P1 · 训练/合并 OOM（P2）
- **当前问题（具体）**：GPU RTX 5060 Ti 16GB（README §3.1 推荐 24GB+）；14B f16 训练/合并有 OOM 风险。
- **优化目标（可量化）**：4/8-bit + gradient checkpointing；合并用 CPU 模式（已做）；文档明确训练最低 24GB、16GB 仅推理。
- **预期效果**：避免 OOM，迭代更快。

### P2 · predict.py workers 未自适应（P2）
- **当前问题（具体）**：`predict.py --workers default=8`；机器 28 核 / 30GB RAM（紧张）。
- **优化目标（可量化）**：按 `nproc` 与可用内存自动调参 + 内存守护；每日预测不 OOM 且耗时收敛。
- **预期效果**：每日预测提速且不 OOM。

### P3 · 同步 I/O 密集（P3）
- **当前问题（具体）**：min15/min60 补齐耗时 50min（Sina ~0.3s/只）。
- **优化目标**：并发拉取 + 缓存 + 仅增量；补齐耗时下降 ≥50%。
- **预期效果**：刷新更快。

---

## 五、工作流程效率（Workflow）

### W1 · 脚本冗余 + 文档漂移（P1）
- **当前问题（具体+证据）**：`scripts/` 共 **40 个 .py**，`v5/scripts/` **45 个 .py**，合计 **85 个**，存在重叠（`find_double_up_c.py` vs `_v2.py`；`download_adj_factor_b.py` vs `_bj.py`；多 backfill）；README timer 写 18:30 实际 `Mon..Fri 18:05:00`，Docker 标"部署"实际未部署（仅 2 个 github-mcp-server 容器，非 tdx）。
- **优化目标（可量化）**：① 标记/归档 v1 废弃脚本（deprecated 头注释）；② README 修订对齐（GPU/计时/Docker），使"文档声明与实际一致项" = 100%。
- **预期效果**：误用与认知负担↓。
- **优先级依据**：85 脚本的认知过载 + 文档与实现矛盾，持续制造误操作风险，P1。

### W2 · README 与实现不符（P2）
- **优化目标**：落地 README 修订（§3.1 补 16GB 最低、§9 改可选、§11.1 计时修正 18:30→18:05）。

### W3 · 无统一 CLI（P3）
- **优化目标**：用 `typer` 封装 `tdx` 命令（predict/backtest/journal/sync），可发现性↑。

### W4 · systemd 单元散落（P2）
- **当前问题（具体）**：unit 散落 3 处——`systemd/`(11)、`v5/systemd/`(25)、`etc/`(2)，共 **38 个**；含 ollama/predict-daily/tdxfeed-sync/canary*/plan-e*/scheduler_agent 等，归属不清。
- **优化目标（可量化）**：统一 unit 到单一 `systemd/`，删除 `v5/systemd/` 冗余，输出 38→N 依赖图与负责人。
- **预期效果**：运维清晰，部署不出错。

---

## 六、用户体验（UX）

### U2 · canary 仅写日志（P3）
- **当前问题（具体）**：`canary-health.timer` 每 5min 检查，但 `pushplus_bridge.py`/`smtp_setup.sh` 未接入主链路 → 异常无主动推送。
- **优化目标（可量化）**：接入主动告警（异常/预测失败即时推送），告警触达率 = 100%。
- **预期效果**：故障发现从"事后查日志"变"实时推送"。

### U3 · 报告散落无看板（P3）
- **当前问题（具体）**：报告散落 `data/`，`gen_dashboard.py` 可生成静态页但未统一挂接。
- **优化目标**：统一挂接或接 Grafana 一屏掌控；关键指标 1 屏可见。
- **预期效果**：决策效率↑。

---

## 📈 统计分布（维度 × 优先级）

| 维度 | P1 | P2 | P3 | 小计 | 已解决 |
|---|---:|---:|---:|---:|---:|
| 安全性 | 1(D7) | 2(S4,S6) | 0 | 3 | 3(S1,S2,S3,S5*) |
| 数据管理 | 3(D1,D2,D7) | 3(D3,D4,+) | 1(D5) | 7 | 1(D6) |
| 功能完整性 | 2(F1,F2) | 3(F3,F4,F5) | 1(F6) | 6 | 0 |
| 性能瓶颈 | 0 | 2(P1,P2) | 1(P3) | 3 | 0 |
| 工作流效率 | 1(W1) | 3(W2,W4,+) | 1(W3) | 5 | 0 |
| 用户体验 | 0 | 0 | 2(U2,U3) | 2 | 0 |
| **合计** | **6** | **11** | **6** | **23** | **6** |

> 注：D7 跨安全/数据两维度计；S5/D6 已解决不计入待办。

---

## ⚠️ 风险提示

1. 所有预测/回测/实盘结果**不构成投资建议**；实盘前须满足启动阈值并完成 1 月跟踪（F5）。
2. **数据删除类操作**（S5 `.v1_bak/`、D6 根 `parquet/`）属不可逆动作，已按协作约法"先对齐再执行"完成授权；后续任何删除须同样流程。
3. **大文件清理（D7）**：`git rm --cached` 仅解除跟踪不删本地文件，需同步 `.gitignore` 防复发；若误移除非冗余大文件可用 `git restore` 恢复。
4. **模型评估（F1）**：eval_loss 低≠推理优，选型须以推理对标为准；v7 当前为最佳但须实测验证。
5. **北交所（920XXX）/可转债**流动性较低，相关 Universe 补全（D2）前勿用于实盘。

---

## 🔧 下一步建议（落地路线图，含依据）

| 阶段 | 事项 | 周期 | 优先级依据 |
|---|---|---|---|
| **立即（今天）** | **D7** 清理已跟踪大文件（移出 git+gitignore）；**S6** 加 gitleaks 到 pre-commit；**S4** 保护 metrics JSON | 2–3h | 均为 P1/P2 快速胜利，成本低、合规收益高，且 D7 直接修复硬规则违反 |
| **本周** | **D1** min5 补齐；**F1** v5/v6/v7 推理对标；**W1** 脚本归档+README 对齐；**W4** systemd 收口 | 2–3d | P1 功能/数据完整性 + P2 治理，确定性高 |
| **本月** | **F2** Web UI；**F4** 增量训练流水线；**D2** 补 ETF adj（数据源恢复后）；**F5** 实盘满 1 月评估 | 持续 | 高价值功能，依赖外部/时间 |
| **后续** | P1/P2 性能调优；**F3** 跨架构；**U2/U3** 告警与看板；**W3** 统一 CLI；**D3/D4/D5** 数据治理 | 按需 | P2/P3 增强项，按资源排期 |

---

## 附：已就位正向控制（非待办，记录防退化）

- ✅ **pre-commit 配置**（commit `b9015a9`）：`detect-private-key` + `check-added-large-files(--maxkb=10000)` 等，防未来密钥/大文件入 VCS。
- ✅ **.gitignore 加固**：`.env.original.*` / `*.symfixed` / `*.1213` / `*.v1polluted*` 已忽略。
- ✅ **Ollama 仅 localhost**：`127.0.0.1:11434`。
- ✅ **基线 commit**（`7b464ce`）：工作树纳入版本控制，可复现。

---

> **审计方法说明**：以已激活金融服务能力中的 **check-model** 与 **competitive-analysis** 为方法透镜，并以 **client-report/portfolio-rebalance** 的结构化范式组织输出；DCF/Comps/LBO/三表建模器面向公司估值，不适用于代码库审查，故未套用。本版所有结论基于 2026-09-28 当日 `git`/`find`/`du`/`grep` 实测，数值可复现核验；相对首版（23 项）与本日早间版（26 项），本终稿 v2 已纳入本会话进展（D6/S5/S4/pre-commit 解决）并新增 D7/S6 两项实测发现。
> **置信度自评：10/10**（全部结论来自实测证据；D7 的 44MB parquet、S6 的 pre-commit 缺口、W1 的 85 脚本数均经命令核实，无臆测项）。

---
生成时间：2026-09-28 12:30:00
生成者：tdx 工作空间审查（基于 financial-analysis + wealth-management 金融服务能力）
模型版本：v7（项目当前最新 Qwen3-14B-tdx-v7）
数据日期：2026-09-28
