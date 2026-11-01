# M-5 修复必须附带测试 & M-6 门禁纯化

（2026-11-01 落地 · 工具 `scripts/audit_fix_tests.py`、`scripts/ci_typecheck_gate.py` · Makefile 目标 `gate` / `typecheck-gate` / `fix-tests-check` / `ci-gate`）

## 0. 本轮执行路径（已选定并完成）

用户给出两条可选路径，本轮选 **路径 B + 机制落地**：

| 路径 | 内容 | 状态 |
|---|---|---|
| **B（本轮主线）** | KI-ATOMIC-001 / 002 / 004 三处 P0 原子写修复 | ✅ 完成，语义债务池 **570 → 551（-19 点）** |
| **M-5 机制** | 修复必须附带测试（可执行检查，非口头规范）| ✅ 完成 |
| **M-6 机制** | 门禁纯化（CI 不改工作树 + 全仓 lint + mypy 门禁 + 门禁链串联）| ✅ 完成 |
| A（未做）| 剩余 21 条里再挑 ~10 条一并解决 | 顺带多做了 **4 条**（KI-ATOMIC-001~004 全部关闭）|

**为什么先做 B 再做机制**：三条 P0 原子写修复同时产出了 `tdxfeed/atomic.py` 这个共享出口；M-5 要检验的正是"修复必须带测试"这条闭环——先用这批修复把闭环跑通，机制才不是空转的规范。

## 1. M-5：修复必须附带测试

### 1.1 四条可执行检查（`scripts/audit_fix_tests.py`）

| 规则 | 内容 | 反例 |
|---|---|---|
| **M5-R1** | `status=FIXED` 的台账条目必须绑定一个**真实存在**的测试文件（`test:` 字段优先；没有时允许 `guard:` 指向 `tests/`）| FIXED 但没绑测试 → 失败 |
| **M5-R2** | 绑定的测试文件里必须**出现该条目的 ID**（marker），否则"有测试文件"只是形式 | 测试文件与该问题无关 → 失败 |
| **M5-R3** | diff 改了**生产代码**（`scripts/` `tdxfeed/` `skill_router/` `mcp/` `quant/` `agents/` 的 .py）→ 必须同时改 `tests/` | 只改脚本不补测试 → 失败 |
| **M5-R4** | diff 的**新增行**引入了新的语义违规（SEM001/2/3）→ 必须同时改 `tests/` | 引入新吞错/非原子写且无测试 → 失败 |

纯文档、台账/Makefile/配置变更**全部豁免**（不碰生产代码就不需要测试）。

### 1.2 完成标准与验收条件

| 判据 | 验收方式 | 现状 |
|---|---|---|
| 现有全部 FIXED 条目满足 R1/R2 | `make fix-tests-check` | ✅ 9 条 FIXED 全部通过 |
| R3 能拦住"只改代码不改测试" | `tests/test_audit_fix_tests.py::test_prod_change_without_tests_is_rejected` | ✅ |
| R2 能拦住"测试文件与问题无关" | `tests/test_audit_fix_tests.py::test_missing_marker_is_detected` | ✅ |
| 缺 `test:` 字段能被识别 | `tests/test_audit_fix_tests.py::test_missing_test_field_is_detected` | ✅ |
| CI 实际执行 | `.github/workflows/lint.yml` 新增步骤 | ✅ |

### 1.3 marker 写法

```python
"""M-5（修复必须附带测试）机制本身的测试，含反例。

关联台账条目：KI-RULE-004（修复可以不带测试，无强制闭环）。
"""
```

## 2. M-6：门禁纯化

| 子项 | 改动前 | 改动后 | 验收 |
|---|---|---|---|
| ① CI 改工作树 | `make ci` 含 `format` → **ruff format 会改文件** | `format-check`（`ruff format --check`，**只报告不阻断**）| `make ci` 后 `git status --porcelain` 为空 |
| ② lint-pipeline | 发现未保护管道只打印警告，**永远成功** | 补 `exit 1`，未保护即失败 | 当前 0 个未保护脚本 → 通过 |
| ③ lint 覆盖范围 | 只查 `v5/` | 全仓阻断集 `E9,F63,F7,F82` + 完整规则集只统计 | `make lint` 全仓阻断集通过 |
| ④ mypy 门禁 | 只在 `v5/` 上跑且不阻断 | **新增模块**（`skill_router/` `mcp/` `tdxfeed/execution/`）走**基线门禁**：70 条存量冻结，新增错误阻断 | `make typecheck-gate` 通过 |
| ⑤ 门禁链串联 | `ci: lint format test canary` | `ci: lint format-check lint-pipeline gate typecheck-gate fix-tests-check test canary` | 另提供 `ci-gate`（不含宿主健康检查，可在 CI 容器内跑）|

### 2.1 mypy 基线门禁（`scripts/ci_typecheck_gate.py`）

与 M-2 同一套思路：判定单位 = **(文件 × mypy 错误码)** 配额；存量冻结在 `config/mypy_baseline.json`；净增长即失败；基线只降不升；上升须 `--allow-growth --reason`（30 天审批，到期自动失败）。

当前基线：**70 条 / 12 文件**（`union-attr` 14、`operator` 13、`assignment` 12、`attr-defined` 10 …）。

### 2.2 为什么 `format-check` 只报告不阻断

全仓 **439 个文件**未按 ruff format 排版。若直接阻断，`make ci` 立刻全红，**门禁会被噪声淹没而失去信号**。因此：
- 「CI 不得改工作树」是硬要求（已满足）；
- 格式债作为**独立批次**处理（单独 PR、单独基线），不与功能门禁混在一起。

## 3. 三处 P0 原子写修复：逐项验收

共享出口：`tdxfeed/atomic.py`（`atomic_write_text` / `atomic_write_bytes` / `atomic_write_frame` / `atomic_write_path` / `tmp_sibling`），语义 = **同目录临时文件 → `os.replace` 原子落位 → 失败时原文件不变 → 可选带时间戳备份**。

| 台账条目 | 位置 | 修法 | 测试用例 | 结果 |
|---|---|---|---|---|
| **KI-ATOMIC-001** | `scripts/plan_e_backtest.py`（11 处写入）| 全部改 `atomic_write_frame` / `atomic_write_text` | `test_emit_today_signals_is_atomic`、`test_emit_today_signals_never_torn`（并发读者只能看到旧值或新值）| ✅ |
| **KI-ATOMIC-002** | `scripts/plan_e_journal.py` `save_positions` | 原子写 + 覆盖前留带时间戳备份 | `test_save_positions_is_atomic_and_backed_up`、`test_save_positions_keeps_original_on_failure`（写失败旧账本不变）| ✅ |
| **KI-ATOMIC-003** | `scripts/plan_e_journal.py` `append_journal` | 读-改-写加 `flock` 互斥锁 + 原子落位 | `test_append_journal_keeps_all_rows`、`test_append_journal_is_locked`（8 线程并发不丢行）| ✅ |
| **KI-ATOMIC-004** | `scripts/train_daban_model.py` | 模型先写 `tmp` 再原子落位，覆盖现役模型前自动备份 | `test_model_is_landed_atomically`、`test_previous_model_is_backed_up`、`test_reader_never_sees_partial_model` | ✅ |

助手自身另有 `tests/test_atomic_write_helper.py`（9 项：落位/无残留/并发不撕裂/失败保原/备份/字节/表/临时落位/临时路径唯一性）。

**债务池变化**：语义维度 570 → **551**（-19）；台账 STILL_PRESENT 35 → **31**，FIXED_OK 3 → **9**。
