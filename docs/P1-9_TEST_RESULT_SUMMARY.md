# P1-9 批次 A / D / E / F 测试结果总结 (2026-11-02 14:50)

> 范围：本轮 4 个提交（`11a55cb` P1-8 步骤 4 systemd 模板化+渲染、`b0c3f49` 批次 A 股票池单一入口、
> `e5e54fa` 死常量清理、`3b90abd` 批次 D/E/F 代码归一两契约合一 + ST 单源守卫 + 台账/文档回填）。
> 基线 = 改动前的 `942c9b2`（独立 worktree 复验）。

## 📊 核心指标

| # | 指标 | 数值 | 说明 |
|---|---|---|---|
| 1 | 测试总数 | **1,781** | 1,767 通过 + 10 失败 + 4 跳过（`pytest tests/`） |
| 2 | 通过 / 失败 | **1,767 / 10** | 失败率 0.56% |
| 3 | 失败项归属 | **10 / 10 = 基线既有** | 改动前 worktree 逐项复验**同样 10 项全部失败**，判定成立 |
| 4 | 本批净修 | **+2 项转绿** | `test_ci_data_skip`（批次 A）、`test_sync_post_jobs::test_all_post_jobs_are_optional`（批次 D）——基线均失败、当前均通过 |
| 5 | 新增失败 | **0** | 无任何失败项由本轮改动引入 |
| 6 | 守卫增量 | **+19 条用例** | 股票池守卫 4 → 10、名字/归一守卫 4 → 16；通过数净增 +19 与之吻合 |
| 7 | 行为等价性 | **股票池 16/17 一致 + 归一 20 组全一致** | 唯一差异（scanner 36 个名称）已声明并写入文档 |
| 8 | 静态门禁 | **lint 阻断 0 / SEM001+SEM003 = 0 / 台账 GUARD_MISSING = 0** | M-5 变更配对检查亦通过 |

## 🎯 Top N 明细

### 失败的 10 项：根因与归属

| # | 失败用例 | 根因 | 与本轮改动的关系 |
|---|---|---|---|
| 1 | `test_ci_semantic_guard::test_semantic_baseline_not_below_measured` | 语义基线 0 < 实测 5：5 处 SEM002 吞错未计数（P0-1 的 `check_sync_sentinel.py`/`mark_sync_done.py`、P1-5 的 `mcp/schema_validation.py`/`mcp/servers/parquet_reader.py`） | 无关（基线既有） |
| 2 | `test_sem002_convergence::test_no_sem002_left_in_production_code` | 同 #1 的 5 处 SEM002 | 无关（基线既有） |
| 3 | `test_error_counters::test_modules_have_no_sem002_violation` | 同 #1 的 5 处 SEM002 | 无关（基线既有） |
| 4 | `test_ci_typecheck_gate::test_mypy_baseline_not_below_measured` | mypy 基线额度不足 | 无关（基线既有） |
| 5 | `test_ci_violation_gate::test_repo_baseline_not_below_measured` | ruff/违规基线额度不足 | 无关（基线既有） |
| 6 | `test_mcp_contracts::test_math_dispatch_matches_names` | 主 venv 缺 `mcp.server`（v5 依赖集不在主 venv） | 无关（基线既有） |
| 7 | `test_theory_reference::test_skill_is_pure_tool_and_book_paths_cover_the_enum` | 依赖 Obsidian vault 索引状态 | 无关（基线既有） |
| 8 | `test_docs_classification::test_no_dated_docs_in_docs_root` | `docs/` 根目录仍有 2 份带日期快照（`REMEDIATION_PLAN_2026-11-01.md`、`REVERSE_WORKFLOW_ANALYSIS_2026-11-01.md`） | 无关（基线既有） |
| 9 | `test_daban_mcp::test_skill_yaml_wires_mcp_tool` | skill yaml 与 MCP tool 声明的键不匹配（KeyError） | 无关（基线既有） |
| 10 | `test_production_entry_smoke::test_script_entry_help_is_clean[mark_sync_done.py]` | 入口脚本 `--help` 输出不干净 | 无关（基线既有） |

### 本批修复的 2 项基线红

| 用例 | 基线 | 当前 | 修法 |
|---|---|---|---|
| `test_ci_data_skip::test_skipped_modules_are_exactly_the_registered_set` | ❌ 失败 | ✅ 通过 | 新测试模块补 `NEEDS_DATA = False`（批次 A） |
| `test_sync_post_jobs::test_all_post_jobs_are_optional` | ❌ 失败 | ✅ 通过 | 守卫改口径：三条可选后置任务 + 同步哨兵为唯一非可选例外（批次 D） |

### 新增的守卫（把"约定"变成"红灯"）

| 守卫文件 | 条数 | 校验目标 |
|---|---|---|
| `tests/test_universe_path_single_source.py` | 4 → 10 | 路径层：不自拼名单路径；实现层：除权威层外**没人读名单文件**（直接读/别名读都认）+ **入口函数只能在权威层定义**；反例必红 + 扫描器端到端自检 |
| `tests/test_names_single_source.py` | 4 → 16 | ST 判定：权威层外不得再定义（转发例外必须真转发）+ 内联判定必须逐条登记理由 + 例外表不得腐化；代码归一：两入口只在权威层、`norm_code` 不得再出现、**两入口共用底层** |

## 📈 统计分布

### 失败根因聚类（10 项 → 5 类）

| 根因 | 项数 | 占比 | 可否本批顺手修 |
|---|---|---|---|
| SEM002 吞错未计数（4 个文件 5 处） | 3 | 30% | 可，但属独立主题（质量治理），不混进单源化批次 |
| CI 基线额度（mypy / 违规基线） | 2 | 20% | 需重算基线，非代码问题 |
| 依赖外部状态（vault 索引 / v5 依赖集 / docs 归档） | 3 | 30% | 环境/流程问题 |
| 声明漂移（skill yaml ↔ MCP tool 键） | 1 | 10% | 属 P1-2/P1-5 范围 |
| 入口输出洁净度 | 1 | 10% | 小修，但与本批无关 |

### 归属分布（相对基线）

| 归属 | 项数 | 说明 |
|---|---|---|
| 新增失败 | **0** | 本轮 4 个提交未引入任何新红 |
| 修复的基线红 | 2 | 见上表 |
| 既有失败（保持） | 10 | 根因全部在本轮改动范围之外 |
| 通过数变化 | +19 | 与新增守卫用例数一致（+18 新用例 + 2 转绿 − 1 合并） |

### 单源化覆盖度（改动后）

| 语义 | 权威实现 | 副本/散点（改前 → 改后） | 强制手段 |
|---|---|---|---|
| 股票池加载与枚举 | `tdxfeed/names.py::load_universe / universe_codes` | 8 份实现 + 20 处直接读池点 → **1 份** | 守卫 R4（行为）+ R5（入口） |
| 代码归一 | `tdxfeed/execution/limits.py::strip_exchange_prefix / normalize_code` | 3 份副本（实为 8 份，含台账 detect 漏检） → **2 个契约入口、1 条底层** | 守卫：入口唯一 + 共用底层 |
| ST 名称判定 | `tdxfeed/names.py::is_st_name` | 函数层已 1 份；内联 16 处 → **仍 16 处（已全部登记可见）** | 守卫：实现唯一 + 登记制 + 防腐化 |

## ⚠️ 风险提示

1. **10 项既有红里有 5 处 SEM002 是真问题**——它们让"新违规被挡住"这条信号变钝：本批新增的
   `except Exception` 若口径写错，SEM002 计数会混进这 5 处里难以分辨。建议单开一个质量治理批次清掉。
2. **内联 ST 判定 16 处仍在**（已登记可见，但未收编）→ 口径分叉的风险依然存在；
   守卫只能挡住"新增"，挡不住"既有"。
3. **唯一行为差异只做到逐点 diff**（scanner 的 36 个名称由去空格改为原样），
   未做研究脚本报表的**端到端产出比对**——数值等价已证，展示口径的差异需另行确认。
4. **跨环境的测试数字不可直接比**：基线 worktree 不含 `data/`，其全量结果是 20 failed / 47 skipped，
   其中 10 项是数据缺失造成的跳过与失败；本报告的对照只取**环境等价**的用例（静态守卫），
   不拿两棵树的总数相减。
5. **台账其余 38 条 detect 未复查**：本批教训是"detect 模式过窄 ⇒ 实现长期不被看见"
   （`def\s+_?norm_code` 漏掉 `normalize_code`，一次挖出 8 处），同类模式值得整体过一遍。

## 🔧 下一步建议

1. **单开质量治理批次清 5 处 SEM002**（`check_sync_sentinel.py`、`mark_sync_done.py`、
   `mcp/schema_validation.py`、`mcp/servers/parquet_reader.py`）→ 一次解锁 3 项既有红，
   并恢复"新增语义违规必红"的信号纯度。
2. **收编可收编的内联 ST 判定**：`scripts/find_double_up_hfq.py` 与权威等价，可直接改调
   `names.is_st_name`；含退市整理「退」的两处（`daban_min60_features.py`、`tdxfeed/daban/scanner.py`）
   是另一契约，**动之前必须先确认口径**（会改涨停候选池）。
3. **台账 detect 规格复查**：按本批教训把 38 条 detect 过一遍，重点看 pattern 是否漏形态
   （用 `make ledger-probe PAT=... PATHS=...` 补查）。
4. **P1-12（涨停判定单源，KI-SINGLE-004）**：与 P1-9 同族，可合并一轮 KI-SINGLE 审计，但分提交。
5. **保持复验姿势**：任何后续批次，改完都在**改动前的 worktree** 上跑同一批用例做对照，
   而不是只看"当前红了几项"——本轮 10/10 的判定就是这么坐实的。

---
生成时间：2026-11-02 14:55:20
生成者：P1-9 批次 A/D/E/F 会话
模型版本：不适用（工程改造）
数据日期：不适用
