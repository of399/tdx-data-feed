# 工作流程逆向分析（Skill / Agent / 程序 / 调度 / 数据）

（2026-11-01 22:20 · 逆向自最终产物倒推至声明层）

> **分析对象**：`docs/archive/ARCHITECTURE_REVIEW_2026-11-01.md`（16:10 快照）+ `config/known_issues.yaml`（40 条台账）
> **方法**：自最终产物反向倒推，逐层回溯「谁产出它 → 谁触发它 → 谁声明它」，全程只读实测（git / grep / 文件行号），不复述审查结论。
> **快照基线**：git `b4ebf40` · 工作树干净 · ruff 基线 **205** · 语义基线 **0** · 台账 **40 条**
> **结论性质**：现状诊断 + 改造路线；**本次为纯分析，未改动任何生产代码**

---

## 📊 核心指标

| # | 指标 | 实测（2026-11-01 22:20） | 相对 16:10 审查快照 |
|---|---|---|---|
| 1 | 生产决策栈 | **LightGBM + 规则**（`daban` 29 维 / Top5），无 LLM 参与 | 不变 |
| 2 | 生产链路层数 | **6 层**（产物 → 调度 → 编排 → 程序 → 能力 → 声明 → 数据） | 首次显式分层 |
| 3 | 真实在役调度单元 | **system 4 组 + user 9 组**（共 20 个 timer） | 不变 |
| 4 | 技能层生产调用方 | **0 个**（`skill_router` 无 systemd / Makefile / MCP 引用） | 审查只说"可触发未触发"，实测为**孤岛** |
| 5 | 技能 yaml 真实消费字段 | **8 / 15**（7 个顶层 key 为死字段） | 审查说 6 / 全量，**精确到 7 死 key** |
| 6 | MCP 工具声明 / 被调 | **23 声明 / ~15 被调**（8 个为死引用） | 新增实测（审查只数 server） |
| 7 | 语义门禁基线 | **0**（SEM001 194→0 · SEM002 349→0 · SEM003 8→0） | ↑ 从 570 → 0 |
| 8 | ruff 门禁基线 | **205**（原 252） | ↓ 收紧 47 条 |
| 9 | 台账状态 | **40 条**：FIXED 13 / OPEN 26 / DEFERRED 1 | ↑ FIXED 6 条 |
| 10 | 无自动守卫的 OPEN 条目 | **18 / 26**（69%） | 审查时 29/36 |
| 11 | scripts→scripts 真耦合边 | **11 条**（`_common` 桥不计） | 审查说 13，**实为 11** |
| 12 | 系统级单元 `OnFailure` | **0 个**（`systemd/system/` 全目录 grep 无命中） | **本轮新暴露** |

---

## 🧭 一、逆向分析总览：从产物倒推到声明

### 1.1 逆向倒推链（答案在最上，问题在最下）

```
┌─ 逆 ────────────────────────────────────────────────────────────────────┐
│ 【L0 最终产物】用户看到的东西                                                │
│   data/predictions/predict_latest.md      每日 Top5 榜单（人工抄进券商 App）      │
│   data/daban/tickets/ticket_{date}.md     人工委托单文本（不下单）                  │
│   data/plan_e/plan_e_today.csv            方案E 盘前研究清单                     │
│   data/plan_e/live/journal.csv            实盘记账台账（人工）                     │
│   logs/canary_history.jsonl               9 维巡检时序                          │
├─ 追问：谁写的？
│   scripts/predict.py · scripts/daban_entry.py · scripts/plan_e_backtest.py      │
│   scripts/plan_e_journal.py · scripts/canary_health_check.py                    │
├─ 追问：谁触发的？
│   systemd/predict-daily.timer  systemd/daban-ticket.timer  systemd/canary-health.timer
├─ 追问：数据从哪来？
│   systemd/system/tdx-sync.timer(16:30) → tdxfeed.full_sync → tdxfeed.adj_factor    │
│   → tdxfeed.hfq_factor → tdxfeed.precompute.indicators                            │
├─ 追问：数据存在哪？
│   data/parquet/daily/ · data/adj_factor/ · data/hfq_factor/ · data/parquet/derived/ │
├─ 追问：谁来保证它没坏？
│   scripts/ci_violation_gate.py + scripts/ci_semantic_guard.py + scripts/audit_ledger.py │
│   + scripts/audit_fix_tests.py + scripts/ci_typecheck_gate.py + tests/test_*.py      │
├─ 追问：上面这些能力，从哪"声明"来的？
│   skills/*.yaml(9) · .codebuddy/agents/*.md(4) · .codebuddy/skills/*/SKILL.md(3)     │
│   ⚠️ 声明层与执行层在此处断裂（详见 §3.5 / §4 裂缝 A）                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 六层环节清单与职责边界

| 层 | 名称 | 载体 | 职责 | 与上层的契约 | 与下层的契约 |
|---|---|---|---|---|---|
| **L0** | 产物层 | `data/predictions/`、`data/daban/tickets/`、`data/plan_e/`、`logs/canary_history.jsonl` | 承载人类可见的唯一事实 | 文件名 = 时间戳 + 口径 | 无 |
| **L1** | 调度层 | `systemd/`（user 20 unit + `systemd/system/` 20 unit） | 决定"什么时候跑" | `OnCalendar` / `ExecStart` | **跨 scope 无序**（system → user） |
| **L2** | 编排层 | `Makefile`（80 target）+ `.github/workflows/`（门禁链） | 决定"手动怎么跑" + "CI 挡什么" | target 名 vs service 名 | **口径多处不一致** |
| **L3** | 程序层 | `scripts/`(147) + `tdxfeed/`(68) + `quant/`(15) | 真正的业务计算与落盘 | CLI 参数 | `tdxfeed/` 单向依赖，无反向 |
| **L4** | 能力层 | `skill_router/`(3) + `mcp/`(6 server / 23 tool) | 面向自然语言的意图 → 数据查询 | `skills/*.yaml` 声明 | 经 MCP 读 `data/` |
| **L5** | 声明层 | `skills/*.yaml`(9) + `.codebuddy/agents/*.md`(4) + `.codebuddy/skills/*/SKILL.md`(3) | 告诉人和 LLM "怎么用" | 无 | **不被 L4 执行** |
| **L6** | 数据层 | `data/parquet/`、`data/adj_factor/`、`data/hfq_factor/`、`data/.state/` | 事实的唯一真相 | schema FROZEN | `parquet_io.write_table` 唯一写入口 ✅ |

### 1.3 依赖关系矩阵（谁依赖谁）

| 下游 \ 上游 | L6 数据 | L3 程序 | L4 能力 | L1 调度 | L2 编排 | L5 声明 |
|---|---|---|---|---|---|---|
| **L0 产物** | ← L3 | ← L3 | — | ← L1 | ← L2（部分） | — |
| **L1 调度** | — | ← L3 | **0 引用** | — | 部署时手工 cp | — |
| **L2 编排** | ← L3 | ← L3 | **0 引用** | 口径需对齐 | — | — |
| **L3 程序** | ← L6 | ← L3（11 条真边） | ← L4（MCP） | ← L1 | — | — |
| **L4 能力** | ← L6（经 MCP） | — | — | — | — | ← L5（**但只消费 8/15 字段**） |
| **L5 声明** | — | 引用脚本名（9 处失效） | — | 引用退役单元（4 处失效） | 引用 Makefile 目标（1 处行号漂移） | — |

> **关键结构事实**：L4 与 L5 之间、L1/L2 与 L4/L5 之间，各有一条**断裂的连线**。L4/L5 整层是**孤岛**——声明完备、执行缺席。

---

## 🎯 二、环节逐层实测（职责 / 依赖 / 现状）

### 2.1 L0 产物层：命名规则与原子性

| 产物 | 命名规则 | 写入方式 | 原子性 |
|---|---|---|---|
| 全市场预测 | `data/predictions/predict_{YYYYMMDD_HHMMSS}.csv` | `atomic_write_frame` | ✅ |
| Top-N 榜单 | `predict_top{N}_{ts}.csv` | 同上 | ✅ |
| 阈值高分 | `predict_high_{ts}.csv` | 同上 | ✅ |
| Markdown 报告 | `predict_{ts}.md` + `predict_latest.md` | `atomic_write_text` | ✅ |
| 方案E 今日清单 | `data/plan_e/plan_e_today.csv` | `atomic_write_frame`（KI-ATOMIC-001 已修） | ✅ |
| 实盘账本 | `positions.json` / `journal.csv` | 原子写 + `flock`（KI-ATOMIC-002/003 已修） | ✅ |
| 打板委托单 | `data/daban/tickets/ticket_{date}.{json,md}` | `tdxfeed/daban/trial.py` 自实现 `_atomic_write` | ✅（但**重复实现**） |
| 翻倍样本 | `data/double_up/double_up_2020_2026_detail.csv` | `find_double_up_c.py` 自实现 `write_atomic` | ✅（重复实现） |
| 巡检历史 | `logs/canary_history.jsonl` | 追加 | ✅ 语义正确 |
| 同步状态 | `data/.state/manifest.json` | tmp→replace，但**每票重写** O(n²) | ✅ 形式对，❌ 性能 |
| 吞错计数 | `data/quality/swallowed_counts.jsonl` | `atexit` 追加，5 个生产入口 | ✅ |

> **小结**：P0 级原子写（KI-ATOMIC-001~004）已全部关闭，产物层从"读者可能读到半截"变为"全部原子落位"。剩余问题退化为**重复实现**（2 处自实现 atomic helper）与**裸写残留**（8 处研究脚本裸 `json.dump`）。

### 2.2 L1 调度层：真实每日时序与断点

```
工作日 16:30  [system] tdx-sync.timer
                └─ tdx-sync.service  full_sync → indicators → adj_factor → hfq_factor
                   （3 条 ExecStartPost 全带 `-` 前缀 = 失败不判红）
                   ⚠️ 无 OnFailure · ⚠️ 无完成哨兵
                              │
                              ╳ 跨 manager 无序（system → user 无法 After=）
                              ▼
工作日 18:05  [user]  predict-daily.timer
                └─ ExecStartPre=precheck_freshness.sh（exit 2 拦整轮）
                   ⚠️ 无 TimeoutStartSec（user manager 默认 90s）
                └─ predict.py --position-mode small --threshold 0.7 --workers 8
                              ▼
工作日 18:10  [user]  plan-e-journal.timer   (After/Wants=predict-daily.service ✅)
工作日 18:30  [user]  plan-e-double-up.timer  ⚠️ After=tdxfeed-sync.service（已退役=死依赖）
工作日 08:45  [user]  plan-e-today.timer      (Persistent=false，防补跑成盘后榜)
工作日 09-14  [user]  daban-ticket.timer      每 5 分钟（TimeoutStartSec=180 防重叠）
每 5 分钟      [user]  canary-health.timer    9 维巡检
```

**实测断点（4 处）**：

| 断点 | 证据 | 后果 |
|---|---|---|
| **跨 scope 无序** | `systemd/predict-daily.service` 无 `After=`/`Requires=` 指向 system 级 `tdx-sync`；`systemd/system/tdx-sync.service` 无 `OnFailure`、无 `sync-done` 哨兵 | 全市场预测可能基于**半新半旧**数据出榜 |
| **system 级告警黑洞** | `systemd/system/` 全目录 `OnFailure` grep **0 命中**；`scripts/onfailure_alert.py` 只查 `--user` scope | `tdx-sync`/`tdx-monitor`/`tdx-verification` 失败只能靠 canary 5 分钟后发现 |
| **死依赖** | `systemd/plan-e-double-up.service:4` `After=tdxfeed-sync.service`（该单元 10-28 已退役） | 18:30 翻倍样本重建不再等任何同步 |
| **超时缺失** | `systemd/predict-daily.service` 无 `TimeoutStartSec` | 5221 只 × 8 进程卡在 90s 默认值边缘 |

**已修复的调度正确性问题**（审查后新增）：
- `plan-e-double-up.service` 的 `OnFailure` 已从 `[Service]` 移到 `[Unit]`（10-24 曾导致告警一次没发过）✅
- `plan-e-today.timer` 刻意 `Persistent=false`，补跑不会把盘前榜变成盘后榜 ✅
- `daban-ticket.service` 有 `TimeoutStartSec=180`，防两轮重叠打穿去重 ✅
- `tdx-sync.service` 的 `TimeoutStartSec=21600`（6h）已按 adj/hfq 实测耗时重设 ✅

### 2.3 L2 编排层：Makefile 口径与生产真源

| target | 生产真源 | 一致性 | 差异 |
|---|---|---|---|
| `full-sync` | `tdx-sync.service` | ✅ | 命令一致 |
| `plan-e-today` / `journal` / `analyze-double-up` / `canary` | 各 service | ✅ | 一致 |
| `data-adj` | `tdx-sync.service` ExecStartPost | ⚠ | Makefile 前台执行会中断；生产是 `-` 前缀不判红 |
| **`predict`** | `predict-daily.service` | ❌ | Makefile `--top 30` vs 生产 `--position-mode small`（**Top5**）；且**无新鲜度守卫** |
| **`data-sync`** | — | ❌ | 只同步 `sh600000` 单标的，易被误当全市场 |
| **`setup`** | — | ❌ | 含 `data-sync`（单标的） |
| **`pipeline`** | — | ❌ | 用 `data-sync` 而非 `full-sync` + 含无关的 `deploy-all` |
| **`ci`** | `.github/workflows/` | ❌ | `ci = ci-gate + canary`，canary 查 Ollama/GPU/磁盘 → **CI 机器必红** |
| `ci-gate` | CI | ✅ | 纯门禁链（lint + format-check + lint-pipeline + gate + typecheck-gate + fix-tests-check + test） |

> **小结**：M-6 已把 `format` 改为 `format-check`（不改工作树）、`lint` 扩到全仓、`lint-pipeline` 补 exit 1、mypy 走基线门禁 —— 但 `ci` 目标仍混入 `canary`，这是**宿主健康检查被当成 CI 门禁**的口径错配（CI 与 canary 应是两个正交的东西）。

### 2.4 L3 程序层：写入口与耦合

**写入口实测**（关键结论：**行情表已是单源**）：

| 类别 | 入口 | 覆盖 |
|---|---|---|
| 行情 parquet | `tdxfeed/parquet_io.write_table`（date32 + 换 inode） | `writers.merge_write` / `precompute.indicators` / `incremental` / `download_universe` / `migrate_v1_to_v2` ✅ 有测试守护 |
| 复权因子 | `adj_factor.py` / `hfq_factor.py` 自实现 tmp→replace | 旁路（手工 date 转换，**不属 write_table 契约**，符合项目约定） |
| CSV/JSON/MD 产物 | `tdxfeed/atomic.py`（4 个函数） | 覆盖 12+ 生产脚本 |
| 残留裸写 | 8 处研究脚本 | `plan_e_v3_live_track.py` / `audit_daily_history_depth.py` / `download_valuation.py` / `factor_engine.py` / `convert_distill.py` / `backfill_hfq_history.py` 等 |

**耦合实测（修正审查数字）**：

- `scripts → scripts` 真边 **11 条**（审查说 13）：

```text
plan_e_backtest  ← compare_plan_e_signals / oracle_plan_e_ceiling / llm_signal_plan_e
                 ← plan_e_validation / plan_e_subset_backtest / plan_e_confidence（6 条，事实上的第二库）
predict         ← plan_e_sim_live（唯一把 predict.py 当库用）
train_daban_model ← diagnose_sample_design / compare_daban_vs_stage1 / eval_daban_pattern（3 条）
llm_signal_plan_e ← plan_e_confidence（二级边 1 条）
```

- `scripts → _common` 的 70+ 条**不算耦合**（`_common` 只是 `tdxfeed.*` 的再导出桥，零逻辑）。
- `sys.path.insert`：**73 个文件**（审查说"约 85 处"，实测文件数 73）。

### 2.5 L4 能力层：skill_router + MCP 实测

**入口链（逆向）**：

```
python -m skill_router.cli  →  os.chdir(项目根)  →  Orchestrator.__init__
   →  SkillRuntime.load_all_skills()      （读 skills/*.yaml 9 个）
   →  Orchestrator.route()                 （硬编码 INTENT 表，**不读 yaml**）
   →  Orchestrator.handle() → extract_code()
   →  SkillRuntime.execute_skill()
        ├─ LLM 分支  _execute_with_llm  → prompt → Ollama/transformers → fallback
        └─ 纯工具分支 _execute_pure_tools → call_mcp → McpClient → subprocess → select 超时
   →  orchestrator 二次降级 indicator-match（**重复执行**）
   →  fmt_result()（CLI 文本化）
```

**生产调用方：0 个** —— `systemd/` 全目录 grep `skill_router|orchestrator` **0 命中**；`Makefile` **0 命中**；`mcp/` 仅文档提及。唯一程序化调用者是 `tests/test_theory_reference.py`。

**yaml 字段消费映射（15 顶层 key 实测）**：

| 状态 | 字段 | 证据 |
|---|---|---|
| ✅ 真实消费 | `name` | `runtime.py:218, 469, 785, 953` |
| ✅ | `llm.model_id` | `runtime.py:455-456`（决定走 LLM 还是纯工具） |
| ✅ | `llm.ollama_model` / `llm.inference.max_new_tokens` | `runtime.py:1000` / `:931` |
| ✅ | `prompt_template` | `runtime.py:888` |
| ✅ | `fallback_skill` | `runtime.py:954-963` |
| ✅（单 skill） | `decision_logic.weight_table/threshold` | `runtime.py:538-539`（仅 indicator-match） |
| ✅（单 skill） | `book_paths` | `runtime.py:731-732`（仅 theory-reference） |
| ❌ **死字段** | `version` `description` `tools` `input_schema` `output_schema` `priority` `timeout_sec` `metrics` | 9/9 yaml 全有，代码 **0 读取** |
| ❌（单例） | `freshness`（仅 news-summary） | 缓存实际在 MCP server 内部实现 |
| ❌（llm 子键） | `base_model` `adapter_path` `inference.backend/dtype/device_map/temperature` | 只从 CLI 参数来 |

**路由实测**：

- 顺序：theory（**无条件前置**）→ 6 条 INTENT 正则 → 兜底 `indicator-match`。
- 6 条 INTENT 正则**全部无 `re.IGNORECASE`** ⇒ 大写 `PE`/`PB`/`SH600000` 不命中；而 `CODE_RE` 与 `extract_code` **带** IGNORECASE ⇒ **大小写策略自相矛盾**（大写代码"能取码但走错分支"）。
- `extract_code` 只认 `sh|sz|bj` 前缀 ⇒ **裸 6 位（`600519 怎么看`）最终报错**"指标分析需要股票代码"。
- `backtest` 与 `financial-statement` 两个 skill **无路由入口**。
- theory 前置抢答："MACD 和 KDJ 的区别" → `theory-reference`。

**MCP 层实测**：

- 6 server / **23 tool**，与 `mcp/schemas/tools.json` 的名称与顺序 **6/6 一致**（`tests/test_mcp_contracts.py:73-76` 已锁定）。
- **但 `description`/`inputSchema` 已漂移**（例：`calc_macd` 的 `golden_cross` 语义在代码与 json 里不同；`read_xdxr.code` 有无 description 不一致）。
- **运行时零参数校验**：`jsonschema` 只出现在测试里（校验 schema 自身合法），server 运行时直接 `TOOLS[tool][handler](**args)` ⇒ `required`/`enum` 从不执行，非法参数变成 `-32603` 内部错误。
- `parquet_reader` 的 `code` **无格式校验** ⇒ `code="../../x"` 可越出数据目录。
- 客户端**已实现真超时**（`select.select` + `TimeoutError`，KI-MCP-001 已修），但超时值**硬编码 30s**，yaml 的 `timeout_sec` 无效；子进程**无健康探测、无自动重启**。
- yaml 里声明但 runtime 从不调用的 MCP 引用 **≥8 个**（`calc_adj_factor`/`list_codes`×2/`list_cats`/`market_news`/`list_strategies`/`calc_boll`/`calc_kdj`/`search`）。

### 2.6 L5 声明层：4 Agent + 3 SKILL + 9 yaml

| 载体 | 职责 | 失效引用实测 |
|---|---|---|
| `.codebuddy/agents/tdx-predictor.md` | 每日全市场预测 | **0**（参数、模型文件、timer 全部存在） |
| `.codebuddy/agents/tdx-backtester.md` | 方案E 回测 / 实盘 journal | **0** |
| `.codebuddy/agents/tdx-data-collector.md` | A 股数据采集 | **4**：`trainsh-health.timer`、`download_etf_bond.py`、`data/missing.txt`、`data/missing_min15.txt` |
| `.codebuddy/agents/tdx-model-trainer.md` | LoRA/QLoRA 训练与部署 | **5**：全部指向已不存在的 v7 checkpoint / merged 模型 / GGUF；`Modelfile-v7` 的 `FROM` 悬空 |
| `.codebuddy/skills/tdx-quant-workflow/SKILL.md` | 5 阶段端到端流程剧本 | **3**：`agents/orchestrator.py`（真实为 `skill_router/`）、"skills 有 8 个"（实为 9）、v7 checkpoint |
| `.codebuddy/skills/tdx-obsidian-version-control/SKILL.md` | 两库 git 化 | 文件级 0；`obsidian-git.json` 内联副本与源不一致（commitOnClose true/false） |
| `.codebuddy/skills/tdx-obsidian-math-verify/SKILL.md` | 数学库零漂移校验 | **2**：`make verify-math` 的 env 名与 Makefile 实际读取的 `VAULT/VAULT_KIND/PREFIX` 不一致（**静默校验另一个库**）；Makefile 行号漂移 |
| `skills/*.yaml`(9) | 运行时技能契约 | 7 死字段 + 8 死 MCP 引用 |

> **结构判断**：Agent 层与 SKILL 层是**给人和 LLM 看的操作手册**，其描述的量化链路（`predict.py` / `plan_e_backtest.py`）与 L4 的 LLM 技能链是**两条互不相通的执行路径**。L5 → L3 的引用全是**按名字猜路径**，没有任何机制验证目标存在。

---

## 📈 三、统计分布

### 3.1 台账 40 条：状态 / 严重度 / 守卫覆盖

| 状态 | 条数 | 占比 |
|---|---:|---|
| FIXED（已修 + 已绑守卫） | 13 | 33% |
| OPEN | 26 | 65% |
| DEFERRED | 1 | 2% |

| 严重度 | OPEN 条目数 | 其中无自动守卫 |
|---|---:|---:|
| **P0** | 5 | 4（KI-COMPETE-001 / KI-MCP-003 / KI-OPS-001 / KI-OPS-002） |
| **P1** | 14 | 11 |
| **P2** | 7 | 3 |
| 合计 | **26** | **18（69%）** |

### 3.2 质量门禁收敛轨迹（2026-11-01 当日）

```
语义维度（AST 守卫）
  SEM001 原子写      213 ▸ 194 ▸ 0   ← 4 批，生产链路优先
  SEM002 吞错必计数   349 ▸ 73+204+72 ▸ 0
  SEM003 timeout        8 ▸ 0
  语义基线 total     570 ▸ 551 ▸ 202 ▸ 0   ← 新增即失败
ruff 维度
  完整规则集存量      252 ▸ 205              ← 增量阻断，只降不升
mypy 维度
  存量类型错误         70 ▸ 64               ← 新增模块走基线门禁
台账守卫覆盖
  无守卫条目          29 ▸ 21 ▸ 18/26
```

### 3.3 三条裂缝的**刷新后**现状（对比 16:10 快照）

| 裂缝 | 审查时状态 | **现在（22:20）** | 结论 |
|---|---|---|---|
| **A 声明层脱钩** | 6 个死字段 / 路由不可用 / Agent 指退役单元 | 死字段精确到 **7 个顶层 key**；`select` 超时已修；新增实测：**skill 层零生产调用方**、**MCP 8 个死引用**、**system 级零 OnFailure** | **未改善**，范围反而更清晰 |
| **B 写入竞态** | 4 个产物非原子、训练覆盖现役模型 | 4 个产物**全部原子化**（KI-ATOMIC-001~004 FIXED）；**竞态哨兵未落地**；`plan-e-double-up` 死依赖仍在 | **产物层已闭合，调度层未动** |
| **C 观测黑洞** | Prometheus 抓错 canary、`onfailure` 只查 user scope | **未动**；新增确认 `systemd/system/` 零 OnFailure | **完全未改善** |

---

## ⚠️ 四、现状与问题分析（结论）

1. **生产主链路是健康的，但健康的是"产物层"和"程序层"。** 原子写、门禁、吞错计数三项已闭合（语义基线 0、ruff 205 只降不升、40 条台账 13 条 FIXED），这些是 2026-11-01 当日真正的增量。

2. **调度层与观测层是当前最大的未修区域，且两条 P0 正在生效：**
   - `predict-daily`（18:05）与系统级 `tdx-sync`（16:30 起、最长 6h）跨 manager 无序，且**没有完成哨兵** ⇒ 半新半旧数据出榜的可能无法被察觉；
   - 系统级单元**零 `OnFailure`** + `onfailure_alert.py` 只查 `--user` ⇒ `tdx-sync` 失败在 canary 的 5 分钟窗口之外是彻底黑洞。

3. **声明层（L5）是"看起来完备、实际不生效"的典型。** 7 个死字段让 yaml 变成装饰；`skill_router` 整层无生产调用方；MCP 8 个声明引用从不被调；Agent/SKILL 共 14 处失效引用。**维护者按文档做会做错**——这是审查文件的核心判断，本轮实测完全证实，并补充了"整层孤岛"这一更严重的结构事实。

4. **能力层的"能跑"不等于"正确"：** MCP 零运行时校验（`required`/`enum` 从不执行）、`parquet_reader.code` 可路径穿越、yaml 的 `timeout_sec` 全部无效、客户端无健康探测与自动重启。

5. **编排层与生产真源存在系统性偏差**：`make predict` 出 Top30 而生产出 Top5 且生产有新鲜度守卫而 Makefile 没有；`make pipeline` 用单标的 `data-sync` 顶替 `full-sync`；`make ci` 混 `canary` ⇒ CI 机器必红。

6. **重复实现仍是口径漂移的温床：** `norm_code` 3 份、`is_st` 3 种语义、`load_universe` 7 份、涨停判定 9 处、`sys.path.insert` 73 文件、`plan_e_backtest` 11 条真耦合边（审查的 13 条含 `_common` 桥，实为 11）。

7. **文档漂移已修正一部分，但新增两处语义级失效：** `agents/` → `skill_router/` 目录引用仍存在；`tdx-obsidian-math-verify` 的 env 名与 Makefile 不一致 ⇒ **静默校验了另一个库**（比"写错数字"危险一档，它成功但校验错对象）。

8. **本轮对审查文件的一处纠正：** 审查 W4-6 称 `plan_e_backtest.py` 没有 `--hold/--tp/--sl` 参数 —— 实测**存在**（`scripts/plan_e_backtest.py:1751-1753, 1756, 1763, 1765, 1770`）。该条应从"文档漂移"清单中移除。

---

## 🔧 五、改造项列表（重点与优先级）

> 编号沿用审查文件 W 序列以便对照；状态以 **2026-11-01 22:20** 实测为准。
> 优先级依据：**是否正在造成错误结果 > 是否使失败不可见 > 是否使声明误导 > 是否产生重复维护成本**。

### 5.1 P0 — 正在造成错误或不可见（本周内，共 5 项 ≈2.5 人日）

| # | 改造项 | 改造重点 | 验收判据 | 台账 | 依赖 |
|---|---|---|---|---|---|
| **W1-1** | **同步完成哨兵（消除 predict/sync 竞态）** | ① `systemd/system/tdx-sync.service` 的 `ExecStartPost` 末尾 `touch data/.state/sync-done`（含退出码，失败不写）；② `systemd/predict-daily.service` 加 `ExecStartPre` 校验哨兵存在且 mtime 在 N 小时内，不足则 **exit 2 跳过出榜**（复用现有 `precheck_freshness.sh` 的 exit 2 语义）；③ **不动时间窗**，先验证再议 | 日志中 predict 启动 **≥** sync 完成；哨兵缺失时 predict **不产出**而非产出脏榜 | KI-COMPETE-001 | 需 root 改 system 单元 |
| **W1-3** | **系统级告警通道（闭合黑洞）** | ① `systemd/system/tdx-sync.service` / `tdx-monitor` / `tdx-verification` 补 `OnFailure=`；② `scripts/onfailure_alert.py` 加 `--scope {user,system}`，系统级用 `systemctl show`（不带 `--user`）；③ 新增 `systemd/system/onfailure-alert-system@.service`（勿与 `pushplus_bridge` 的 5002 端口抢） | 系统级单元失败能收到微信；载荷不再是 `?` | KI-OPS-002 | 需 root |
| **W1-8b** | **parquet_reader code 路径穿越** | 三个工具统一走 `_norm_code` 正则（其余 server 已有现成实现），在 `tools/call` 分支统一拦截 | `code="../../x"`、`code="/abs/path"` 返回 `-32602` | KI-MCP-003 | 无 |
| **W1-2** | **timer-install 白名单补 `obsidian-sync`** | `Makefile` 新增 `USER_PATHS := obsidian-sync.service obsidian-sync.path` 纳入 `timer-install`（**service 先于 path**）；新增 `RETIRED_PATHS := tdxfeed-sync.path` 纳入 `timer-disable` | `make timer-install` 后 `.path` 与仓库一致；canary 的 `obsidian_sync` 不再"提示修复动作无效" | KI-OPS-001 | 无 |
| **W1-7** | **`TimeoutStartSec` 全量显式化** | `predict-daily` 加 `TimeoutStartSec=1800`；`plan-e-journal` / `canary-health` / `plan-e-weekly` 同样显式给值 | 全市场预测不再卡在 user manager 默认 90s | KI-OPS-003 | 无 |

### 5.2 P1 — 让声明与执行对齐（2 周内，共 6 项 ≈6 人日）

| # | 改造项 | 改造重点 | 验收判据 | 台账 | 依赖 |
|---|---|---|---|---|---|
| **W2-6** | **`skill_router` 回归测试（**必须最先做**）** | ① `tests/test_skill_router_route.py`：路由参数化（裸 6 位 / 大写 / theory 抢答 / 无入口 skill）；② `tests/test_skill_router_contracts.py`：yaml 字段 → runtime 消费映射，断言**无死字段**（改造前会红，用来锁死改造结果）；③ `tests/test_mcp_client.py`：fake server 测超时 / 断开 / 重启 | 改 orchestrator 行为时测试先红；**作为 W2-1/W2-2 的前置** | KI-SKILL-001 | — |
| **W2-2** | **yaml 变真契约** | ① `execute_skill` 入口按 `input_schema` 做 required/type 校验；② `_request` 超时取 `skill["timeout_sec"]`（删硬编码 30s）；③ 输出按 `output_schema` 校验，不合规记 `output_format_compliance=false`；④ `route()` 改读 yaml `priority`，消除两套优先级表 | 改一个 yaml 的 `timeout_sec` 真实影响超时；改 `priority` 真实影响路由顺序 | KI-SKILL-002 | **W2-6 之后** |
| **W2-1** | **路由可预期化** | ① `extract_code` 支持裸 6 位并按首位补 sh/sz（复用 `mcp/servers/news.py` 规则）；② theory 判定**下移到所有业务意图之后**且仅 `not code` 时生效；③ 6 条 INTENT 正则统一加 `re.IGNORECASE`；④ 拆出"报表/资产负债/现金流"给 `financial-statement`；⑤ 新增 `backtest` 意图 | "600519 怎么看" / "PE和PB区别" / "MACD和KDJ区别" / "查资产负债表" 四用例全部命中预期 skill | KI-SKILL-002 | **W2-6 之后** |
| **W1-8a** | **MCP 运行时 schema 校验** | 抽共享 `_validate(schema, args)`（`jsonschema` 已是测试依赖）在 `tools/call` 分支调用；业务错误改 `result:{isError:true}`，JSON-RPC error 只留"未知方法/工具"；补 `ping` | 非法参数返回 `-32602` 而非 `-32603` | KI-MCP-002 | — |
| **W1-1b** | **死依赖清理** | `systemd/plan-e-double-up.service` 的 `After=tdxfeed-sync.service` 改为哨兵校验（与 W1-1 同源）或直接指 system 级单元；`systemd/predict-daily.timer` 注释里的"tdxfeed-sync 18:00"同步更正 | 无任何单元引用已退役单元名 | KI-COMPETE-002 | W1-1 |
| **W4-4** | **Makefile 口径对齐生产** | ① `make predict` 对齐 `--position-mode small --workers 8` + `precheck_freshness.sh`；② `make pipeline` 的 `data-sync` 换 `full-sync`、去掉 `deploy-all`；③ `make ci` 去掉 `canary`（canary 是宿主健康，不是 CI 门禁）；④ `journal-buy/sell` 移入 `make-legacy` 或加二次确认；⑤ `setup` 的 `data-sync` 换 `full-sync` | 手工跑出的东西 = 生产跑出的东西 | KI-MAKE-001/002 | — |

### 5.3 P1 — 收敛重复实现（3 周内，共 4 项 ≈5 人日）

| # | 改造项 | 改造重点 | 验收判据 | 台账 |
|---|---|---|---|---|
| **W3-3** | **涨停判定单源** | 全部改走 `tdxfeed/execution/limits.py` 的 `is_limit_up_ret`；删 `features_5d.py` 的 `LIMIT_MAP` 改为派生 | 全仓 `limit - 0.001` 内联 **0 处**（已有守卫可复用） | KI-SINGLE-004 |
| **W3-5** | **名字/池单源** | `norm_code`（3 份）/`is_st`（3 种语义）/`load_universe`（7 份）收敛到 `tdxfeed/names.py` + `tdxfeed/paths.py`；扩展现有 AST 单源守卫覆盖 `norm_code` / `is_st` | 全仓各实现数 **= 1**（守卫断言） | KI-SINGLE-001/002/003 |
| **W3-6** | **`state.py` 批量落盘** | `State.update` 只改内存；每 200 只或退出时落盘（保留 tmp→replace） | 5500 只同步的 manifest 写盘从 ≈44s / 1.8GB 降到 <2s | KI-PERF-001 |
| **W3-1** | **`plan_e_backtest` 拆库** | 公共部分迁 `tdxfeed/execution/` 或新建 `tdxfeed/plan_e/`；保留薄壳；**先做 W2-6 的测试再动** | scripts→scripts 真边 **11 → ≤3** | KI-IMPORT-001 / KI-SIZE-001 |

### 5.4 P2 — 性能、文档与长期治理（并行，共 6 项 ≈5 人日）

| # | 改造项 | 改造重点 | 台账 |
|---|---|---|---|
| **W3-2** | **editable install** | 加 `pyproject.toml` + `pip install -e .`；只保留 `scripts/_common.py` 一处引导；同步验证 systemd `ExecStart` 的 PATH | KI-PATH-001 |
| **W3-7** | **`find_double_up_c` 向量化** | `rolling(21).max().shift(-1)`；`screen_one` 进程池化（用现有 CSV 做 golden 对比） | KI-PERF-002 |
| **W3-8** | **`predict.py` IO 优化** | `read_parquet(columns=...)` + 只读尾部 N 行 | KI-PERF-003 |
| **W4-6** | **文档漂移批量修正** | `agents/`→`skill_router/`（4 处）；timer 清单实为 12 timer + 2 path；`make verify-math` 的 env 名对齐 Makefile 的 `VAULT/VAULT_KIND/PREFIX`（**校验错库比写错数字更危险**）；`obsidian-git.json` 改为运行时 `cat` 而非内联副本；**移除"plan_e_backtest 无 --hold/--tp/--sl"的错误说法** | KI-DOC-001 / KI-OBS-001 / KI-OBS-002 |
| **W4-8** | **防复发 CI 护栏** | 新增 `tests/test_docs_consistency.py`：① SKILL/agent md 提到的 `scripts/*`、systemd 单元真实存在；② Makefile 行号引用不越界；③ `skills/*.yaml` name 集合 == runtime 加载集合 == 文档表格条目数；④ yaml 的 `mcp:` 引用 ⊆ `tools.json`（**且被 runtime 真正调用**） | KI-SKILL-001 / KI-DOC-001 |
| **W4-1/2** | **生产 canary 指标化** | exporter 读 `logs/canary_history.jsonl` 暴露 `tdx_canary_check_status{check=...}`；Prometheus job 改名 `math-canary` 消歧；新增 `check_user_units()` 遍历 `ExecMainStatus` | （审查 W4，尚未登记台账） |

### 5.5 待决策（需要你拍板，不由我代决）

| # | 决策项 | 现状 | 建议 |
|---|---|---|---|
| D1 | **容器栈定位** | `docker-compose.yml` 引用的 `tdxfeed.scheduler` **不存在** ⇒ 必然崩；Dockerfile COPY 的 `requirements-extra.txt` 不存在；11434 与宿主 Ollama 冲突 | **演示化**（compose 加 `profiles:[demo]`、改端口、换成真实入口）而非生产化（生产化 ≥1 周） |
| D2 | **执行研究线是否归档** | `overall_go=false` 已定，但 `tdxfeed/daban/`(7 模块) + `scripts/daban_*.py`(19 脚本) 仍在主仓，每轮审计都被"重新发现" | 台账已 DEFERRED 至 12 月复评；若无复评计划则归档并登记替代资产位置 |
| D3 | **skill 层去留** | `skill_router` 整层（1481 行 + 6 MCP server / 23 tool）**零生产调用方**；但 MCP 的 `daban_top_picks` 被 `skills/daban.yaml` 声明、技能路由器 `skills/*.yaml` 又被 LLM 客户端消费 | 二选一：① 接上生产（至少让 `daban` 技能可从 CLI/IDE 触发）② 归档为研究资产，**不要维持"看起来在用"的状态** |
| D4 | **skill 层"接生产"的具体形态** | 若选 ①，建议优先接 `daban`（与 `predict.py` 同源、3-4 秒、已有 MCP 工具），而不是接 `theory-reference`（依赖已止损的 LLM 线） | — |

### 5.6 优先级矩阵（影响 × 工作量）

```
高影响 · 低工作量（本周内，建议立刻排）
  W1-2 timer-install 白名单   W1-7 TimeoutStartSec   W1-8b 路径穿越
  W4-6 文档漂移(仅 env 名与目录名两项)   W4-4 make ci 去 canary
        └──────────────────────────────► 立即排（≈1 人日）

高影响 · 高工作量（需排期 + 配套测试）
  W1-1 同步哨兵   W1-3 系统级告警   W1-8a MCP 校验
  W2-1 路由重排   W2-2 yaml 真契约   W3-1 plan_e 拆库
        └──────────────────────────────► W2-6 测试先行，再同批推进

低影响 · 低工作量（随缘）
  W3-6 state 批量落盘   W3-7 向量化   W3-8 IO 优化   W4-8 文档一致性测试

低影响 · 高工作量（暂缓 / 待决策）
  W3-2 editable install   D1 容器栈生产化   plan_e_backtest 完整拆分
```

---

## ⚠️ 六、风险提示

1. **本次是分析不是修复**：上表所有改造项**均未实施**。当前仍在生效的 P0 是 **KI-COMPETE-001（数据竞态）**、**KI-OPS-002（系统级告警黑洞）**、**KI-MCP-003（路径穿越）**、**KI-OPS-001（告警死循环）** —— 前两项在今天这个交易日全程生效。
2. **W1-1 哨兵方案会改变产出节奏**：同步没跑成 → 当天**不出榜**。这是正确行为（脏榜比没榜更危险），但需要提前约定"没榜"的沟通口径。
3. **W1-3 动系统级单元需 root**，且 `pushplus_bridge` 已占用 5002 端口 —— 新增系统级告警单元若抢端口会导致**告警通道与告警员同时失效**（比现在的黑洞更隐蔽）。
4. **W2-2 让 yaml 变真契约会立刻暴露存量 yaml**：`input_schema` 里存在 runtime 根本不消费的字段（如 `news-summary.topics`），一旦开始校验，**存量 yaml 会集体变红**，需安排一次"yaml 与实现对齐"的批量修，不能只加校验不加对齐。
5. **W3-1 / W3-2 是大手术**：`plan_e_backtest` 有 6 个消费者，`sys.path` 改造涉及 systemd `ExecStart` 的 PATH，**必须与 W2-6 的测试同批推进**。
6. **数字会漂移**：本文所有数量均为 2026-11-01 22:20 快照（git `b4ebf40`）。本项目已连续发生"文档写 7 维、实际 9 维"这类漂移 —— 引用本文数字请带日期，或先跑 `make ledger-report` + `make quality-trend` 取最新。

---

## 🔧 七、下一步建议

1. **今天就做 W1-2 + W1-7 + W1-8b**（合计 ≈1 人日，全低风险高收益）：`Makefile` 加 `USER_PATHS` 两行 → `predict-daily.service` 加一行 `TimeoutStartSec` → `parquet_reader` 三个工具接 `_norm_code`。每处配一个测试。
2. **本周排 W1-1 哨兵**：这是唯一"正在发生的数据正确性风险"，用哨兵（**不挪时间窗**）成本最低、收益最直接；改完当天就能从日志验证 predict 是否早于 sync 完成。
3. **同步排 W1-3（系统级告警）**：与 W1-1 同批改 system 单元，一次 root 部署解决两个 P0。改完立刻用 `systemctl show` 手工造一次失败验证告警真的会响。
4. **把 W2-6 提到 W2 之前**：先补 `skill_router` 的三个测试文件（**改造前它们会红**，正好用来锁死改造结果），再动路由与 schema —— 避免又一轮"改了行为没人知道"。
5. **决策四件事**：D1 容器栈演示化 / D2 执行研究线归档 / **D3 skill 层去留** / D4 若接生产先接 `daban` 不接 `theory`。其中 D3 最紧要：**维持"整层看起来在用"的状态是最贵的选项**。
6. **保留现状快照纪律**：本报告即快照；下次改动后更新 `docs/WORKFLOW_SNAPSHOT_<date>.md`，或直接更新本文数字，避免再次出现"文档 8 维、实际 9 维"。

---

## 附录 A · 本轮新增发现（不在 2026-11-01 16:10 审查文件中）

| # | 发现 | 证据 | 归属 |
|---|---|---|---|
| N1 | `skill_router` 整层**零生产调用方**（systemd / Makefile / mcp 全 0 命中） | `systemd/` grep 0、`Makefile` grep 0、`mcp/` 仅文档 | 审查裂缝 A 的严重化 |
| N2 | `systemd/system/` 全目录 **0 个 `OnFailure`** —— 系统级失败无第一手通知 | grep `OnFailure` → 0 命中 | KI-OPS-002 的具体化（新增 P0 证据） |
| N3 | `make ci` 含 `canary`（查 Ollama/GPU/磁盘）→ **CI 机器必红** | `Makefile:646` | M-6 遗留 → KI-MAKE-003（建议新建） |
| N4 | yaml 死字段精确到 **7 个顶层 key**（审查只说"6 个字段"） | 15 key 频次实测 + runtime 逐 key grep | KI-SKILL-002 细化 |
| N5 | MCP **8 个 yaml 声明引用从不被调用**；23 个 tool 实际被调 ~15 | runtime 21 处 `call_mcp` 硬编码点 | KI-MCP-002 关联 |
| N6 | `tools.json` 的 `description`/`inputSchema` 与代码**已漂移**（名称/顺序仍一致，有测试锁定） | `calc_macd.golden_cross` 语义差异 | 建议新建 KI-MCP-004 |
| N7 | `tdxfeed/daban/trial.py` 与 `scripts/find_double_up_c.py` 各**自实现一份 atomic helper**，未复用 `tdxfeed/atomic.py` | `trial.py:261-269` / `find_double_up_c.py:125` | KI-ATOMIC 延伸 |
| N8 | 8 处研究脚本仍是裸 `json.dump`/`write_text`（无 tmp→replace） | `plan_e_v3_live_track.py:119,133` 等 | KI-ATOMIC 延伸 |
| N9 | Agent/SKILL 共 **14 处失效引用**（4 + 5 + 3 + 2） | 见 §2.6 | KI-DOC-001 延伸 |
| N10 | **审查文件的一处错误**：`plan_e_backtest.py` 实际**存在** `--hold/--tp/--sl` | `scripts/plan_e_backtest.py:1751-1753,1756,1763,1765,1770` | 应从 W4-6 移除 |

## 附录 B · 证据索引（可复现命令）

```bash
# 门禁基线
python3 -c "import json;print(json.load(open('config/semantic_baseline.json'))['total'])"   # → 0
python3 -c "import json;print(json.load(open('config/ruff_baseline.json'))['total'])"       # → 205
venv/bin/python scripts/audit_ledger.py report                                              # → 台账 40 条两栏
venv/bin/python scripts/audit_ledger.py scan                                                 # → 复扫判定
venv/bin/python scripts/quality_trend.py                                                     # → 债务趋势

# 结构事实
grep -rn "skill_router\|orchestrator" systemd/ Makefile mcp/          # → 生产调用方 0
grep -rn "OnFailure" systemd/system/                                 # → 0 命中（系统级黑洞）
grep -rn "TimeoutStartSec" systemd/predict-daily.service             # → 0 命中
grep -c "sys.path.insert" $(grep -rl "sys.path.insert" scripts/ tdxfeed/ quant/)   # → 73 文件
grep -rn "^from plan_e_backtest\|^import plan_e_backtest" scripts/   # → scripts→scripts 真边
```

---

生成时间：2026-11-01 22:20:00
生成者：CodeBuddy（工作流程逆向分析 · 只读实测）
代码快照：git `b4ebf40`（工作树干净）
分析范围：审查文件 `docs/archive/ARCHITECTURE_REVIEW_2026-11-01.md` + 台账 `config/known_issues.yaml`（40 条）
数据日期：2026-11-01（代码 / 调度 / 门禁基线实况快照）
