# 架构审查与升级改造路线图（技能 / Agent / 程序 / MCP / 调度 / 数据）

（2026-11-01 16:10）

> 审查范围：`.codebuddy/skills/`(3) · `skills/`(9) · `.codebuddy/agents/`(4) · `skill_router/` · `scripts/`(139) · `tdxfeed/`(68) · `quant/`(15) · `mcp/`(6 server/23 tool) · `systemd/`(user 12 timer + system 8 timer) · `Makefile`
> 方法：三路并行只读审查（技能/Agent 层、程序/数据层、MCP/运维层）+ **6 条 P0 结论人工复核**（见附录 A）
> 结论性质：**现状诊断 + 改造建议**；本次为纯审查，未改动任何生产代码

---

## 📊 核心指标

| # | 指标 | 实测（2026-11-01） | 说明 |
|---|---|---|---|
| 1 | 生产决策栈 | **LightGBM + 规则**（无 LLM） | `predict.py` = `lgb.Booster`；方案E 纯规则 |
| 2 | 运行中调度单元 | **user 9 + system 4** | 用户级 sync 已于 10-28 退役，改系统级 `tdx-sync.timer`（工作日 16:30）|
| 3 | canary 维度 | **8**（文档多处仍写 7）| 末位 `verification` 为 10-27 新增 |
| 4 | 技能 yaml 的"活字段"占比 | **6 / 全量**（`input_schema`/`output_schema`/`timeout_sec`/`priority`/`tools` 均为死字段）| 声明层与执行层脱钩 |
| 5 | MCP 工具运行时校验 | **0 / 6 server** | 全是 `handler(**a)`；`tools.json` 的 enum/required 从不执行 |
| 6 | MCP 客户端真超时 | **无**（`timeout=30` 参数从未被使用）| `readline()` 无限阻塞且持锁 |
| 7 | scripts→scripts 跨脚本 import | **13 条** | `plan_e_backtest.py` 事实上成了第二个库 |
| 8 | `sys.path.insert` | **约 85 处**（69 脚本 + 3 tdxfeed + 1 quant）| 根因：tdxfeed 未 editable 安装 |
| 9 | 高危产物原子写覆盖率 | **4 个未覆盖** | `plan_e_today.csv` / `positions.json` / `journal.csv` / 现役模型 |
| 10 | 跨文件重复实现 | 涨停判定 9 处 · `load_universe` 6 处 · 单股 parquet 读取 8 处 · `is_stock_code` 3 份 | 单源化治理只完成 features_5d / limits 两域 |
| 11 | 生产 canary 进 Prometheus | **0 条指标** | Prometheus 的 `canary` job 抓的是 v5 数学 canary |
| 12 | 技能/Agent 层测试 | **0 个**（`skill_router/` 无测试）| 40+ 处失效引用无防线 |

---

## 1. 结论摘要

**架构主线是清晰的**（Agent 角色层 / 运行时技能层 / 生产脚本层 / 数据层 四层分工，4 个项目 Agent 的互斥职责写得很明确），但实现层存在 **三条结构性裂缝**，它们决定了下面所有细节：

| 裂缝 | 表现 | 后果 |
|---|---|---|
| **A. 声明层与执行层脱钩** | yaml 的 schema/timeout/priority/tools 全是死字段；路由对最自然的"600519 怎么看"完全不可用；`adj-factor` 把股息率估计当复权因子输出；Agent 指向已退役的 systemd 单元 | 文档/声明看起来完备，实际不生效；**维护者按文档做会做错** |
| **B. 写入路径与调度竞态** | `predict-daily`(18:05) 与系统级 `tdx-sync`(16:30 起、最长 6h) **跨 manager 无法 `After=`**；4 个关键产物非原子无锁；训练默认覆盖现役模型 | **正在发生**：日志显示 predict 启动时间从 18:30~18:35 跳到 18:05（10-28 退役 sync 依赖后）|
| **C. 观测层"名字对得上、数据对不上"** | Prometheus 的 `canary` job 抓的是 v5 数学 canary；生产 canary 8 维零指标零规则；`onfailure_alert` 只查 user scope | "监控栈已上线"在**数据链路上不成立**；系统级单元失败是黑洞 |

**优先级总览**：

| 波次 | 主题 | 项数 | 累计工作量 | 核心目标 |
|---|---|---|---|---|
| **W1** | 止血：数据竞态 / 告警死循环 / 静默失败 | 9 | ≈6 人日 | 让"数据不会悄悄少一半、告警不会黑洞" |
| **W2** | 契约与路由：声明层接上执行层 | 6 | ≈6 人日 | yaml 变成真契约、路由可预期 |
| **W3** | 收敛：单源化 + 性能 + 规范 | 9 | ≈12 人日 | 消灭重复实现与最大性能坑 |
| **W4** | 可观测与长期治理 | 8 | ≈8 人日 | 指标化 + 防复发 |

---

## 2. 现状快照（2026-11-01 实测）

- **提交活跃**：10-26 至今 20+ 次提交，主线是"路径单源化 / 口径单源化 / CI 守卫 / 分层归档"——工程治理方向正确。
- **健康**：canary **err=0 warn=0**（8 维全绿）；工作树仅 2 项未提交（v5 审计快照）。
- **调度实况**：

| 作用域 | 单元 | 状态 |
|---|---|---|
| user | `canary-health`(5min) / `trainsh-health`(5min) / `daban-ticket`(工作日盘中 5min) / `plan-e-today`(08:45) / `predict-daily`(18:05) / `plan-e-journal`(18:10) / `plan-e-double-up`(18:30) / `tdx-clean-logs`(周一03:00) / `obsidian-sync.path` | enabled + active |
| user | `tdxfeed-sync` / `plan-e-monitor` / `plan-e-weekly` / `plan-e-real-refresh` / `archive-obsidian-annual` | disabled（文件仍留）|
| system | `tdx-sync`(16:30) / `tdx-monitor`(5min) / `tdx-verification`(10:00) / `audit-log-rotate` | enabled + active |

- **生产拓扑（真实）**：系统级 `tdx-sync` 同步 → 用户级 `predict-daily` 出榜 → `plan-e-journal` 归档 → `daban-ticket` 产单 → `canary` 巡检 → `onfailure-alert@` 推微信。**用户级 skills/agents 不被任何 timer 调用**（可触发未触发）。

---

## 3. 真实架构图（含裂缝位置）

```
┌─ IDE 剧本层 ────────────────────────────────────────────────┐
│ .codebuddy/skills/*/SKILL.md (3)   ← 流程剧本；含失效引用与过期断言 │
│ .codebuddy/agents/*.md (4)        ← 角色封装；含指向退役单元的描述  │
└──────────────┬───────────────────────────────────────────────┘
               │ ⚠️A：allowed-tools 无 MCP；agents 无 MCP；yaml 死字段
┌─ 运行时技能层 ──────────────────────────────────────────────┐
│ skills/*.yaml (9) ─→ skill_router/runtime.py ─→ MCP 6 server   │
│ orchestrator 路由：theory 前置 / 裸6位不可用 / 大小写不一致      │
│ ❌ 零测试；MCP 假超时 + 持锁死锁；9/9 skill 参数传递不完整        │
└──────────────┬───────────────────────────────────────────────┘
┌─ 生产脚本层 ────────────────────────────────────────────────┐
│ predict.py(1079) plan_e_backtest.py(2002) plan_e_journal.py(950)│
│ ⚠️ 13 条 scripts→scripts import；约 85 处 sys.path.insert      │
│ ⚠️ 4 个高危产物非原子无锁；训练默认覆盖现役模型                 │
└──────────────┬───────────────────────────────────────────────┘
┌─ 数据层（方向正确：tdxfeed 零反向依赖）────────────────────┐
│ parquet_io.write_table(tmp+replace) ← 唯一权威写盘口 ✅            │
│ writers.merge_write / amount_fix / adj_factor / backfill_history │
│ ⚠️ state.py O(n²) manifest 写入；8 处绕过 read_daily 的重复读法   │
└──────────────┬───────────────────────────────────────────────┘
┌─ 调度与观测 ────────────────────────────────────────────────┐
│ system: tdx-sync(16:30) ──✂──✂── user: predict-daily(18:05)    │
│            ↑ 跨 manager 无法 After= → 竞态窗口                   │
│ canary(8维,零指标) ─→ onfailure-alert@(仅user scope) ─→ 微信     │
│ Prometheus job "canary" 实际抓的是 v5 数学 canary ⚠️              │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 三条裂缝的证据链

### 4.1 裂缝 A · 声明层与执行层脱钩

| # | 证据 | 位置 |
|---|---|---|
| A-1 | yaml 只有 6 个字段被 runtime 真正消费；`input_schema`/`output_schema`/`timeout_sec`/`priority`/`tools`/`metrics`/`freshness` 全为死字段 | `skill_router/runtime.py`（grep 命中 0）|
| A-2 | 路由对"600519 怎么看"完全不可用：`CODE_RE` 支持裸 6 位，但 `extract_code` 只认带前缀 | `skill_router/orchestrator.py:38,113` |
| A-3 | `pe|pb` 未加 `re.IGNORECASE` → 写"PE/PB"（中文财经极常见）不命中；但 `extract_metric` 却带 IGNORECASE | `orchestrator.py:32` vs `:118-129` |
| A-4 | theory 判定**无条件前置** → "MACD 和 KDJ 的区别"被抢去讲义库 | `orchestrator.py:91-92` vs `:46-53` |
| A-5 | `backtest` / `financial-statement` 两个 skill **无路由入口** | `orchestrator.py:89-96` |
| A-6 | `adj-factor` 声明调 `calc_adj_factor`（真算法）却从不调用，实际输出自造的"股息率下限估计" | `skills/adj-factor.yaml:16-17` vs `runtime.py:452-494` |
| A-7 | `indicator-match` 声明 16 个权重键，runtime 只实现 6 个 | `skills/indicator-match.yaml:27-43` vs `runtime.py:515-563` |
| A-8 | 4 个 Agent 指向 **10-28 已退役**的 `tdxfeed-sync.timer`；真实入口是系统级 `tdx-sync.timer`（工作日 16:30）| `.codebuddy/agents/tdx-data-collector.md:20` |
| A-9 | 文档里 `agents/` 目录**不存在**，真实是 `skill_router/`（4 处失效引用）| `.codebuddy/README.md:42,50,82,90` |
| A-10 | `skills/` 数量三处维护（skill 说 8 / 实际 9 / docs 说 9）| `.codebuddy/skills/tdx-quant-workflow/SKILL.md:194` |
| A-11 | `obsidian-git.json` 内联副本与仓库源文件不一致（skill 说 `commitOnClose: true`，源文件 `false`）→ 按 skill 执行会写出漂移配置 | `skills/tdx-obsidian-version-control/SKILL.md:113-127` vs `etc/obsidian/obsidian-git.json:9` |
| A-12 | `tdx-obsidian-math-verify` 教用户用 `MATH_VAULT=... make verify-math` 切库，但 Makefile 读的是 `VAULT`/`PREFIX`/`VAULT_KIND` → **变量被静默忽略，校验的是另一个库** | `SKILL.md:92,117` vs `Makefile:509` |

### 4.2 裂缝 B · 写入竞态与静默失败

| # | 证据 | 位置 |
|---|---|---|
| B-1 | `predict-daily.service` 注释明写"2026-10-28 退役，不再 Requires/After"；而系统级 `tdx-sync` 工作日 16:30 起、最长 6h、还带两行全量复权 `ExecStartPost` | `systemd/predict-daily.service:3-6`；`systemd/system/tdx-sync.service:10-15` |
| B-2 | 日志实证：10-20~10-27 predict 启动在 **18:30~18:35**（排在同步之后），10-28 后在 **18:05:22/18:10:16** | `logs/predict.log:3858` vs `:3943` |
| B-3 | 新鲜度守卫只挡"整体陈旧"（50 抽样 >20% 末日期早于 today-2d → exit 2），**挡不住"部分标的正在被写"** | `scripts/precheck_freshness.sh` |
| B-4 | `plan-e-double-up.service` 的 `After=tdxfeed-sync.service` 已成**死依赖**（该单元已退役）| `systemd/plan-e-double-up.service:4` |
| B-5 | `plan_e_today.csv` / `positions.json` / `journal.csv` / 现役模型 `.txt` —— 4 个关键产物**非原子、无锁、无备份** | `plan_e_backtest.py:691`、`plan_e_journal.py:227-248`、`train_daban_model.py:224` |
| B-6 | `train_daban_model.py` 不带 `--tag` 直接覆盖现役模型，而 `predict.py:573` 同时在读 | `train_daban_model.py:278` |
| B-7 | `tdxfeed/state.py:39-41` 每只股票整文件重写 manifest → O(n²)（≈44s 纯序列化 + 1.8GB 写盘）| `state.py:20-31` |
| B-8 | 212 处 `except Exception`（scripts 153 / tdxfeed 59），`ruff.toml:55-56` 全量豁免 BLE001 | `ruff.toml` |
| B-9 | 数据层 98 处裸 `print`（`full_sync.py:20` 已建 logger 却不用）| `tdxfeed/` |
| B-10 | `amount_fix.flush_estimated` 用进程内缓存覆盖磁盘条目 → 两个 full_sync 并发时后者用陈旧缓存覆盖前者 | `tdxfeed/amount_fix.py:142-159` |

### 4.3 裂缝 C · 观测错配与告警黑洞

| # | 证据 | 位置 |
|---|---|---|
| C-1 | Prometheus job 名 `canary`，实际抓 `v5/scripts/canary_metric_exporter.py` → 读的是 **v5 数学 canary** 状态，与生产 canary 无关 | `/etc/prometheus/prometheus.yml`；`v5/scripts/canary_metric_exporter.py:13-15` |
| C-2 | 6 条告警规则**全部针对 math-sympy-http**；生产 canary 8 维**零指标零规则** | `prometheus-alerts.yml` |
| C-3 | `onfailure_alert.py:57,70` 一律 `--user` scope → **系统级单元状态永远取不到**（result/status 全是 `?`）| `scripts/onfailure_alert.py` |
| C-4 | `make timer-install` 白名单 `USER_TIMERS` **不含 obsidian-sync**（.path/.service），而它已 enabled+active → `verify_obsidian_systemd.sh` 报漂移时提示的修复动作"make timer-install"**无效** | `Makefile:47-48` vs `Makefile:395-407`；`scripts/verify_obsidian_systemd.sh:71` |
| C-5 | `predict-daily.service` 等缺 `TimeoutStartSec` → user manager 默认 90s，全市场 5221 只 × 8 进程**卡在边缘**（`plan-e-today.service:15-16` 已显式写 600）| `systemd/predict-daily.service` |
| C-6 | canary 只查 4 个 timer 的 is-active，不看任何 user 单元的 `ExecMainStatus` → 连续失败看不见；daban 试验仓 / 执行研究线 / 成交额回填**零覆盖** | `scripts/canary_health_check.py:296-324` |
| C-7 | 5 组已停用单元文件仍在 `systemd/` 与 `~/.config/systemd/user/`，`make timer-status` 的 `ls *.timer` **展示成"已安装即启用"** | `Makefile:406` |
| C-8 | 投递失败只写日志，`onfailure-alert@` 自身无 OnFailure → 告警员与告警通道同时失效时是黑洞 | `scripts/onfailure_alert.py:161` |

### 4.4 三条裂缝的相互放大

```
裂缝B（竞态）→ canary 的 data_files 抽样可能命中"正在被写的文件" → 误报/漏报
     ↓
裂缝C（观测错配）→ 生产 canary 无指标、无规则 → 误报看不见、真故障也看不见
     ↓
裂缝A（声明层脱钩）→ 没人知道 predict-daily 已不再等同步（service 注释里有，但 docs/agent 里写的是旧单元）
     ↓
⇒ 三者叠加 = "看起来一切正常，实际上可能半市场数据是半新半旧的"
```

---

## 5. 改造清单

### 5.1 W1 · 止血（9 项，P0，≈6 人日）

> 目标：让"数据不会悄悄少一半、告警不会黑洞、静默失败会响"。

| # | 问题 | 改法 | 工作量 | 风险 | 验收判据 |
|---|---|---|---|---|---|
| **W1-1** | predict 与 sync 跨 manager 无序 → 半新半旧数据 | ① `tdx-sync.service` 的 `ExecStartPost` 末尾 touch `data/.state/sync-done`；② `predict-daily.service` 加 `ExecStartPre` 校验哨兵存在且 mtime 在 N 小时内（不足则 exit 2 跳过出榜）；③ 或把 predict 挪到 19:00 | 0.5-1 人日 | 中（改时间窗影响产出节奏）| 日志中 predict 启动时间 ≥ sync 完成时间；哨兵缺失时 predict **不产出**而非产出脏榜 |
| **W1-2** | `make timer-install` 漏 obsidian-sync → 告警死循环 | 新增 `USER_PATHS := obsidian-sync.service obsidian-sync.path` 纳入 `timer-install`；`RETIRED_PATHS := tdxfeed-sync.path` 纳入 `timer-disable` | 0.5 人日 | 低（注意 copy 顺序：service 先于 path）| `make timer-install` 后 `obsidian-sync.path` 与仓库一致；canary 的 obsidian_sync 不再误报 |
| **W1-3** | `onfailure_alert` 只查 user scope → 系统级黑洞 | 加 `--scope {user,system}`；新增 `systemd/system/onfailure-alert-system@.service`（需 root） | 1 人日 | 中（勿与现有 pushplus_bridge 抢 :5002）| 系统级单元失败能收到微信 |
| **W1-4** | MCP 客户端"假超时" + 持锁死锁 | `select.select([proc.stdout],[],[],timeout)` 真超时；超时后 kill + `proc=None` 让 `start()` 能重启；`stop()` join stderr 线程；`SkillRuntime` 注册 `atexit` | 1-1.5 人日 | 中（超时后 server 状态丢失，需重跑 initialize）| server 挂起时调用方在 timeout 内返回错误而非永久阻塞 |
| **W1-5** | 4 个高危产物非原子无锁 | `plan_e_today.csv`/`positions.json`/`journal.csv`/模型 `.txt` 全部改 tmp+replace；`append_journal` 加 `fcntl.flock` | 1 人日 | 中（锁需超时防死锁）| 崩溃后文件不半截；并发追加不丢行 |
| **W1-6** | 训练默认覆盖现役模型 | 模型写 tmp+replace；或默认强制 `--tag`，仅显式 `--promote` 才覆盖现役路径（复用 `stage1_retrain.py:259-264` 的 `guard_output_path`） | 0.5 人日 | 中 | 不带参数跑训练**不会**动现役模型 |
| **W1-7** | 缺 `TimeoutStartSec` | `predict-daily` 加 `TimeoutStartSec=1800`；`plan-e-journal/weekly/monitor`、`canary-health` 同样显式给值 | 0.5 人日 | 低 | 全市场预测不再卡在 90s 默认值 |
| **W1-8** | MCP 无运行时校验 + `parquet_reader` code 路径穿越 | 抽共享 `_validate(schema, args)`（`jsonschema` 已是测试依赖）在 `tools/call` 调用；`parquet_reader` 三工具统一用 `_norm_code` 正则 | 1-1.5 人日 | 中（会暴露历史调用方传了 schema 不允许的参数）| 非法参数返回 `-32602` 而非 `-32603`；`code="../../x"` 被拒 |
| **W1-9** | `adj-factor` 输出语义错误（股息率估计冒充复权因子） | 短期：改名为"股息调整估计"，输出字段改 `cum_dividend_yield_est`，描述首行写「非复权因子，生产复权以 `data/adj_factor/` 为准」；长期：`read_xdxr` 增加 `detail=True` 返回逐次事件，runtime 真调 `calc_adj_factor` | 短期 0.5 人日 / 长期 1.5 人日 | 短期低；长期中（改数值口径，须与 `data/adj_factor/` 对账）| 用户不会再把该输出当复权因子使用 |

### 5.2 W2 · 契约与路由（6 项，≈6 人日）

> 目标：让 yaml 变成**真契约**，让路由**可预期**。

| # | 问题 | 改法 | 工作量 | 验收判据 |
|---|---|---|---|---|
| **W2-1** | 路由顺序错 / 裸 6 位不可用 / 大小写不一致 | ① `extract_code` 支持裸 6 位并按首位补 sh/sz（复用 `mcp/servers/news.py:61-67` 规则）；② theory 判定**下移到所有业务意图之后**且仅 `not code` 时生效；③ 所有 INTENT 正则加 `re.IGNORECASE`；④ 拆出"报表/资产负债/现金流"给 `financial-statement`；⑤ 新增 `backtest` 意图 | 1 人日 | "600519 怎么看"/"PE和PB区别"/"MACD和KDJ区别"/"查资产负债表" 四个用例全部命中预期 skill |
| **W2-2** | yaml 死字段（schema/timeout/priority/tools） | ① `execute_skill` 入口按 `input_schema` 做 required/type 校验；② `_request` 用真超时，超时值取 `skill["timeout_sec"]`；③ 输出按 `output_schema` 校验，不合规记 `output_format_compliance=false`；④ `route()` 改读 yaml `priority`，消除两套优先级 | 2 人日 | 改一个 yaml 的 `timeout_sec` 能真实影响超时；改 `priority` 能真实影响路由顺序 |
| **W2-3** | LLM 降级双跑 | runtime 已执行 `fallback_skill` 并返回 `fallback_from`，orchestrator 又判 `"llm_output" not in result` 再跑一次 → 覆盖首次结果 + MCP 调用翻倍 | 0.5 人日 | LLM 不可用时 indicator-match 的 MCP 调用次数 = 1 组（4 次），不是 2 组 |
| **W2-4** | 参数 9/9 不完整传递 | runtime 一律 `inputs.get(x, skill["input_schema"]["properties"][x]["default"])`；orchestrator 传全 schema 字段；删 `runtime.py:500` 的 60 与 yaml 20 的双默认值 | 1 人日 | yaml 里每个可配参数都能从对话侧改到并生效 |
| **W2-5** | MCP 工具清单 3 份、无一致性测试 | 新增 `tests/test_skill_mcp_contract.py`：yaml 的 `mcp:` 引用 ⊆ `tools.json`；`SERVER_SCRIPT_MAP` 键 ⊆ yaml 出现的 server 名；AST 扫 `runtime.py` 的 `call_mcp("server","tool")` 与 yaml 声明做差集（标 known-issue） | 1 人日 | 新增一个不存在的 MCP 引用 → CI 立即红 |
| **W2-6** | `skill_router/` 零测试 | 新增 `tests/test_skill_router_route.py`（路由参数化）+ `tests/test_skill_router_contracts.py`（yaml 字段 → runtime 消费映射，断言无死字段）+ `tests/test_mcp_client.py`（fake server 测超时/断开重启） | 1.5 人日 | 改 orchestrator 行为时测试先红 |

### 5.3 W3 · 收敛：单源化 + 性能 + 规范（9 项，≈12 人日）

| # | 问题 | 改法 | 工作量 | 风险 |
|---|---|---|---|---|
| **W3-1** | 13 条 scripts→scripts import（`plan_e_backtest` 成第二个库） | `plan_e_backtest` 的公共部分迁到 `tdxfeed/execution/` 或新建 `tdxfeed/plan_e/`；`find_d_signal_5d.load_one` → `tdxfeed.parquet_io.read_daily`；`stage1_baseline` 公共部分 → `tdxfeed/stage1/` | 2-3 人日 | 中（6 个脚本需同步改）|
| **W3-2** | 约 85 处 `sys.path.insert` | 加 `pyproject.toml` 并 `pip install -e .`；只保留 `scripts/_common.py` 一处；删 `tdxfeed/{full_sync,kline,fin_parquet}.py` 与 `quant/multifactor/engine.py` 里的 4 处 | 1 人日 | 中（systemd `ExecStart` 固定 PATH，需同步验证）|
| **W3-3** | 涨停判定多源（`features_5d.py:158/160/246` 内联 `limit-0.001` vs `execution/limits.py:109`） | 全部改走 `is_limit_up_ret`；删 `features_5d.py:50` 的 `LIMIT_MAP`（改从 limits.py 派生） | 0.5 人日 | 中（会动 polars 复刻基准，须跑 parity 测试）|
| **W3-4** | 8 处绕过 `read_daily` 的重复 parquet 读取 | 统一到 `read_daily`，并为其加 `columns`/`last_n_rows` 参数 | 1 人日 | 中（`read_daily` 有严格 date 校验，会暴露存量脏数据）|
| **W3-5** | 6 份 `load_universe` / 3 份 `is_stock_code` / 3 份 `_exchange_of` | 收敛到 `tdxfeed/names.py` + `tdxfeed/paths.py`；加 AST 单源测试（已有 `test_limit_single_source.py` 范式） | 2 人日 | 低 |
| **W3-6** | `state.py` O(n²) manifest 写入（≈44s + 1.8GB） | `State.update` 只改内存；批量落盘（每 200 只）或退出时落盘 | 0.5 人日 | 低 |
| **W3-7** | `find_double_up_c.py:157-171` 880 万次 numpy 切片 | `rolling(21).max().shift(-1)` 向量化；`screen_one` 进程池化 | 0.5 人日 | 低（须用现有 CSV 做 golden 对比）|
| **W3-8** | `predict.py:393` 全量读 parquet（≈840MB 无用 IO） | `pd.read_parquet(columns=...)` + 只读尾部 N 行（依赖 parquet 按 date 升序写入，writers 已保证） | 0.5 人日 | 中 |
| **W3-9** | ruff 规则缺 ANN/D/PLE/C901；212 处 `except Exception` 全量豁免 | 改 per-file 白名单（旧文件豁免、新文件报错）；tdxfeed 的 98 处 print 改 logging | 3 人日 | 中 |

### 5.4 W4 · 可观测与长期治理（8 项，≈8 人日）

| # | 问题 | 改法 | 工作量 |
|---|---|---|---|
| **W4-1** | 生产 canary 零指标 | 新增 exporter 读 `logs/canary_history.jsonl` 暴露 `tdx_canary_check_status{check=...}`；加 `== 0 for 10m` 规则；Prometheus job 改名 `math-canary` 消歧 | 1.5 人日 |
| **W4-2** | canary 覆盖不到新路径 | 新增 `check_user_units()`（遍历 USER_TIMERS 的 `ExecMainStatus`）、`check_daban_tickets()`（当日产单时间/停止线/成功率）、`check_amount_backfill()`（daily 的 amount 缺失率） | 2-3 人日（先跑一周 shadow）|
| **W4-3** | 停用单元物理隔离 | 移到 `systemd/retired/`；`timer-status` 区分 enabled/disabled | 0.5 人日 |
| **W4-4** | Makefile 口径与生产不一致 | `make predict` 对齐 `predict-daily.service`（`--position-mode small --workers 8` + precheck 守卫）；`plan-e`→`plan-e-full`；`journal-buy/sell` 移入 `make-legacy` 或加二次确认；`data-sync`→`data-sync-one`；`pipeline` 去掉 `deploy-all` 与单标的 `data-sync` | 1.5 人日 |
| **W4-5** | 容器栈与生产冲突且必然崩 | compose 加 `profiles:[demo]`、改端口、`command` 换成真实入口（`tdxfeed.scheduler` **不存在**）、修 Dockerfile 的 `requirements-extra.txt` COPY 通配符；明确标注"容器栈 ≠ 生产" | 1 天（生产化 ≥1 周）|
| **W4-6** | 文档漂移批量修正 | canary 7→8 维、`agents/`→`skill_router/`、timer 清单（实为 12 timer + 2 path）、README 的 automation 数（自相矛盾）、`deploy/README.md` 的 6→10 服务、`plan_e_backtest.py` 的 `--hold/--tp/--sl`（**该脚本没有这些参数**） | 0.5 人日 |
| **W4-7** | MCP 错误返回不合规范 | 业务错误改 `result:{isError:true, content:[...]}`；JSON-RPC error 只留"未知方法/工具"；13 处非法 code `404` 改标准码；补 `ping` | 1 天 |
| **W4-8** | 防复发 CI 护栏 | 新增 `tests/test_docs_consistency.py`：① SKILL.md 提到的 `scripts/*` 与 Makefile 目标真实存在；② Makefile 行号引用不越界；③ `skills/*.yaml` 的 name 集合 == runtime 实际加载集合 == docs 表格条目数；④ yaml 的 `mcp:` 引用 ⊆ tools.json | 1 天 |

---

## 6. 优先级矩阵与实施路线

### 6.1 影响 × 工作量 四象限

```
高影响 · 低工作量（先做，收益/成本比最高）
  W1-2 timer-install 白名单      W1-7 TimeoutStartSec      W1-6 模型覆盖保护
  W1-9 adj-factor 语义           W4-6 文档漂移批量修正        W3-6 state 批量落盘
  W4-3 停用单元隔离              W3-3 涨停判定单源            W2-3 降级双跑
        └──────────────────► 立即排入本周

高影响 · 高工作量（需排期 + 配套测试）
  W1-1 predict/sync 竞态         W1-4 MCP 真超时与崩溃恢复    W1-8 MCP Schema 校验
  W1-5 高危产物原子写与锁        W2-1 路由重排                W2-2 yaml 变真契约
  W2-4 参数完整传递              W3-1 scripts import 收敛     W4-2 canary 覆盖新路径
        └──────────────────► 需 1-2 人日连续投入 + 回归验证

低影响 · 低工作量（随缘）
  W2-5 契约测试   W4-8 文档一致性测试   W3-7 向量化   W3-8 IO 优化

低影响 · 高工作量（暂缓/需决策）
  W3-2 editable install（需协调部署）  W4-5 容器栈生产化（当前直接演示化更划算）
  plan_e_backtest.py 2002 行拆分    run_mf_backtest 316 行拆分
```

### 6.2 四波次实施计划

| 波次 | 时间窗 | 内容 | 出口判据 |
|---|---|---|---|
| **W1-0 立即（今天，≈2h）** | D0 | W1-2（timer-install 白名单）· W1-7（TimeoutStartSec）· W1-6（模型覆盖保护）· W4-6（文档漂移批量修正） | canary 不再出现"修复动作无效"的告警；跑训练不动现役模型 |
| **W1 止血（1.5 周）** | D1-D8 | W1-1 竞态哨兵 · W1-3 onfailure 双 scope · W1-4 MCP 超时 · W1-5 原子写与锁 · W1-8 Schema 校验 · W1-9 adj-factor 语义 | 连续 5 个交易日 predict 输出稳定且"不早于 sync 完成"；MCP 挂起不再拖死 runtime |
| **W2 契约（2 周）** | D9-D20 | W2-1~W2-6（含 skill_router 三个测试文件） | yaml 改一个字段真实生效；路由四个用例全对；契约测试守住 |
| **W3 收敛（3-4 周）** | D21-D45 | W3-1~W3-9 | 13 条跨脚本 import 归零；`sys.path.insert` ≤ 5 处；ruff 新规则对新增代码零违规 |
| **W4 治理（并行，2-3 周）** | D15-D45 | W4-1~W4-8 | 生产 canary 在 Prometheus 有指标有规则；新代码路径进巡检；CI 挡住下一次"声明与实现漂移" |

**依赖关系**：W2-1/W2-2 必须在 W2-6 之后做（先有测试再改行为）；W3-1 建议在 W3-2 之后（可编辑安装能简化 import 迁移）；W4-2 的 shadow 模式需要 W1-1 完成后才有干净数据。

### 6.3 建议的起手式（不新增依赖）

1. **W1-2**：`Makefile` 加 `USER_PATHS` / `RETIRED_PATHS` 两行 + `timer-install`/`timer-disable` 各加一个循环 → 15 分钟，立刻止住 canary 告警死循环。
2. **W1-7**：`predict-daily.service` 加一行 `TimeoutStartSec=1800` → 5 分钟。
3. **W1-6**：`train_daban_model.py` 的 `model_out` 改为"默认写 `<tag>.txt`，仅 `--promote` 覆盖现役" → 30 分钟（可复用 `stage1_retrain.py:259-264`）。
4. **W1-1 的一半**：先只加"哨兵 + ExecStartPre 校验"（不改时间窗）→ 1 小时，当天就能验证 predict 是否早于 sync 完成。

---

## ⚠️ 风险提示

1. **本次是审查不是修复**：所有改造建议**尚未实施**，现状仍带 W1 的全部风险；特别是 predict/sync 竞态与 onfailure 系统级黑洞**此刻仍在生效**。
2. **W1-1 改调度会影响产出节奏**：哨兵方案会让"同步没跑成 → 当天不出榜"，这是正确行为但会改变你看到榜单的时间，需提前约定。
3. **W1-5 加锁有死锁风险**：`fcntl.flock` 必须配超时，且要保证"读-改-写"三步在同一把锁内，否则加锁反而制造新的丢数据路径。
4. **W2-2 让 yaml 变成真契约会暴露历史调用方**：现有 yaml 里的 `input_schema` 有字段 runtime 根本不消费（如 `news-summary.topics`），一旦开始校验，**存量 yaml 会立刻变红**，需安排一次"yaml 与实现对齐"的批量修。
5. **W3-1/W3-2 是大手术**：`plan_e_backtest` 有 6 个消费者，`sys.path` 改造涉及 systemd `ExecStart` 的 PATH，**必须与 W2-6 的测试同批推进**，否则改到一半无法回归。
6. **W4-5 容器栈当前"必然崩"**（`tdxfeed.scheduler` 不存在、Dockerfile COPY 的 `requirements-extra.txt` 不存在、11434 端口与宿主冲突）——在未做决策前**不要执行 `docker compose up`**，否则会与宿主 Ollama 抢端口。
7. **数字会漂移**：本文所有数量均为 2026-11-01 快照；近期已连续发生"10-26 文档写 7 维、实际 8 维"这类漂移，引用时须带日期。

---

## 🔧 下一步建议

1. **今天就做 W1-0 四件小事**（≈2h）：timer-install 白名单、TimeoutStartSec、模型覆盖保护、文档漂移批量修正 —— 全部低风险高收益，且能立刻止住"告警响但修复动作无效"的死循环。
2. **本周排 W1-1 哨兵**：这是唯一"正在发生的数据竞态"，用哨兵（不挪时间窗）成本最低、收益最直接。
3. **把 W2-6 的测试先行**：先补 `skill_router/` 的三个测试文件，再动路由与 schema —— 避免又一轮"改了行为没人知道"。
4. **决策两件事**：① 容器栈是"演示化"还是"生产化"（当前建议演示化）；② 执行研究线是否继续维护（`overall_go=false` 已定，若不再投入则归档相关 19 个脚本 + 7 个模块，让仓库回到可维护规模）。
5. **保留一份"现状快照"**：本文即快照；下次改动后请更新 `docs/WORKFLOW_SNAPSHOT_<date>.md` 与本文的数字，否则又会出现"文档 7 维、实际 8 维"。

---

## 附录 A · 人工复核记录（6 条 P0 结论）

| # | 结论 | 复核方式 | 结果 |
|---|---|---|---|
| 1 | 用户级 sync 已退役、系统级为生产入口 | `systemctl --user is-enabled tdxfeed-sync.timer` → **disabled**；`systemctl is-enabled tdx-sync.timer` → **enabled**（工作日 16:30）| ✅ 确认 |
| 2 | predict-daily 已无 sync 依赖 | `grep Requires/After systemd/predict-daily.service` → 无；仅 `ExecStartPre=precheck_freshness.sh` | ✅ 确认 |
| 3 | timer-install 白名单漏 obsidian-sync | `Makefile:47-48` `USER_TIMERS` 无 obsidian；`Makefile:395-407` 仅复制白名单 | ✅ 确认（该 path 已 enabled+active）|
| 4 | MCP 客户端 timeout 是假超时 | `skill_router/runtime.py:121` `def _request(..., timeout: float = 30)`；`:128` `self.proc.stdout.readline()` 无 select/超时 | ✅ 确认 |
| 5 | canary 实际 8 维 | `scripts/canary_health_check.py:397-404` 8 个 check 调用 | ✅ 确认（文档写 7 维是漂移）|
| 6 | parquet_reader 无 code 校验 | `mcp/servers/parquet_reader.py:71,93` `DAILY_DIR / f"{code}.parquet"`，无正则（其余 server 有 `_norm_code`）| ✅ 确认 |

---

生成时间：2026-11-01 16:10:00
生成者：CodeBuddy（架构审查 + 改造路线图）
数据日期：2026-11-01（代码/调度/磁盘实况快照）
