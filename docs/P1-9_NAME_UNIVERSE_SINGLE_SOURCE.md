# P1-9 名字/池单源方案（2026-11-02）

> 任务来源：整改计划 `docs/REMEDIATION_PLAN_2026-11-01.md` 的 **P1-9「名字/池单源」**
> （KI-SINGLE-001/002/003，R-仓库，计划窗口 11-09 → 11-15）。
> 上游依赖 **P1-8 步骤 4 已于本会话完成**（见 `docs/P1-8_DEPLOY_PATH_UNIFICATION.md` §5.1）。

---

## 1. 目标与范围

把「股票代码怎么规范化」「ST 怎么判」「全 A 名单怎么读」三件事收敛到**每个语义一份实现**，
并用 AST 守卫把"实现数 = 1"钉死——不是靠人记得改，而是靠 CI 红。

**范围内**：

1. `norm_code`（3 份实现）→ 收敛到权威实现 + 两个语义明确的入口。
2. `load_universe`（8 份实现，且**返回类型还不统一**：dict 与 list 混用）→ 收敛到 `tdxfeed/names.py`。
3. `is_st`（计划描述「3 种语义」）→ **核查后确认已是单源**，本项转为「把既有事实用守卫钉死」。

**不在范围内**：涨停判定单源（原 P1-8，顺延 **P1-12**，与本项同属 KI-SINGLE，可合并一轮审计）；
数据表路径单源（已有守卫 `test_no_hardcoded_data_paths.py` / `test_universe_path_single_source.py` 覆盖）。

---

## 2. 现状盘点（2026-11-02 全仓 grep 实测，非计划书转述）

### 2.1 `norm_code` —— 3 份实现，实为**两种契约**

| # | 位置 | 口径 | 是否委托 |
|---|---|---|---|
| 1 | `scripts/_common.py:46` | **规范化**：lower + 去 sh/sz/bj 前缀 + zfill(6)，给 CLI 用户输入 | ✅ 已委托 `tdxfeed.execution.limits.normalize_code` |
| 2 | `scripts/factor_engine.py:57` | **剥前缀**：`re.sub(r"^(sh\|sz\|bj)", "")`，无 lower/zfill，给内部文件名/裸代码 | ❌ 本地实现 |
| 3 | `quant/multifactor/factors.py:31` | 同上（注释自述"与 factor_engine 口径一致"） | ❌ 内联最小实现 |

⚠️ **两份代码注释里已写明"契约不同, 勿盲目统一"**（2026-10-18 审计 Round2 注记）。
因此本项**不是**"删掉两份留一份"，而是：**一份前缀剥离实现 + 两个命名明确的入口**
（`normalize_code` = 规范化；`strip_exchange_prefix` = 仅剥前缀），消除"复制粘贴"而非消除"两种语义"。

### 2.2 `load_universe` —— 8 份实现，返回类型分裂

| # | 位置 | 返回 | 备注 |
|---|---|---|---|
| 1 | `tdxfeed/names.py:11` | `dict[str, str]` | **权威**：带 `_CACHE`，文件缺失返回 `{}` 由调用方决定是否致命 |
| 2 | `tdxfeed/daban/scanner.py:182` | `dict[str, str]` | 带 `path` 参数（默认 `UNIVERSE_PATH`） |
| 3 | `scripts/hengpan_t1_eval.py:192` | `dict[str, str]` | 带 `path` 参数 |
| 4 | `scripts/daban_formula1_screen.py:56` | `dict[str, str]` | 自拼路径 |
| 5 | `scripts/daban_formula1_validate.py:57` | `dict[str, str]` | 自拼路径 |
| 6 | `mcp/servers/backtest.py:686` | `dict[str, str]` | 名为 `_load_universe` |
| 7 | `scripts/predict.py:554` | `list` | **返回类型不同**（列表） |
| 8 | `scripts/plan_e_sim_live.py:89` | `list[str]` | 带 `limit` 参数，**返回类型不同** |

注意 #7 / #8 的 list 版**可能带过滤逻辑**（如剔除指数/ETF）——收编前必须逐个核对语义，
不能想当然改成 `sorted(load_universe())`。

### 2.3 `is_st` —— 计划描述已过时，**现状是单源**

| 位置 | 关系 |
|---|---|
| `tdxfeed/names.py::is_st_name` | **权威实现**（`"ST" in name.replace(" ","").upper()`） |
| `scripts/predict.py:207 is_st()` | 委托 `is_st_name` ✅ |
| `tdxfeed/daban/limit_price.py:81 is_st()` | `from tdxfeed.names import is_st_name as _is_st_name` 后转发 ✅ |

现有守卫 `tests/test_names_single_source.py` 只覆盖 `predict` → `names` 的委托（源码级断言），
**不覆盖** `limit_price`，也不覆盖"全仓不得出现第二份 ST 字符串判定"。

### 2.4 现有守卫覆盖面

- `tests/test_names_single_source.py`：4 条用例，全部围绕 `predict.is_st`。
- `tests/test_universe_path_single_source.py`：**只管路径来源**（禁止自拼 `all_a_stocks.json`），
  **不管"实现数"**。

→ P1-9 的核心交付是**把"实现数 = 1"变成可执行断言**。

---

## 3. Canon 选定

| 语义 | 权威实现 | 入口设计 |
|---|---|---|
| 名单加载 | `tdxfeed/names.py::load_universe()` | 保留一个**带 `path` 的可选参数**重载（覆盖 #2 / #3 的"可指定文件"能力），默认走 `paths.universe_path()` |
| 名单取代码列表 | 同上 + 新增 `universe_codes()` | 供需要 list 的调用点（#7 / #8）使用，**语义差异（过滤）逐点核对后再收编** |
| ST 判定 | `tdxfeed/names.py::is_st_name` | `predict.is_st` / `limit_price.is_st` 保持公开别名（调用点不动），实现唯一 |
| 代码规范化 | `tdxfeed/execution/limits.py::normalize_code` | 保持不变；`scripts/_common.norm_code` 已是薄封装 |
| 仅剥前缀 | **新增** `tdxfeed/paths.py::strip_exchange_prefix()` | `factor_engine` / `quant.multifactor.factors` 改为委托 |

---

## 4. 迁移步骤（分批，每批独立可回滚）

| 批次 | 范围 | 风险 | 验收 |
|---|---|---|---|
| **A** | `tdxfeed/names.py` 加 `load_universe(path=...)` 重载 + `universe_codes()`；`tdxfeed/daban/scanner.py`、`scripts/hengpan_t1_eval.py` 收编 | 低 | 对应模块单测绿；`def load_universe` 计数 8 → 6 |
| **B** | `scripts/daban_formula1_screen.py`、`scripts/daban_formula1_validate.py`、`mcp/servers/backtest.py` 收编 | 低（研究脚本 + MCP 工具） | `make test` 无新增红；MCP 工具返回与改前一致（抽样比对） |
| **C** | `scripts/predict.py`、`scripts/plan_e_sim_live.py` 的 list 版收编 | **中**：这两处带过滤逻辑，误改会改变生产候选池 | 收编前后候选池逐票 diff = 0（用同一份 universe 快照比对）；`predict` Top5 输出一致 |
| **D** | `norm_code` 收敛：`paths.strip_exchange_prefix()` + `factor_engine` / `factors.py` 改委托 | 低 | `def norm_code` 计数 ≤ 2 且必须转发；两处调用点输出逐条一致（回归用例） |
| **E** | 守卫升级：`tests/test_names_single_source.py` 扩为 AST 全仓断言；`tests/test_universe_path_single_source.py` 升到"实现数 = 1" | 低 | **反例必红**：造第二份 `load_universe` / ST 判定 / `norm_code` 必须让守卫失败 |
| **F** | 文档与台账：更新 `AGENTS.md`「数据路径与表口径」小节、`config/known_issues.yaml`（KI-SINGLE-001/002/003 状态） | 低 | 守卫绿 + 台账与代码一致 |

**每批都要跑**：`venv/bin/python -m pytest tests/ -q` + `make gate`。
**每批单独提交**，显式路径（共享工作树，禁止 `-A`）。

---

## 5. 依赖关系

```
P1-8 步骤 4（systemd 模板+渲染）──► P1-9（✅ 本会话已完成上游）
P1-9（名字/池单源）──► P1-12（原 P1-8 涨停判定单源，KI-SINGLE 同族，合并一轮审计）
P1-11（plan_e 拆库）──⚠─► 批次 C 触碰 scripts/plan_e_sim_live.py
                            → 两者不要同批；建议 P1-11 先抽公共部分，P1-9 再收编
D2 决策（执行研究线是否归档）──⚠─► 归档会让 6 份副本中的研究脚本直接消失
                            → 决策截止 11-15；在 D2 定案前，批次 B 里的
                              daban_formula1_* 收编可能白做（但改动本身无害）
```

- **与 P1-8 的衔接**：P1-8 步骤 4 已把"部署路径"单一源化（`scripts/lib/tdx-env.sh` → 渲染器 → 单元）；
  P1-9 把"数据/名字"单一源化（`tdxfeed/names.py` / `paths.py` → 调用方）。两处共用同一条原则：
  **权威实现放 `tdxfeed/`，脚本与工具只做薄封装，守卫用 AST 钉住"只有一份"**。
- **可并行性**：整改计划标注 P1-8/9/10 互相独立；但共享工作树下**必须分提交、错峰**。

---

## 6. 成功判据（可执行）

| # | 判据 | 验证方式 |
|---|---|---|
| 1 | `load_universe` 实现数 = 1 | `grep -rn "def load_universe" --include="*.py" scripts tdxfeed mcp quant` → 仅 `tdxfeed/names.py` |
| 2 | ST 判定实现数 = 1 | AST 守卫：除 `tdxfeed/names.py` 外无第二处 `"ST" in` 判定；`def is_st` 仅允许委托实现 |
| 3 | 代码规范化无复制 | `def norm_code` ≤ 2 处且必须转发；`strip_exchange_prefix` 实现唯一 |
| 4 | 返回类型不分裂 | 名单加载权威返回 `dict`；需要 list 的调用点走 `universe_codes()` |
| 5 | 反例必红 | 守卫自检：往临时目录塞一份第二实现 → 守卫必须失败（`tmp_path` 造样例） |
| 6 | 无新增红 | `venv/bin/python -m pytest tests/ -q` 与改动前同批红（当前基线：10 项既有红，见 §7） |
| 7 | 生产口径不变 | `predict` 候选池与 Top5 输出在收编前后逐票一致；MCP `backtest` 工具返回抽样一致 |

---

## 7. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| **误改生产候选池** | 批次 C 收编 list 版时若把过滤逻辑抹掉，Top5 会变 | 收编前后用同一份 universe 快照做逐票 diff；不一致即回滚该批 |
| **守卫误伤** | AST 扫全仓时把文档字符串/注释里的 `"ST" in` 判成违规 | 沿用仓库既有做法：只扫 AST 字符串字面量，注释与 docstring 不算（见 `tests/test_no_hardcoded_data_paths.py` 的 `_code_strings` 思路） |
| **D2 归档抢先** | 决定归档执行研究线后，批次 B 的目标文件会消失 | 批次 B 可延后到 D2 定案；先做 A/D/E（与归档无关） |
| **P1-11 撞车** | 批次 C 与 plan_e 拆库都动 `plan_e_sim_live.py` | 拆库先行，批次 C 后置；或两批合并做（不推荐，回归面太大） |
| **共享工作树并发** | 与其他代理会话共用同一 checkout | 分批 + 显式路径提交；`git add` 禁用 `-A`/`.` |

---

## 8. 当前基线（2026-11-02，供批次验收对表）

改动前 `venv/bin/python -m pytest tests/ -q` 的**既有红（10 项，与本项无关）**：

- `test_ci_semantic_guard`（5 处 SEM002：P0-1 的 `check_sync_sentinel.py` / `mark_sync_done.py`、
  P1-5 的 `mcp/schema_validation.py` / `mcp/servers/parquet_reader.py`）
- `test_ci_typecheck_gate`、`test_ci_violation_gate`（基线额度）
- `test_daban_mcp::test_skill_yaml_wires_mcp_tool`、`test_mcp_contracts`
- `test_docs_classification`、`test_theory_reference`（依赖 Obsidian vault 状态）
- `test_error_counters`、`test_sem002_convergence`（同上 SEM002 根因）
- `test_production_entry_smoke[mark_sync_done.py]`

> 本会话另修掉 1 项既有红：`test_ci_data_skip`——上一轮 P1-8 新增的
> `tests/test_deploy_paths_unified.py` 缺 `NEEDS_DATA = False`，被 conftest 误判成数据依赖模块。

---

## 9. 下一步（立即可做的第一步）

1. **批次 A**：`tdxfeed/names.py` 加 `load_universe(path=...)` 与 `universe_codes()`，
   `tdxfeed/daban/scanner.py` + `scripts/hengpan_t1_eval.py` 改委托。
2. 同批升级守卫：`tests/test_universe_path_single_source.py` 从"路径"扩到"实现数 = 1"，
   并加 `tmp_path` 反例自检（造第二份 `load_universe` 必红）。
3. 批次 A 绿 → **批次 B** → **批次 D**（`norm_code`）→ **批次 E**（守卫全量升级）→
   **批次 C**（生产口径最敏感，最后做）→ **批次 F**（文档与台账）。
4. P1-12（涨停判定单源）与本项合并成一轮 KI-SINGLE 审计，但**分提交**。

---
生成时间：2026-11-02 11:45
生成者：P1-9 启动会话
模型版本：不适用（工程改造）
数据日期：不适用
