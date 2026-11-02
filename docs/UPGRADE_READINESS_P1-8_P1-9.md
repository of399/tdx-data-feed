# 升级就绪评估报告：P1-8 步骤 4 + P1-9（2026-11-02 14:57 入库 · 15:14 核正）

> 评估对象：基线 `942c9b2` → 当前 `95d57d7`，共 **7 个提交 / 84 个文件（+2,241 / −524）**。
> 目的：判断改造是否全部完成、是否具备上线条件。结论见 §⚠️ 与 §🔧。

## 📊 核心指标

| # | 维度 | 现状 | 结论 |
|---|---|---|---|
| 1 | **版本标识** | 无独立版本号（仓库无 setup.py / pyproject / VERSION）→ **以 git commit 为版本**：`942c9b2`（改造前）→ `95d57d7`（当前） | — |
| 2 | **运行环境** | **开发机 = 生产运行机（同一台）**：用户级 systemd 定时器在跑、`linger=yes`、`/etc/systemd/system` 有系统级单元 | ⚠️ 生产已生效 |
| 3 | **数据库迁移** | **无**。主仓存储为 parquet + JSON；sqlite3 共 **3 处**（`v5/mcp_math/math_cache.py`、`v5/mcp_math/derivation_cache.py`、`v5/workbench/server.py`），本次 v5 零改动、均未触及 | ✅ 无迁移需求 |
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
| 单元是模板、无仓库根字面量 | `tests/test_deploy_paths_unified.py` | 7 条全绿（模板化 / 与主机路径无关 / 已安装单元 == 渲染结果） | ✅ |
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
2. **🟠 首跑数据新鲜度受限**：`tdxfeed_sync` 已滞后 2 天 22 小时（canary ⚠️，既有运维滞后，非本次引入）。
   但**不存在「带旧数据跑」的分支**：`predict-daily` 的 `ExecStartPre=precheck_freshness.sh` 是
   `max_age_days=2` 硬闸门（周一阈值 10-31，周五 10-30 的 bar 已算陈旧）——今天 16:30 同步若成功，
   18:05 用新鲜数据正常跑；若不成功，闸门 exit 2 **整轮跳过**。放行观察须同时覆盖「被跳过」分支：
   跳过不等于本次改动有问题，但首跑取证要顺延到下一个同步成功的交易日。
3. **🟢 门禁信号被稀释（已结案，见「P1-12 落地与生产首跑补记」#1）**：10 项既有红里有 **3 项**
   （`test_ci_semantic_guard` / `test_sem002_convergence` / `test_error_counters`）由同 **5 处 SEM002** 触发
   （`check_sync_sentinel.py`×2、`mark_sync_done.py`、`mcp/schema_validation.py`、
   `mcp/servers/parquet_reader.py`）⇒ 新增吞错违规会混在这 5 处里难以分辨；且 `mark_sync_done.py` 的入口 smoke 至今红。
4. **🟡 性能结论是推断**：仅微基准，无端到端基准（见 §📈）。
5. **🟢 口径分叉残留（已结案，见「P1-12 落地与生产首跑补记」#2；下列数字是批次 E 当时口径）**：
   内联 ST 判定 **16 处**（宽口径计数，含 9 处已知误报；守卫用的精确 AST 口径是 6 处）仍未收编
   （已全部登记进守卫例外表，守卫能挡"新增"、挡不住"既有"）；
   涨停判定 **9 处 `limit - 0.001`** 仍在（P1-12 未做），且其中 **0.001 容差的性质待确认**。
6. **🟡 台账 19 条 STILL_PRESENT**（14 条无守卫、会复发）——与本次改动无关，但属既有技术债。
7. **🟡 共享工作树**：本会话与其它会话共用 checkout ⇒ 回滚与提交都须**显式路径**，禁用 `-A`。

## 🔧 下一步建议

> 下列 #1–#4 已在文末「P1-12 落地与生产首跑补记（2026-11-02 20:30）」逐条结案，保留原文作为决策轨迹；
> #5（回滚演练）与 #6（端到端性能基准）**仍未做**。

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
生成时间：2026-11-02 14:57（入库 commit `4b4c882`；原署 15:15/15:35 晚于入库时间，已核正）

## 核正补记（2026-11-02 15:14，独立核查会话）

逐项复验确认：7 提交 / 84 文件 / +2241−524、1767 passed / 10 failed / 4 skipped、10 项失败名单、
渲染 49 / 镜像 21 / names 16 / universe 10、SEM002 恰 5 处、台账 19/5/16/0、smoke 14/1、
净修 2 项、MCP 工具面零变化、predict-daily 时间线、canary err=0 warn=2 均与实测一致；
微基准 0.74/0.93 ms 在无负载下复现（0.74/0.95）。更正 4 处：

1. **时间戳**：原署 15:15/15:35 晚于入库时间 14:57:29，改按入库时间。
2. **风险 #2**：原「首跑可能用旧数据」不成立——新鲜度闸门会整轮拦下（见该条改写）。
3. **「唯一 sqlite3」**：实为 3 处（见 §核心指标 #3）；「未触及、无迁移」结论不变。
4. **deploy_paths 守卫计数**：实测 7 条，原写 4 条。

另：10 项红之第 8 项（`test_no_dated_docs_in_docs_root`）已于本补记同时消解——
`REMEDIATION_PLAN_2026-11-01.md` / `REVERSE_WORKFLOW_ANALYSIS_2026-11-01.md` 两份残留快照
移入 `docs/archive/` 并改写 3 处路径引用，守卫 5 条全绿。
生成者：升级就绪评估会话
模型版本：不适用（工程改造）
数据日期：不适用

---

## P1-12 落地与生产首跑补记（2026-11-02 20:35，独立核查会话）

本节把上面「风险 #3 / #5 / #6」与「下一步建议 #1–#4」逐条结案。数字均为实测，清理旧产物（仓库
`__pycache__` / `.pytest_cache`、副本 `data/`、副本里的 gitignore 目录）后重跑取得。

1. **风险 #3 结案（吞错面）**：5 处 SEM002 全部计数化（`d9d6951`），`scripts/ci_semantic_guard.py`
   现 **SEM001/SEM002/SEM003 各 0 条**，`config/semantic_baseline.json` 仍为 0（没靠抬基线解锁）；
   `mark_sync_done.py` 补 argparse 后入口 smoke 红消解 ⇒ 三项红（semantic_guard / sem002_convergence /
   error_counters）解锁。
2. **风险 #5 结案（口径分叉）**：
   - 内联 ST：2a 收编 `find_double_up_hfq`、2b 新增 `names.is_st_or_delisting`（方案 A）收编含「退」两处，
     精确 AST 口径 6 处 → **3 处**（仅前缀匹配 1 + vault 文件名词干 2，口径与权威不同、逐条登记保留）；
     `daban/paper.py` 的「退 → `risk_warning`」属状态分类，未合并。逐票 diff = 0（宇宙 5221 / 样本 17794）。
   - 涨停容差：Phase 0② 确认 **0.001 = `limits.RET_TOLERANCE` 浮点容忍**，不是有意放宽 ⇒ P1-12 是去重不是收窄。
     原 detect 只认变量名 `limit`，放宽形态后是 **17 处代码 + 1 处 docstring**，全部收编（标量走
     `is_limit_up_ret`、向量化减 `RET_TOLERANCE`，`features_5d.LIMIT_MAP` / `LIMIT_ST_MAIN_OLD` 改派生）。
     等价性：与 HEAD 版同输入对跑，**1044 个特征向量 / 30276 个取值 + 839913 个逐日涨停判定点零差异**
     ⇒ 翻倍样本 CSV 与特征表**不需要重跑**。价格级 EPS（`ztj/limit_price - EPS`，min60 是 0.005）
     是另一契约，明确不收编。
3. **放行判定结案（原建议 #1/#2）**：18:05 `predict-daily` 两道 `ExecStartPre` 均 SUCCESS、`ExecStart`
   exit 0（18:10 又跑一次，两次 Top5 一致），5213/5221 只打分并出榜；`logs/predict.err` 中
   `load_universe` / `normalize_code` / `is_st` 相关异常 **0 条**；本轮改到的开盘一字过滤在新代码路径上
   实际执行（日志「过滤 T 日开盘即涨停: 4932 → 4923」）。canary 20:30 那条 **err=0 / warn=1**
   （warn 从 2 降到 1，剩 `swallowed_errors`——吞错从「不被计数」变成「计数非零」，正是 #1 的预期副作用）。
4. **风险 #6 计数更新**：台账复扫 **STILL_PRESENT 15 · RESOLVED 0 · FIXED_OK 25 · GUARD_MISSING 0**，
   无守卫（会复发）14 → **11** 条；M-5「FIXED 必须绑定含条目 ID 的真实测试」通过。
5. **全量回归（清理旧产物后）**：
   - 本机 `tests/ + v5/tests/`：**1832 passed / 4 failed / 6 skipped**（6:53）。4 条红 = 3 条既有基线红
     （mypy 基线、ruff 基线、`test_daban_mcp` 的 skill yaml `KeyError`）**+ 1 条新红**
     `test_hfq_factor_monotone::test_downward_jumps_match_whitelist`——**数据缺陷，非代码回归**，见下。
   - CI 同构副本（改用 `git ls-files` 跟踪清单重建，1941 文件、无 `data/`、遮蔽 `/home`）：
     **1758 passed / 82 skipped / 2 failed**，2 条均为副本免疫项（bare-pytest shebang、`/etc` 单元镜像比对）；
     上一轮那两条语义红是我用 tar 整树拷贝时把 gitignore 的 `train/` 带进副本造成的，已按跟踪清单重建消除。
     CI 阈值（≥1140 passed、≤100 skipped）满足。
6. **新红归因（绊线正常工作，需人处置）**：`tdxfeed.hfq_factor` 今晚增量写坏了
   `data/hfq_factor/002247.parquet` 的 2026-10-29 / 10-30 / 11-02 三行——`raw_close` 落的是
   **6 位小数的后复权价**（`13.687908`；交易所价不可能，13.687908 ÷ 6.48716 = 2.1099 才是真收盘价），
   于是 `hfq_factor` 算成 1.0，序列上出现 **−84.6%** 的因子塌方（该文件 mtime 19:41:12，复跑两次同一结果，
   不是读写竞态）。全表不变量审计（`data/parquet/daily` 权威收盘价 vs hfq 表 `raw_close`，
   2026-09-01 起 **199019 行逐行对齐**）：**只有 002247 这 3 行不一致**，其余全部一致
   （日线定位用 `paths.daily_files()` + `exchange_prefix` 消歧，避免同码双文件把个股读成指数）。
   影响面：`adjusted.frame(kind="hfq")` 会给 002247 造出一个约 +546% 的假日涨幅，污染跨 10-29 的
   价格比率特征与标签；**今天 18:05 的实盘预测未受影响**（它跑在 19:41 写入之前）。
   正确值可精确还原（无需臆造）：日线收盘 × 既有因子 6.48716 = 表内已存的 `hfq_close`
   （2.11 → 13.6879076 ✓ / 2.02 → 13.1040632 ✓ / 2.08 → 13.4932928 ✓），
   所以处置就是把这三行的 `raw_close` 改回日线收盘价、`hfq_factor` 回到 6.48716。
   处置建议（须带日期备份后动手，`data/` 不进 git）：按上法定点修复或整表重拉 002247，
   回到 12 条白名单；同时在写入侧加「`raw_close` 必须与日线权威收盘价一致（容差 0.005 元）」
   的前置校验，让这类写坏在落盘前就被拦下（下一批次项）。

仍未做：#5 回滚演练、#6 端到端性能基准、上面第 6 条的写入侧前置校验。

生成者：P1-12 批次执行会话
模型版本：不适用（工程改造）
数据日期：2026-11-02
