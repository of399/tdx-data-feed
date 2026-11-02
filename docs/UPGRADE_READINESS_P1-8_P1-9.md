# 升级就绪评估报告：P1-8 步骤 4 + P1-9（2026-11-02 15:15）

> 评估对象：基线 `942c9b2` → 当前 `95d57d7`，共 **7 个提交 / 84 个文件（+2,241 / −524）**。
> 目的：判断改造是否全部完成、是否具备上线条件。结论见 §⚠️ 与 §🔧。

## 📊 核心指标

| # | 维度 | 现状 | 结论 |
|---|---|---|---|
| 1 | **版本标识** | 无独立版本号（仓库无 setup.py / pyproject / VERSION）→ **以 git commit 为版本**：`942c9b2`（改造前）→ `95d57d7`（当前） | — |
| 2 | **运行环境** | **开发机 = 生产运行机（同一台）**：用户级 systemd 定时器在跑、`linger=yes`、`/etc/systemd/system` 有系统级单元 | ⚠️ 生产已生效 |
| 3 | **数据库迁移** | **无**。主仓存储为 parquet + JSON；唯一 sqlite3 在 `v5/mcp_math/math_cache.py`（数学缓存，本次未触及） | ✅ 无迁移需求 |
| 4 | **配置变更** | 3 类：`Makefile`（新增 `systemd-render`、安装链改渲染后安装）、`config/known_issues.yaml`（3 条 FIXED）、`systemd/` 33 个单元**改存模板**（部署形态变化） | ⚠️ 部署链有变化 |
| 5 | **对外接口变更** | **0**。MCP 工具面未变（只改内部符号 `_load_universe`/`_universe` → `_pool`）；`names.load_universe(path=None)` 为**向后兼容**的可选参数扩展；`_common.norm_code` 改为**同一函数对象的别名** | ✅ 无兼容性破坏 |
| 6 | **回归测试** | `pytest tests/` = **1,767 passed / 10 failed / 4 skipped**；10 项失败经**基线 worktree 逐项复验**（改动前 `942c9b2` 同样 10 项全失败）⇒ **新增失败 0**，并净修 2 项基线红 | ✅ 无回归 |
| 7 | **性能验证** | 微基准已量化：股票池加载 **0.74 ms/次**（本次去缓存的代价）、`universe_codes` 0.93 ms、systemd 渲染 0.5 ms（一次性）。**未做端到端性能基准** | 🟡 部分完成 |
| 8 | **部署态** | 用户级单元已安装且与渲染结果**逐字节一致**（守卫绿）；系统级 21 个镜像单元与渲染结果**字节一致**（守卫 6 passed）；canary `err=0`（2 warn） | ✅ 已部署 |
| 9 | **数据影响** | 无 schema 变更、无数据文件改动（`git status data/` 为空）⇒ **无需数据回滚** | ✅ |
| 10 | **未解决项** | 台账 **STILL_PRESENT 19 条**（其中 14 条无守卫、会复发）；本会话新增待办 6 项；**1 个上线验证缺口**（见 §⚠️-1） | ⚠️ 有遗留 |

## 🎯 Top N 明细：变更清单与逐项回滚动作

| # | 模块 / 功能 | 具体变更 | 对外影响 | 回滚动作 |
|---|---|---|---|---|
| 1 | `scripts/render_systemd_units.py`（新）+ `Makefile` | 新增渲染器（`@TDX_HOME@` → 真实绝对路径，单一源 = `scripts/lib/tdx-env.sh` 的 `TDX_HOME`）；`timer-install` 先渲染再安装 | 无（构建期） | `git revert 11a55cb` + `make timer-install` |
| 2 | `systemd/` 33 个单元 | 字面量 → 占位符模板；`plan-e-monitor` 的 `%h/tdx-data-feed` 一并统一 | **部署形态变化**（单元不再是可直接加载的文件，须经渲染） | `git checkout 942c9b2 -- systemd/ && make timer-install && systemctl --user daemon-reload`（系统级**无需动作**：字节未变） |
| 3 | `tdxfeed/names.py` | 股票池唯一入口：`load_universe(path=...)` + `universe_codes(path, prefixes, fallback)`；**刻意不缓存**（MCP 长驻进程会让名称映射变陈旧） | `load_universe` 新增可选参数（向后兼容）；其余为新增 | `git revert b0c3f49`（或 `git checkout 942c9b2 -- tdxfeed/names.py`） |
| 4 | 20 处直接读池点 / 19 个文件 + `predict` / `plan_e_sim_live` / 3 个 MCP server | 全部改走统一入口；`_load_universe`/`_universe` → `_pool`（内部符号） | 无（MCP 工具面不变） | 随 #3 一并回滚 |
| 5 | `tdxfeed/execution/limits.py` | 新增 `strip_exchange_prefix`；`normalize_code` 改为复用它（**两契约一底层**） | 新增函数；`normalize_code` 行为不变 | `git revert 3b90abd` |
| 6 | `_common.py` / `factor_engine.py` / `quant/multifactor/factors.py` | 三份 `norm_code` 副本删除或改为别名；调用点改 `strip_exchange_prefix` | 无（`_common.norm_code` 仍是同名可导入符号） | 随 #5 一并回滚 |
| 7 | `tests/test_names_single_source.py`（4→16）、`tests/test_universe_path_single_source.py`（4→10） | ST/归一/股票池的单源守卫（行为层 + 入口层 + 反例必红 + 端到端自检） | 无 | 随代码回滚 |
| 8 | `config/known_issues.yaml` | KI-SINGLE-001/002/003 → **FIXED**（含 `fix_note`，detect 规格修正） | 无 | 随代码回滚 |
| 9 | `AGENTS.md` / `docs/*`（5 份） | 记录 systemd 模板约定、股票池唯一入口、两契约边界、测试结果总结、下一批方案 | 无 | 无需回滚 |

**回滚三层**：
- 代码层：`git revert`（按 #3→#4、#5→#6 成对回滚；共享工作树必须**显式路径**，禁用 `-A`）。
- 部署层：用户级按 #2 重装；**系统级无需动作**（渲染对已部署内容字节无变化）。
- 数据层：**无需回滚**（无 schema 变更、无数据文件改动）。

## 📈 统计分布

### 验收标准达成情况（逐条判据 + 实测结果）

| 判据 | 命令 / 位置 | 实测结果 | 状态 |
|---|---|---|---|
| 单元是模板、无仓库根字面量 | `tests/test_deploy_paths_unified.py` | 4 条全绿（模板化 / 与主机路径无关 / 已安装单元 == 渲染结果） | ✅ |
| 单元渲染正确 | `venv/bin/python scripts/render_systemd_units.py --check` | `✓ 49 个单元模板通过校验` | ✅ |
| 系统级镜像零漂移 | `tests/test_systemd_system_units_mirror.py` | **6 passed**（渲染 vs `/etc` 字节一致） | ✅ |
| 股票池实现数 = 1 | `tests/test_universe_path_single_source.py` | 10 条全绿（路径层 + 行为层 R4 + 入口层 R5 + 反例必红 + 端到端自检） | ✅ |
| ST / 归一单源 | `tests/test_names_single_source.py` | 16 条全绿（实现唯一 + 内联登记制 + 例外表防腐化 + 两入口共用底层） | ✅ |
| 全量回归无新增红 | `pytest tests/` + 基线 worktree 对照 | 1,767 passed / 10 failed（10/10 基线既有，逐项复验） | ✅ |
| 生产入口可启动 | `tests/test_production_entry_smoke.py` | 14 passed / 1 failed（唯一失败是 `mark_sync_done.py`——**既有红**，且正是 P1-12 项 1 的对象）；`predict.py --help` 实跑 OK | ✅ |
| 语义违规归零 | `scripts/ci_semantic_guard.py` | SEM002 **5 条**（P1-12 项 1 待清）、SEM001/SEM003 = 0 | 🟡 |
| 台账绑定完整 | `scripts/audit_ledger.py scan` | STILL_PRESENT 19 · RESOLVED 5 · FIXED_OK 16 · **GUARD_MISSING 0** | ✅ |
| M-5 修复必附测试 | `scripts/audit_fix_tests.py check` | 通过（FIXED 条目均绑定可验证测试） | ✅ |
| lint 阻断集 | `make lint` | 阻断集全仓通过 | ✅ |
| 运行时健康 | `scripts/canary_health_check.py` | `err=0 warn=2`（⚠️tdxfeed_sync 同步过旧 2 天 22 小时；⚠️swallowed_errors） | ✅/⚠️ |

### 变更面分布（84 文件）

| 类别 | 文件数 | 说明 |
|---|---|---|
| 生产代码 | 24 | `tdxfeed/`（4）、`scripts/`（16）、`mcp/`（3）、`quant/`（1） |
| 部署/配置 | 35 | `systemd/`（33）+ `Makefile` + `config/known_issues.yaml` |
| 测试 | 12 | 守卫升级 2、新增守卫内容、快照/映射修正 3、入口与语义守卫 7 |
| 文档 | 13 | `AGENTS.md` + `docs/`（P1-8 / P1-9 方案与落地记录 / 测试结果总结 / 本报告） |

### 性能影响（微基准，本机实测）

| 场景 | 改造后 | 影响评估 |
|---|---|---|
| 股票池加载（`load_universe`，**本次去掉缓存**） | **0.74 ms/次** | 可忽略：批处理脚本每次运行 1–2 次；MCP 每次请求 1 次（请求本身是 ms 级） |
| `universe_codes()` | 0.93 ms/次 | 同上 |
| systemd 渲染（49 个单元） | 0.5 ms | 一次性，安装前执行 |

> ⚠️ **未做端到端性能基准**（如 `predict-daily` 全市场推理耗时对比）——改动不涉及计算内核，
> 且未删任何计算路径，但"端到端耗时无回归"目前是**推断**而非实测。

## ⚠️ 风险提示

1. **🔴 上线验证缺口：新代码尚未在生产调度中实际跑过。**
   `predict-daily.timer` 于 **2026-11-01 10:28** 才启用（`OnCalendar=Mon..Fri 18:05` + `Persistent=true`），
   上次实际运行为 **10-30**（旧代码）⇒ **今天 18:05 是启用后的首次运行**，也就是新代码的首次真实生产执行。
   本次改动触及的定时路径（`predict.py` / `plan_e_sim_live.py`）正是这条。
   ⇒ **判定：代码侧具备上线条件，但"已在生产跑过"这一条不成立**，需以 18:05 首跑作为放行观察点。
2. **🟠 首跑数据新鲜度受限**：`tdxfeed_sync` 已滞后 2 天 22 小时（canary ⚠️，既有运维滞后，非本次引入）
   ⇒ 18:05 首跑用的可能是旧数据，**不能用首跑结果评估本次改动的正确性**（只验证"不崩"）。
3. **🟠 门禁信号被稀释**：10 项既有红里有 **5 处 SEM002** 未清（`check_sync_sentinel.py`×2、
   `mark_sync_done.py`、`mcp/schema_validation.py`、`mcp/servers/parquet_reader.py`）
   ⇒ 新增吞错违规会混在这 5 处里难以分辨；且 `mark_sync_done.py` 的入口 smoke 至今红。
4. **🟡 性能结论是推断**：仅微基准，无端到端基准（见 §📈）。
5. **🟡 口径分叉残留**：内联 ST 判定 **16 处**仍未收编（已全部登记进守卫例外表，守卫能挡"新增"、挡不住"既有"）；
   涨停判定 **9 处 `limit - 0.001`** 仍在（P1-12 未做），且其中 **0.001 容差的性质待确认**。
6. **🟡 台账 19 条 STILL_PRESENT**（14 条无守卫、会复发）——与本次改动无关，但属既有技术债。
7. **🟡 共享工作树**：本会话与其它会话共用 checkout ⇒ 回滚与提交都须**显式路径**，禁用 `-A`。

## 🔧 下一步建议

1. **放行判定**：建议**条件性放行**——代码、测试、部署、接口四项均达标，唯一缺口是「生产首跑未发生」。
   放行条件 = 今天 18:05 `predict-daily` 首跑后检查：① 服务 `Result=success`（不因本次改动崩）；
   ② 日志无 `load_universe` / `normalize_code` 相关异常；③ `make canary` 仍 `err=0`。
2. **首跑后取证**：`journalctl --user -u predict-daily.service --since 18:00` + `logs/canary.log` 留档，
   并把结论补进本报告（作为"已上线"证据）。
3. **紧接 P1-12 批次 Phase 1**（不依赖任何口径确认，零候选池影响）：清 5 处 SEM002 ⇒ 一次解锁 3 项红
   + 修掉 `mark_sync_done.py` 的入口 smoke 红；随后做台账 detect 复查（40 条，重点 KI-SINGLE-002/003/004）。
4. **口径确认（Phase 0）**：① 「退」字判定归属（建议新增 `is_st_or_delisting`，行为等价）；
   ② `limit - 0.001` 的 0.001 是浮点容忍还是有意放宽（决定 P1-12 是"收编"还是"权威层显式提供带容差入口"）。
5. **回滚演练**：上线前在 worktree 里演练一次「用户级单元回滚」（`git checkout 942c9b2 -- systemd/ &&
   make timer-install && daemon-reload`），确认回滚路径可用——目前回滚方案只经过推演，未实跑。
6. **补端到端性能基准**（可选）：若要求"性能无回归"的实测证据，可在 18:05 首跑时记录 `predict-daily`
   的 `Elapsed` 与改造前同口径对比（历史基线需从 journal 取）。

---
生成时间：2026-11-02 15:35:00
生成者：升级就绪评估会话
模型版本：不适用（工程改造）
数据日期：不适用
