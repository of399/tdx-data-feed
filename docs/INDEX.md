# tdx-data-feed 文档目录索引

> **入口文档** · 维护者：jiuben · 创建：2026-10-16
> 用途：避免 docs/ 47 个文档再次"散落过时"，按主题分类便于查阅/检索/治理。
> 上游索引：根目录 `README.md`（新手 5 分钟上手）；本索引面向**工程内**查阅与维护。

---

## 阅读路径

| 你是谁 | 先看这些 |
|---|---|
| **新成员 onboarding** | `README.md`（根目录）→ 本索引 → §1 核心架构 |
| **排查问题** | §1.4 审查类 + §6 退役登记 |
| **跑量化/预测/回测** | §2 量化策略 + `README.md §5 快速上手` |
| **看当前/未来要做什么** | `docs/ROADMAP_2026-10-16.md` + `WORKFLOW.md §7.4` |
| **训练/部署模型** | §4 训练 / §2.2 阶段1 因子 |
| **Obsidian 笔记库** | §5 Obsidian 笔记库（16 个文档） |
| **数据治理/同步** | §3 数据治理与同步 |

---

## §1. 核心架构 / 工作流 / 审查

| 文档 | 内容 | 状态 |
|---|---|---|
| `WORKFLOW.md` | 工作流全景（程序/Agent/Skill/MCP/数据流）| 2026-10-16 回填 10 处 |
| `ARCHITECTURE.md` | 架构深度说明 | 长期 |
| `WORKSPACE_AUDIT.md` | 工作空间早期审计 | 历史 |
| `WORKFLOW_REVIEW_2026-10-16.md` | **审查 v1**（v2 审查对照基线）| 历史 |
| `WORKFLOW_REVIEW_2026-10-16-v2.md` | **审查 v2**（最新，10 timer + 1 path + 1 service）| 最新 |
| `ROADMAP_2026-10-16.md` | **路线图**（4 项：v9 量化 / D→E 联调 / 监控栈 / docs 治理）| 最新 |

> 建议路径：`WORKFLOW.md`（总览）→ `WORKFLOW_REVIEW_2026-10-16-v2.md`（细节）→ `ROADMAP_2026-10-16.md`（未来）。

## §2. 量化策略

| 文档 | 内容 |
|---|---|
| `PLAN_E.md` | 方案 E 策略详解（v1/v2 规则 + 关键参数）|
| `PLAN_E_LIVE.md` | 方案 E 实盘启动指南 |
| `LIVE_TRACKING_GUIDE.md` | 实盘跟踪使用手册 |
| `quant_plan_feasibility_review.md` | 量化计划可行性审查 |
| `HYBRID_LLM_STRATEGY.md` | 混合 LLM 策略（Qwen3-14B + 量化信号）|
| `MULTIFACTOR.md` | 多因子模型设计 |

### §2.1 阶段1 因子迁移（Polars 21 维 → 21 维 OOS Sp 0.3475）

| 文档 | 内容 |
|---|---|
| `stage1_feature_migration_plan.md` | 阶段1 因子迁移计划（Polars 复刻 features_5d）|
| `stage1_optuna_report.md` | Optuna 特征选择报告（21 维）|

## §3. 数据治理 / 同步 / Schema

| 文档 | 内容 |
|---|---|
| `tdx-data-feed-数据字典.md` | 数据 schema 字典（手工/自动生成）|
| `SYNC_PIPELINE_FAULT_ANALYSIS_2026-10-12.md` | 同步管道故障分析（2026-10-12）|
| `BACKFILL_REPORT_20260927.md` | 回填报告（2026-09-27）|
| `taskA_sync_acceptance_20261010.md` | taskA 同步验收 |
| `volume-单位治理计划书.md` | 成交量单位治理（手/股）|
| `phase0_data_layer_hardening.md` | Phase 0 数据层硬化 |
| `dependency_run_verification_prompt.md` | 依赖运行验证 prompt |

## §4. 训练 / 模型 / 部署

| 文档 | 内容 |
|---|---|
| `training_stack_e2e_guide.md` | 训练栈端到端指南 |
| `training_stack_e2e_empirical_checklist.md` | 训练栈端到端清单（实证）|
| `training_stack_prompt.md` | 训练栈 prompt 模板 |
| `finetune_low_vram_comparison.md` | 低显存微调方案对比 |
| `github_resources_upgrade_plan_20261010.md` | GitHub 资源升级计划 |

## §5. Obsidian 笔记库（16 文档）

> 笔记库项目本身在 `~/ObsidianVaults/tdx-data-feed` 与 `~/ObsidianVaults/math-study`，与本仓解耦；本节文档记录其规划/同步/治理。

### §5.1 总体与升级
- `OBSIDIAN_BIDIRECTIONAL_SYNC_EVAL.md` — 双向同步评估
- `OBSIDIAN_KB_RENOVATION.md` — 知识库翻新总览
- `obsidian_upgrade_plan_20261015.md` — 2026-10-15 升级计划
- `obsidian_pending_cleanup_checklist_20261016.md` — 待清理清单

### §5.2 数学笔记库（独立库）
- `math_upgrade_plan_20261015.md` — 数学库升级计划
- `math_capability_audit_20261015.md` — 数学能力审计
- `obsidian_math_enhancements_design_20261015.md` — 增强设计
- `obsidian_math_kb_plan_review_20261015.md` — 计划评审

### §5.3 阶段计划（Phase 1.x / Phase 2）
- `obsidian_phase_1_1_plan_20261015.md` / `obsidian_phase_1_2_plan_20261015.md` / `obsidian_phase_1_4_plan_20261015.md`
- `obsidian_phase_1_2_rollback_20261015.md` / `obsidian_phase_1_4_rollback_20261015.md`（回滚方案）
- `obsidian_phase_2_execution_20261016.md` / `obsidian_phase_2_followup_20261016.md`

### §5.4 同步 / 重载 / 元数据 / 标签
- `obsidian_sync_reload_verify_plan.md` — 同步重载验证计划
- `obsidian_git_workflow_20261015.md` — git 工作流
- `obsidian_dataview_snippets.md` — Dataview 片段
- `obsidian_metadata_schema.md` — 元数据 schema
- `tag_taxonomy.md` — 标签分类法

## §6. 退役 / 治理登记

| 文档 | 内容 | 状态 |
|---|---|---|
| `pushplus_bridge_status_2026-10-16.md` | **pushplus-bridge 退役记录**（零调用方已停用，可逆，详见内附 7 段）| 2026-10-16 生效 |

## §7. API / 集成 / 标签

| 文档 | 内容 |
|---|---|
| `API.md` | 项目 API 接口文档 |
| `ai_plugin_audit_20261015.md` | AI 插件审计（2026-10-15）|

---

## 维护约定

1. **新增文档**：在本索引对应类别加一行，标注状态（最新/历史/草稿）。
2. **文档改名/废弃**：在 `WORKFLOW_REVIEW_2026-10-16-v2.md` 或新审查中说明，不要直接删除 git tracked 文件。
3. **过时治理**：每个季度或重大变更（schema/部署/数据规模）后，跑一次"WORKFLOW 审查"，回填过时陈述，产出新 ROADMAP 段落。
4. **退役登记**：任何停用的服务/脚本/单元在 `§6 退役 / 治理登记` 加一条。

## 索引本身的状态

- 创建于 2026-10-16（路线 4：docs 治理的产出）
- 上游：`README.md` / `WORKFLOW.md` / `WORKFLOW_REVIEW_2026-10-16-v2.md`（互链）
- 下游：47 文档按 7 段分类

---

> 本索引定位：**docs/ 的入口**，与 README.md 并列（README 面向新成员，INDEX 面向工程内查阅/治理）。如发现遗漏/错误，请直接编辑本文件并提交。
