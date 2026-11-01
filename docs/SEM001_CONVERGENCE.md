# SEM001/SEM003 收敛：语义债务池 202 → 0

（2026-11-01 · 工具 `scripts/sem001_migrate.py` · 守卫 `scripts/ci_semantic_guard.py` · 基线 `config/semantic_baseline.json`）

## 0. 目标与结果

上一轮把 **SEM002 吞错 349 处**清零后，语义维度只剩两类：

| 规则 | 收敛前 | 收敛后 | 说明 |
|---|---|---|---|
| **SEM001 原子写** | 194 | **0** | 分 4 批，按目录推进，生产链路优先 |
| **SEM002 吞错必计数** | 0（上一轮已清）| 0 | 本轮保持 |
| **SEM003 外部调用必带 timeout** | 8 | **0** | 逐条补 timeout + 1 处真正的超时实现 |
| **合计** | **202** | **0** | 目标"降至个位数"，实际清零 |

ruff 债务池保持 205（未因本轮改造新增），mypy 存量从 70 降到 64（顺带修掉 6 处）。

## 1. SEM001 批次划分

| 批次 | 范围 | 处数 | 文件 | 手法 |
|---|---|---|---|---|
| **B1** | 生产链路核心（`predict.py`、`monitor_plan_e_live.py`、`plan_e_confidence.py`、`plan_e_v3_live_track.py`、`factor_engine.py`、`find_double_up_c_v2.py`、`plan_e_real_samples.py`、`compare_6year.py`、`top_n_sensitivity.py`）| 30 | 9 | 工具 + 逐文件导入冒烟 |
| **B2** | `scripts/` 其余 | 179 | 95 | 工具批量 + 94 个文件导入冒烟 |
| **B3** | `tdxfeed/` | 20 | 9 | 工具批量 |
| **B4** | `mcp/` `quant/` `training/` | 9 | 4 | 工具批量 |
| **手工** | 4 处特殊形态 | 4 | 4 | 读-改-写加 flock / Popen 流式重定向豁免 |

### 改造模式（统一）

| 原写法 | 改为 |
|---|---|
| `X.write_text(v, encoding=...)` | `atomic_write_text(X, v, encoding=...)` |
| `X.write_bytes(b)` | `atomic_write_bytes(X, b)` |
| `df.to_csv(path, ...)` | `atomic_write_frame(df, path, ...)` |
| `df.to_parquet(path, ...)` | `atomic_write_parquet(df, path, ...)` |
| `with open(p,"w") as f: ...` | 写 `f"{p}.tmp"` + 块后 `os.replace(tmp, p)`（块体原样保留）|
| 读-改-写追加 | `flock` 互斥 + 原子落位 |

### 4 处手工处理的特殊形态

| 位置 | 问题 | 处理 |
|---|---|---|
| `scripts/batch_distill.py` / `distill_outputs.py` | 先读行数再追加 → 并发续跑会重复/丢行 | `open(OUT,'a+')` + `fcntl.flock(LOCK_EX)` 后再读再追加 |
| `scripts/download_adj_factor_watch.py` | `stdout=open(log,'w')` 交给子进程流式写 | **守卫修正**：Popen 参数里的 open() 是流式写入，不算"整体覆盖" |
| `tdxfeed/backfill_history.py` | `open(path,"a+")` 锁文件被当成覆盖写 | **守卫修正**：`"w"` 才算覆盖，`"a"`/`"+"` 是追加 |

> 两条守卫修正都是**把误判改成正解**，不是给违规开口子：`a+` 本就不截断；子进程流式写入本就不存在"半截文件"语义。

## 2. SEM003：8 处逐条补齐

| 位置 | 原状 | 处理 |
|---|---|---|
| `scripts/compare_6year.py` / `top_n_sensitivity.py` | `subprocess.run(回测)` 无 timeout | `timeout=SUBPROCESS_TIMEOUT`（3600s）|
| `scripts/plan_e_real_samples.py` | `subprocess.run(purge-sim)` | `timeout=SUBPROCESS_TIMEOUT`（600s）|
| `scripts/plan_e_v3_live_track.py` | `subprocess.run(cmd)` | `timeout=SUBPROCESS_TIMEOUT`（600s）|
| `training/scripts/train_lora.py` | LoRA 训练无 timeout | `timeout=TRAIN_TIMEOUT`（12h）|
| `skill_router/runtime.py:133` | **`_request(timeout=30)` 的 timeout 参数从未生效** —— `readline()` 无限阻塞且持锁（KI-MCP-001）| **真正实现超时**：`select.select([proc.stdout], [], [], timeout)` → 超时抛 `TimeoutError`；并补 `proc is None` 防御（顺带修掉 6 处 mypy 报错）|
| `skill_router/orchestrator.py:205/212` | `self.sessions.get(...)` 被当成 HTTP 调用 | **守卫修正**：只有导入 `requests/httpx/aiohttp` 的模块才按 session/client 判定 HTTP |

## 3. 守卫本轮的 5 处修正（都是把误判改成正解）

| 守卫修正 | 原行为 | 新行为 |
|---|---|---|
| `open(p,"a+")` 模式 | `+` 判为覆盖写 | 只有 `"w"` 是覆盖；`"a"`/`"+"` 是追加 |
| Popen 参数里的 `open()` | 判为覆盖写 | 流式写入 → 不算 SEM001 |
| 读-改-写检测 | 只要"先读后追加"就报 | 同函数有 `fcntl.flock` → 已被互斥保护，放行 |
| 管道读检测 | `readline()` 无 timeout kwarg 就报 | 同函数有 `select.select(..., timeout)` → 有超时保护，放行 |
| HTTP 判定 | 接收者名含 session/client 就当 HTTP | 仅当模块真的导入了 HTTP 库才判定 |

## 4. 工具（`scripts/sem001_migrate.py`）与踩过的坑

| 坑 | 现象 | 修法 |
|---|---|---|
| **AST 的 `col_offset` 是 UTF-8 字节偏移** | 含中文的行被切错（把下一行的注释粘到代码后）→ 语法错误 | 整段按**字节**切片再解码（`out[:start] + new.encode() + out[end:]`）|
| **导入注入位置** | 插在 docstring 之后 → `from __future__` 不再是最前 → SyntaxError；插在函数体内 → NameError | 分两处插：stdlib 段进**文件开头的连续导入块**，项目段进**第一个项目导入之前**；且只认"文件开头连续块"，不认文件中段的散落导入 |
| **中段才导入的同名模块** | `plot_loss.py` 第 23 行才 `from pathlib import Path`，但引导代码在第 19 行 → F821 | 可用性判定只看**文件开头导入块** |
| **`sys.path` 引导重复** | 部分文件已有自定义引导 → 又插一段 | AST 判定是否已有顶层 `sys.path.insert`（含 ROOT/parent/dirname 线索）|
| **多行调用带行内注释** | 拼接后语法错位 | 字节级 splice + 每文件编译校验（**批量跑完必须逐文件 `py_compile`**）|

## 5. 验证

| 判据 | 结果 |
|---|---|
| 语义维度守卫 | **SEM001 0 / SEM002 0 / SEM003 0** |
| 语义基线 | `config/semantic_baseline.json` total = **0**（新增即失败，无存量豁免）|
| 编译 | 107 个改造文件全部 `py_compile` 通过 |
| 导入冒烟 | 107 个改造文件逐个 `exec_module`，仅 2 个失败且**改动前就失败**（既有的相对导入问题）|
| ruff | 205 → 205（无新增）|
| mypy | 70 → 64（顺带收敛 6 处）|
| 门禁 | `make lint-gate` 全绿（A1/A2/A3 零失败）|
| 新增测试 | `tests/test_sem001_convergence.py`（10 项：收敛结果 + 改造器单测 + UTF-8 字节偏移回归）|

## 6. 语义维度清零后的门禁语义（重要）

语义基线为 0 意味着：**从此刻起，任何新增的原子写违规 / 吞错未计数 / 缺 timeout 都会直接失败**（A1 净增长 + A2 新增行双重拦截），不再有任何存量豁免。

这比"降到个位数"更强，但也带来两条必须记住的约束：
1. **改造器的"批量重跑"不再安全** —— 任何一次批量改造都必须带测试；
2. **语义规则的边界必须持续校准** —— 本轮修的 5 处守卫误判说明：规则一旦过宽，就会逼出"绕过守卫"而非"修好代码"，反而掩盖真问题。因此每轮收敛后都应回看：被新规则拦下的，是真问题还是规则本身需要修正。
