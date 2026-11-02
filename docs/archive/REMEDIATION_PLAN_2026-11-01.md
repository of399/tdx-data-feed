# 整改与决策执行计划（P0 / P1 / P2 / 待决策）

（2026-11-01 23:05 · 依据 `docs/archive/REVERSE_WORKFLOW_ANALYSIS_2026-11-01.md`）

> **范围**：逆向分析产出的 **26 个条目** —— P0 5 项 + P1 11 项 + P2 6 项 + 待决策 4 项，逐项给出状态 / 整改动作 / 责任人 / 时限 / 验收标准。
> **性质**：执行计划，**尚未实施**。本计划本身不改任何生产代码。
> **快照基线**：git `b4ebf40`（工作树干净，本计划未提交）· 台账 40 条 · 语义基线 0 · ruff 基线 205

---

## 📋 执行须知（三条硬约束，先读）

### 须知 1 · 三类责任人代号

| 代号 | 含义 | 执行边界 |
|---|---|---|
| **R-仓库** | 仓库内改动：Python / shell / unit 文件 / Makefile / tests | 工作区内，可立即动手，**不需要 root** |
| **R-部署** | 改仓库**不够**，还要落到 `/etc/systemd/system/` 并 reload+restart | **需要 root**，走 `systemd-run --user --pty --wait` + `SUDO_ASKPASS=<zenity 脚本> sudo -A`（本机进程树 `NoNewPrivs=1`，`sudo`/`pkexec` 直接拿不到 root） |
| **R-验收** | 判定达成 | 自动化判据优先（可 `make`/可 `pytest` 复现），目视为辅 |

> **本项目唯一执行人是仓库 owner（jiuben）**，台账 `owner` 字段亦全部为该账号。本计划不虚构"团队分工"，改为按**执行边界**分派——真正的分工点是**能否在不获 root 的前提下把该条目推进到可验收状态**。

### 须知 2 · M-5 硬规则：修复必须附带测试

`scripts/audit_fix_tests.py` 的 **M5-R3** 规定：改动生产代码**必须同时**改测试；**M5-R1/R2** 规定台账 `FIXED` 条目必须绑定真实存在的测试文件且测试内含条目 ID marker。
⇒ **本计划每一项的"落地物"列都含测试文件**。未按此落地的条目，`make fix-tests-check` 会直接红，CI 不得放行。

### 须知 3 · 时间锚点：今天周日，第一个验证窗口是明天

| 事实 | 值 | 影响 |
|---|---|---|
| 今天 | 2026-11-01（周日） | 仓库改动可今晚完成；systemd timer **工作日才触发** |
| 第一个可验证的 sync 窗口 | **11-02（周一）16:30** | 哨兵 P0-1 的首次真实验证点 |
| 第一个受影响的 predict 窗口 | **11-02（周一）18:05** | 哨兵若未妥善设计，会导致当天**不出榜** |
| 最早的系统级 root 部署窗口 | 11-02 上午（需桌面弹窗输密码） | P0-1 / P0-2 必须在此前完成仓库改动 |

> ⚠ **P0-1 的上线设计已内置"不误杀"双通道**（哨兵文件 **或** 单元状态任一满足即放行），避免首次上线当日因哨兵文件尚不存在而拦掉 11-02 的正常出榜。详见 P0-1。

---

## 🔴 P0 · 今日须生效（5 项）

> **共同目标**：让"数据不会悄悄少一半、告警不会黑洞、非法参数不会越界"。这 5 项全部**今天完成仓库改动**，其中 2 项需在 11-02 上午前完成 root 部署。

### P0-1 · 同步完成哨兵（消除 predict/sync 竞态）

| 字段 | 内容 |
|---|---|
| **台账** | KI-COMPETE-001（P0 / OPEN / 无守卫） |
| **当前状态** | ❌ 未落地。`systemd/system/tdx-sync.service` 无完成标记；`systemd/predict-daily.service` 的两个 `ExecStartPre` 位置只有一个新鲜度守卫（`scripts/precheck_freshness.sh`），只挡"整体陈旧"，**挡不住"部分标的正在被写"**。日志实证：predict 启动时间已从 18:30~18:35 跳到 18:05。 |
| **责任人** | **R-仓库** + **R-部署**（system 单元） |

**整改动作**

1. 新增 `scripts/mark_sync_done.py`（R-仓库）：写 `data/.state/sync-done`，内容 = ISO 时间戳 + 本轮 `manifest.last_date`；写入用 `tdxfeed/atomic.py` 的 `atomic_write_text`（禁裸写）。
2. 新增 `scripts/check_sync_sentinel.py`（R-仓库）：双通道判定，**任一满足即放行**——
   - 通道 A：`data/.state/sync-done` 存在且 mtime 在 `--max-age-hours` 内；
   - 通道 B（首次上线兜底）：`systemctl show tdx-sync.service -p Result -p ExecMainExitTimestamp` 显示 `Result=success` 且退出时间在窗口内。
   - 两通道均不满足 → **exit 2**（与 `require_fresh` 的 exit 2 语义一致 = "数据未就绪，不出榜"）。
3. `systemd/system/tdx-sync.service`（R-仓库→R-部署）：在 3 条 `ExecStartPost` **之前**插入一条**不带 `-` 前缀**的
   `ExecStartPost=... scripts/mark_sync_done.py` —— ExecStart 失败则 ExecStartPost 整体不执行 ⇒ **同步失败必然不写哨兵**；同步成功则先写哨兵再跑 indicators/adj/hfq。
4. `systemd/predict-daily.service`（R-仓库→R-部署）：在现有 `ExecStartPre=precheck_freshness.sh` **之后**追加
   `ExecStartPre=... scripts/check_sync_sentinel.py --max-age-hours 12`。systemd 的 ExecStartPre 按出现顺序执行，任一非零即中止主 ExecStart。
5. 测试（R-仓库，M5-R3 强制）：`tests/test_sync_sentinel.py` —— 覆盖"哨兵新鲜则放行 / 哨兵缺失且单元 success 则放行 / 两者皆失效则 exit 2 / mark 写盘原子"。

**时限**：仓库改动 **11-01 23:40 前**；root 部署 **11-02 10:00 前**（赶在 16:30 第一个 sync 之前）。

**验收标准**

```bash
# ① 哨兵机制在线（11-02 16:30 后）
ls -l data/.state/sync-done && date -r data/.state/sync-done     # 存在且 mtime = 本交易日 16:30~22:30
# ② predict 确实在 sync 完成之后启动（关键判据）
grep -n "sync-done\|哨兵\|sentinel" logs/predict.log logs/predict.err | tail
journalctl -u predict-daily.service --since "11-02 16:00" | grep -i "Started\|Finished"
# ③ 负向验证：把哨兵 mtime 改旧后，predict 必须不产出
touch -d "3 days ago" data/.state/sync-done && systemctl --user start predict-daily.service
systemctl --user show predict-daily.service -p Result -p ExecMainStatus   # 期望 Result=exit-code / ExecMainStatus=2
# ④ 单元配置已加载
systemctl show tdx-sync.service -p ExecStartPost | grep mark_sync_done
```

**判定标准**：正负两条都通过 = 达成。负向验证必须在 11-02 出榜后做（或用 `--dry-run` 模式的守卫脚本单独验证，避免真的挡掉生产出榜）。

**风险**：若哨兵判定逻辑写错，可能导致每天都不出榜（假阴性）或每天都出脏榜（假阳性）。⇒ **双通道 + 上线首日先只读观察**（哨兵脚本加 `--dry-run` 只打印判定不退出非零，观察 1 个交易日后再开启拦截）。

---

### P0-2 · 系统级告警通道（闭合黑洞）

| 字段 | 内容 |
|---|---|
| **台账** | KI-OPS-002（P0 / OPEN / 无守卫） |
| **当前状态** | ❌ 未落地。`systemd/system/` 全目录 `OnFailure` grep **0 命中**；`scripts/onfailure_alert.py:75` 硬编码 `systemctl --user show`、`:61` 硬编码 `journalctl --user` ⇒ 系统级单元失败时载荷全是 `?`。**系统级 `tdx-sync` 失败目前只能靠 canary 的 `tdxfeed_sync` 维度在 5 分钟后发现**。 |
| **责任人** | **R-仓库** + **R-部署**（system 单元） |

**整改动作**

1. `scripts/onfailure_alert.py`（R-仓库）：加 `--scope {user,system}` 参数（默认 `user` 保兼容）——
   - `collect_state()`：system scope 用 `systemctl show` / `journalctl`（**不带 `--user`**）；
   - `journal_tail_cmd()`：system scope 用 `journalctl -u <unit>`（系统级日志在 journalctl 全局空间）。
2. 新增 `systemd/system/onfailure-alert-system@.service`（R-仓库）：模板单元，`ExecStart=... scripts/onfailure_alert.py --scope system %i`；**它只是 pushplus 的 HTTP 客户端，不监听端口 ⇒ 与 `pushplus_bridge` 的 5002 无冲突**。
3. 给系统级单元补 `OnFailure=`（R-仓库→R-部署）：
   - `systemd/system/tdx-sync.service` → `OnFailure=onfailure-alert-system@tdx-sync.service`
   - `systemd/system/tdx-monitor.service`、`tdx-verification.service` 同理。
4. 测试（R-仓库）：`tests/test_onfailure_scope.py` —— 断言 system scope 生成的 `systemctl`/`journalctl` argv **不含** `--user`；user scope 含 `--user`。

**时限**：仓库改动 **11-01 23:40 前**；root 部署 **11-02 10:00 前**（与 P0-1 同一次 root 会话一起做）。

**验收标准**

```bash
# ① 配置已加载
systemctl show tdx-sync.service -p OnFailure          # 期望 onfailure-alert-system@tdx-sync.service
systemctl cat onfailure-alert-system@tdx-sync.service  # 模板已进系统 scope
# ② 真实失败推送（用探针单元，不碰生产单元）
sudo tee /etc/systemd/system/tdx-fail-probe.service <<'EOF'
[Unit]
OnFailure=onfailure-alert-system@tdx-fail-probe.service
[Service]
Type=oneshot
ExecStart=/bin/false
EOF
sudo systemctl daemon-reload && sudo systemctl start tdx-fail-probe.service
# 期望：微信收到一条 TdxUnitFailed，载荷里 result=failed / status=1（而不是 ?）
```

**判定标准**：探针单元失败 ⇒ 微信收到载荷且 `result`/`status` **不是 `?`**。探针测完即 `systemctl disable --now` 并删文件。

---

### P0-3 · MCP `code` 参数路径穿越

| 字段 | 内容 |
|---|---|
| **台账** | KI-MCP-003（P0 / OPEN / 无守卫） |
| **当前状态** | ❌ 未落地。`mcp/servers/parquet_reader.py:75` `DAILY_DIR / f"{code}.parquet"`、`:97` 同款，**无任何格式校验**；`code="../../x"` 可越出数据目录读任意 `.parquet`。同仓其他 server 已有 `_norm_code`（参考实现 `mcp/servers/news.py:60-70`：仅接受 `sh|sz` + 6 位，裸 6 位按首位补前缀）。 |
| **责任人** | **R-仓库** |

**整改动作**

1. `mcp/servers/parquet_reader.py`（R-仓库）：新增 `_norm_code(code)`，**与 `mcp/servers/news.py:60-70` 同实现**（唯一来源仍是 `tdxfeed/execution/limits.py` 的 `normalize_code`，避免再造第 4 份实现 —— 与 KI-SINGLE-001 冲突）。
2. 三个涉及 `code` 的工具（`read_daily_kline` / `read_xdxr` / `list_codes` 的过滤参数）**统一先过 `_norm_code`**，非法值抛 `McpError(400, ...)`（本仓 McpError 的用法见 `parquet_reader.py:77`）。
3. 测试（R-仓库）：`tests/test_mcp_code_validation.py` —— 参数化断言 `../../x` / `/etc/passwd` / `sh600000/../x` / `600000.txt` 全部被拒，且**不产生任何文件读取**（用 monkeypatch 断言 `read_parquet` 未被调用）。

**时限**：**11-01 23:40 前**（纯仓库改动，无外部依赖，可与 P0-1/P0-2 并行）。

**验收标准**

```bash
venv/bin/pytest tests/test_mcp_code_validation.py -q        # 期望全绿
venv/bin/python -m pytest tests/test_mcp_contracts.py -q     # 23 个 tool 的名称/顺序不得被破坏
# 手工负向：code="../../x" 不得读到数据目录外文件
```

---

### P0-4 · `make timer-install` 白名单补 `obsidian-sync`

| 字段 | 内容 |
|---|---|
| **台账** | KI-OPS-001（P0 / OPEN / 无守卫） |
| **当前状态** | ❌ 未落地。`Makefile:48-49` 的 `USER_TIMERS` 只有 7 个 timer；`timer-install`（`:438-450`）只复制 `$(USER_TIMERS)` 的 `.service`/`.timer` 与 `$(USER_UNIT_TEMPLATES)` ⇒ **`obsidian-sync.path`/`.service` 从未被复制**。而 `timer-enable` 在 `:473-474` 又单独 enable 了 `obsidian-sync.path` ⇒ 单元在跑、但仓库源文件不在管理范围，`make timer-install` 的修复提示**无效**，canary 每 5 分钟持续报错。 |
| **责任人** | **R-仓库** |

**整改动作**

1. `Makefile`（R-仓库）：新增 `USER_PATHS := obsidian-sync.service obsidian-sync.path`（紧邻 `USER_TIMERS` 定义）；`timer-install` 追加复制循环，**必须 service 先于 path**（path 触发时 service 必须已存在，否则首触失败）。
2. `Makefile`：新增 `RETIRED_PATHS := tdxfeed-sync.path`，`timer-disable`（`:479-486`）在扫 `$(RETIRED_TIMERS)` 后追加扫 `$(RETIRED_PATHS)`，避免退役的 `.path` 被手工 enable 后收不掉。
3. `Makefile`：`timer-status`（`:488-494`）的 `grep -E` 模式补 `obsidian`，让停用/漂移状态可见。
4. 测试（R-仓库）：`tests/test_timer_install_scope.py` —— 解析 Makefile 断言 `obsidian-sync.path` ∈ 复制名单、`tdxfeed-sync.path` ∈ 停用名单、且复制顺序 service 先于 path。

**时限**：**11-01 23:40 前**（仓库改动）+ 立即执行 `make timer-install`（用户级，**无需 root**）。

**验收标准**

```bash
make timer-install
diff ~/.config/systemd/user/obsidian-sync.path systemd/obsidian-sync.path   # 期望无差异
diff ~/.config/systemd/user/obsidian-sync.service systemd/obsidian-sync.service
systemctl --user daemon-reload
venv/bin/python scripts/canary_health_check.py | grep -A3 obsidian   # 期望不再报"修复动作无效"
```

---

### P0-5 · `TimeoutStartSec` 全量显式化

| 字段 | 内容 |
|---|---|
| **台账** | KI-OPS-003（P1 / OPEN / 无守卫） |
| **当前状态** | ❌ 未落地。`systemd/predict-daily.service` **无** `TimeoutStartSec` ⇒ 用 user manager 默认 **90s**；而全市场预测是 5221 只 × 8 进程，量级上贴边（`plan-e-today.service:16` 已显式写 600，`daban-ticket.service:15` 写 180，唯独最重的 predict 漏了）。 |
| **责任人** | **R-仓库** |

**整改动作**

1. `systemd/predict-daily.service`（R-仓库）：加 `TimeoutStartSec=1800`（30 分钟，按实测 ~1 分钟留足余量，同时避免卡死时占住机器 6 小时）。
2. 同批显式化：`plan-e-journal.service`（1800）、`canary-health.service`（120）、`plan-e-weekly.service`（1800）、`plan-e-real-refresh.service`（1800）。
3. `systemd/tdx-clean-logs.service`、`obsidian-sync.service`（300）—— 逐个确认默认值是否够用，够用则**不写**（避免为写而写）。
4. 测试（R-仓库）：`tests/test_systemd_timeout_explicit.py` —— AST/文本解析所有 `systemd/*.service`，断言重任务单元（predict/plan-e-*/canary/daban-ticket）均含 `TimeoutStartSec` 且 ≥ 合理下界。

**时限**：**11-01 23:40 前**（仓库改动）+ `make timer-install` + `systemctl --user daemon-reload`（用户级，无需 root）。

**验收标准**

```bash
make timer-install && systemctl --user daemon-reload
systemctl --user show predict-daily.service -p TimeoutStartUSec   # 期望 1800s（1h30m0s）
systemctl --user show plan-e-journal.service -p TimeoutStartUSec  # 期望 1800s
venv/bin/pytest tests/test_systemd_timeout_explicit.py -q
```

---

### P0 小结：今日串行路径

```
21:50  ├ P0-3  parquet_reader _norm_code + 测试        （仓库，30 min）
21:50  ├ P0-4  Makefile USER_PATHS/RETIRED_PATHS + 测试  （仓库，20 min）
21:50  ├ P0-5  TimeoutStartSec ×6 + 测试                 （仓库，20 min）
22:15  ├ P0-1  mark_sync_done.py + check_sync_sentinel.py + 测试（仓库，60 min）
22:15  └ P0-2  onfailure_alert --scope + 系统级模板单元 + 测试（仓库，40 min）
23:40  └ 跑 make gate / make fix-tests-check / venv/bin/pytest tests/ -q
11-02 10:00  └ 一次 root 会话：cp systemd/system/* → /etc/systemd/system/ + daemon-reload + restart
11-02 16:30  └ 第一个 sync 窗口 → 哨兵真实验证
11-02 18:05  └ 第一个 predict 窗口 → 确认"在 sync 之后出榜"
```

---

## 🟠 P1 · 两周内（11 项）

> **顺序铁律**：**P1-1（测试基座）必须最先做**，P1-2/P1-3/P1-4 全部在其之后同批推进；P1-11（拆库）必须与 P1-1 的测试同批，否则改到一半无法回归。

| # | 项 | 状态 | 责任人 | 时限 | 验收标准 | 落地物（测试） |
|---|---|---|---|---|---|---|
| **P1-1** | **skill_router 三测试文件（先行基座）** | ❌ 零测试（`tests/` 下无 `test_skill_router_*`） | R-仓库 | **11-02 → 11-04** | ① 路由参数化 4 用例全绿；② 契约测试能**列出 7 个死字段**并标红 | `tests/test_skill_router_route.py`、`tests/test_skill_router_contracts.py`、`tests/test_mcp_client.py` |
| **P1-2** | **yaml 变真契约**（含参数完整传递） | ❌ 7 死字段；`timeout_sec` 硬编码 30s；默认 `lookback=60` 与 yaml 的 20 冲突 | R-仓库 | **11-05 → 11-09** | 改 yaml 的 `timeout_sec` 真实影响超时；改 `priority` 真实影响路由顺序；`input_schema` 必填项缺失返回可读错误 | `tests/test_skill_router_contracts.py` 扩展（死字段断言转绿） |
| **P1-3** | **路由可预期化** | ❌ 裸 6 位不可用；6 条 INTENT 无 IGNORECASE；theory 无条件前置；backtest/financial-statement 无入口 | R-仓库 | **11-05 → 11-09** | 4 用例全对："600519 怎么看"→kline / "PE和PB区别"→financial / "MACD和KDJ区别"→indicator / "查资产负债表"→financial-statement | `tests/test_skill_router_route.py` |
| **P1-4** | **LLM 降级双跑** | ❌ runtime 已降级并返 `fallback_from`，orchestrator 又判 `"llm_output" not in result` 再跑一次 → MCP 调用翻倍 | R-仓库 | **11-05 → 11-09**（与 P1-2 同批） | LLM 不可用时 indicator-match 的 MCP 调用次数 = **1 组（4 次）**，不是 2 组 | `tests/test_skill_router_route.py` 计数断言 |
| **P1-5** | **MCP 运行时 schema 校验** | ❌ `jsonschema` 只在测试里；server 直接 `handler(**args)` ⇒ 拼错参数名变 -32603 | R-仓库 | **11-05 → 11-08** | 非法参数返回 **-32602**；业务错误改 `result:{isError:true}`；补 `ping` | `tests/test_mcp_schema_validation.py` |
| **P1-6** | **死依赖清理** | ❌ `plan-e-double-up.service:4` `After=tdxfeed-sync.service`（该单元 10-28 已退役） | R-仓库 | **11-02**（随 P0-1 同批） | 全仓无任何 unit 引用已退役单元名；`predict-daily.timer` 注释里的"tdxfeed-sync 18:00"同步更正 | `tests/test_no_retired_unit_refs.py`（AST 扫 `systemd/`） |
| **P1-7** | **Makefile 口径对齐生产** | ❌ `make predict` Top30 vs 生产 Top5 且无守卫；`setup`/`pipeline` 用单标的 `data-sync`；**`ci` 含 `canary`（CI 必红）** | R-仓库 | **11-05 → 11-07** | `make predict` 与 `predict-daily.service` 参数逐项一致；`ci` 不含 `canary`；`pipeline` 用 `full-sync` | `tests/test_makefile_production_parity.py` |
| **P1-8** | **涨停判定单源** | ❌ 9 处实现（含内联 `limit - 0.001`） | R-仓库 | **11-09 → 11-12** | 全仓 `limit\s*-\s*0\.001` = **0 处**；`features_5d.py` 的 `LIMIT_MAP` 改为从 limits.py 派生 | 复用现有 `tests/test_limit_single_source.py`（升级断言） |
| **P1-9** | **名字/池单源** | ❌ `norm_code` 3 份 / `is_st` 3 种语义 / `load_universe` 7 份 | R-仓库 | **11-09 → 11-15** | 全仓各实现数 = **1**（AST 守卫断言） | `tests/test_names_single_source.py` 扩展 + `tests/test_universe_path_single_source.py` 升级 |
| **P1-10** | **`state.py` 批量落盘** | ❌ 每票整文件重写 manifest → O(n²)，≈44s 序列化 + 1.8GB 写盘 | R-仓库 | **11-09 → 11-11** | 5500 只同步的 manifest 写盘 **< 2s**；崩溃后 manifest 不损坏（保留 tmp→replace） | `tests/test_state_batch_persist.py` |
| **P1-11** | **`plan_e_backtest` 拆库** | ❌ 2003 行 / 6 个消费者 / scripts→scripts 真边 11 条 | R-仓库 | **11-16 → 11-29**（**最大项**） | scripts→scripts 真边 **11 → ≤3**；`plan_e_backtest.py` ≤ 1500 行（台账 `lines_gt` 阈值） | `tests/test_no_scripts_cross_import.py`（新 AST 守卫） |

### P1 依赖图

```
P1-1（测试基座）
   ├─► P1-2（yaml 真契约）─┐
   ├─► P1-3（路由重排）───┼─► P1-4（降级双跑）   ← 同一批改 runtime/orchestrator，避免三次回归
   └─► P1-11（拆库）──────┘   ← 与 P1-1 同批测试
P1-5（MCP 校验） ── 独立，可并行
P1-6（死依赖）  ── 独立，11-02 随 P0-1
P1-7（Makefile）── 独立，可并行
P1-8 / P1-9 / P1-10 ── 互相独立，可并行
```

### P1 责任与验收细则（补充）

| 项 | 关键风险 | 缓解 |
|---|---|---|
| P1-2 | 一开始校验就会让**存量 yaml 集体变红**（如 `news-summary.topics` runtime 根本不消费） | 必须与"yaml 与实现对齐"的批量修**同批**落地，不允许只加校验不加对齐 |
| P1-3 | 裸 6 位补前缀会引入歧义（`000001` 既是 sh 指数又是 sz 平安银行） | 复用 `mcp/servers/news.py:60-70` 的既有规则（6→sh，0/3→sz），并在验收用例里固定这个约定 |
| P1-5 | schema 校验会暴露历史调用方传了非法参数 | 错误码从 -32603 改为 -32602 是**行为变更**，需在 `docs/API.md` 记一行 |
| P1-7 | `make ci` 去掉 `canary` 后，CI 不再覆盖宿主健康 | 明确分工：**CI 只管代码门禁，canary 只管宿主**，二者不再混在一条命令里 |
| P1-11 | 6 个消费者同时改 ⇒ 大爆炸 | 拆库走"先抽公共部分到 `tdxfeed/plan_e/`，薄壳保留原名"的渐进路线，每抽一块跑一次全量测试 |

---

## 🟡 P2 · 覆盖范围与推进节奏（6 项）

> P2 的推进原则：**与 P0/P1 并行、互不阻塞**，按"低成本先走"排序；每项独立提交，不打包。

| # | 项 | 覆盖范围 | 节奏 | 时限 | 验收标准 | 落地物 |
|---|---|---|---|---|---|---|
| **P2-1** | **文档漂移批量修正** | `agents/`→`skill_router/`（4 处）· **`make verify-math` 的 env 名**（`MATH_VAULT`→`VAULT/VAULT_KIND/PREFIX`）· `obsidian-git.json` 内联副本改运行时 `cat` · skills 数量 8→9 · canary 维度 7→9 · timer 清单（12 timer + 2 path） | **第一批（与 P0 同批）**，因为它只改 markdown、无回归风险 | **11-02 → 11-04** | `grep -rn "agents/orchestrator" docs/ .codebuddy/` = 0 命中；`make verify-math` 能真正切库校验 | `tests/test_docs_consistency.py`（P2-6 的一半先上） |
| **P2-2** | **裸写残留清理** | 8 处研究脚本裸 `json.dump`/`write_text`（`plan_e_v3_live_track.py:119,133` 等）→ 改走 `tdxfeed/atomic.py` | 第二批 | **11-09 → 11-12** | 全仓生产代码裸写 = 0（`SEM001` 语义守卫已能拦截新增，本项清存量） | 复用 `tests/test_sem001_convergence.py` |
| **P2-3** | **`find_double_up_c` 向量化** | 880 万次 numpy 窗口切片 → `rolling(21).max().shift(-1)`；`screen_one` 进程池化 | 第二批（须先有 golden 对比） | **11-12 → 11-16** | 用现有 `double_up_2020_2026_detail.csv` 做 golden 对比，**输出逐行一致** | `tests/test_find_double_up_golden.py` |
| **P2-4** | **`predict.py` IO 优化** | 全量读 parquet ≈840MB → `read_parquet(columns=...)` + 只读尾部 N 行（依赖 writers 已保证 date 升序） | 第二批 | **11-12 → 11-14** | 全市场预测 IO 下降 ≥ 80%；**出榜结果与优化前逐行一致** | `tests/test_predict_io_equivalence.py` |
| **P2-5** | **editable install** | 加 `pyproject.toml` + `pip install -e .`；`sys.path.insert` 73 文件 → ≤ 5；同步验证 systemd `ExecStart` 的 PATH | 第三批（**依赖 P1-11** 拆库完成，拆完 import 边界才干净） | **11-20 → 11-25** | `sys.path.insert` 文件数 ≤ 5；**所有 systemd 单元仍能正常起**（逐个 `systemctl --user start` 验证） | `tests/test_no_hardcoded_syspath.py` 升级 |
| **P2-6** | **CI 护栏（防复发）** | ① SKILL/agent md 提到的 `scripts/*` 与 systemd 单元真实存在 ② Makefile 行号引用不越界 ③ `skills/*.yaml` name 集合 == runtime 加载集合 == 文档表格条目数 ④ yaml 的 `mcp:` 引用 ⊆ `tools.json` **且被 runtime 真正调用** | 第三批（④ 依赖 P1-2 接完契约） | **11-20 → 11-26** | 故意写一条失效引用 → CI 立即红 | `tests/test_docs_consistency.py`（完整版）+ `tests/test_skill_mcp_contract.py` |

### P2 推进节奏示意

```
11-01~11-04  P2-1 文档漂移（低成本，先清）
11-05~11-12  P2-2 裸写清理（与 P1 同批跑）
11-12~11-16  P2-3 向量化 / P2-4 IO（需 golden 对比，单独跑）
11-20~11-26  P2-5 editable install / P2-6 CI 护栏（依赖 P1-11 / P1-2）
并行         P2 各项互不阻塞，各自独立提交
```

---

## 🔵 待决策（4 项 · 需你拍板）

> 每项给出：**决策依据**（可复现事实）/ **可选方案** / **建议结论** / **决策截止**。未拍板项按"建议结论"临时执行，但会标注为"暂定"。

### D1 · 容器栈定位（演示化 vs 生产化）

| 字段 | 内容 |
|---|---|
| **决策依据** | `docker-compose.yml` 引用的 `tdxfeed.scheduler` **不存在于仓库**（`tdxfeed/` 下无该模块）⇒ 容器启动即崩；`Dockerfile` COPY 的 `requirements-extra.txt` 不存在；11434 端口与宿主 Ollama 冲突（宿主 Ollama 在跑，训练线要用）。三条独立证据都指向"当前必然崩"。 |
| **方案 A（演示化）** | compose 加 `profiles:["demo"]`（默认不启）；`command` 换成真实入口 `python -m tdxfeed.full_sync`；端口改 11435；修 Dockerfile 的 COPY 通配符；README 标注"容器栈 ≠ 生产" |
| **方案 B（生产化）** | 补齐 `tdxfeed.scheduler` 模块 + 依赖清单 + 独立数据卷 + 端口隔离 + 进程编排 ⇒ **≥1 周**且与现有 systemd 双调度源冲突 |
| **方案 C（归档）** | 删 `deploy/` 或标注 DEPRECATED，只留文档 |
| **建议结论** | **方案 A（演示化）** —— 理由：本项目生产调度已收口到 systemd（系统级 `tdx-sync` + 用户级 9 只 timer），容器栈再造一套调度源就是**第三套真源**，与"每日全市场同步只有一个调度源"的项目约定直接冲突。演示化能让它"可跑可展示"而不参与生产决策。 |
| **决策截止** | **11-08**（P1-7 改 Makefile `pipeline` 时会碰到 `deploy-all`，需先定） |
| **风险** | 若选 B 而不补齐调度源，必然出现"两套调度同时跑全市场同步"（正是 10-28 刚收口掉的问题） |

### D2 · 执行研究线是否归档

| 字段 | 内容 |
|---|---|
| **决策依据** | `tdxfeed/daban/` 7 模块 + `scripts/daban_*.py` 19 脚本在主仓；研究结论已定：`Stage1 gate.passed=false`（test AUC 0.52 ≈ 随机）、`Stage2 overall_go=false`（baseline 平均净收益 -1.30%/笔）。台账 KI-EXEC-001 已 DEFERRED 至 12 月复评。**成本实据**：每轮审计都会把这些文件"重新发现"一次（KI-EXEC-001 的 `seen_count=2`），且它们贡献了 `except Exception` 吞错、P1 路由、裸写等一批治理项。 |
| **方案 A（归档）** | 移 `docs/archive/` + 把 19 脚本 / 7 模块移出扫描范围；在 KI-EXEC-001 登记替代资产位置（R7 要求）；仓库回到可维护规模 |
| **方案 B（保留待复评）** | 维持现状至 12 月；`seen_count` 继续累加，每轮审计多花注意力 |
| **方案 C（改造后保留）** | 先做"研究资产规范化"（补 logging、把 `except Exception` 收窄、给 `tdxfeed/daban/` 一个显式的"研究"包标记）⇒ 额外 ≈2 人日 |
| **建议结论** | **方案 A（归档）**，若确有 12 月复评计划则选 B。理由：`overall_go=false` 是**已定结论**而非"待验证假设"，继续留在主仓的唯一收益是"万一要复评"——而复评为真时再从 `git` 取回即可（git 是最好的归档）。**不建议 C**：为一个已判负的结论投 2 人日做工程化。 |
| **决策截止** | **11-15**（与 P1-8/P1-9 单源化同批 —— 归档后 `load_universe` / `is_st` 的副本数会直接下降，能省一半工作量） |
| **风险** | 归档时**必须**同步：① KI-EXEC-001 的 `locations` 与 `detect` 规格改写 ② `docs/DABAN_EXECUTION_RESEARCH_LINE.md` 补归档注记 ③ `tdxfeed/daban/__init__.py` 的 `__all__` 不含新模块的约定保持。**误删风险**：生产侧 `predict.py` 与 `daban` 模型是**另一条线**，不要一起归档 |

### D3 · skill 层去留（接生产 vs 归档）

| 字段 | 内容 |
|---|---|
| **决策依据** | `skill_router/`（1481 行）+ `mcp/`（6 server / 23 tool）**零生产调用方**：`systemd/` grep `skill_router\|orchestrator` = 0 命中，`Makefile` = 0 命中，`mcp/` 仅文档提及。yaml 有 7 个死字段 + 8 个死 MCP 引用；路由对"600519 怎么看"直接报错。同时 `skills/daban.yaml` 又被**外部 LLM 客户端**（skills 目录是给工具调用用的技能声明）消费，`mcp/servers/backtest.py` 的 `daban_top_picks` 与 `predict.py` 同源 ⇒ **并非全无用**。 |
| **方案 A（接生产，优先接 daban）** | 把 `daban` 技能接到可触发入口（CLI 已有 `python -m skill_router.cli`；补 `make skill-daban` 目标 + 文档登记）；同时修 P1-2/P1-3 让契约与路由可信。成本 ≈1.5 人日（接入口）+ P1 的 6 项 |
| **方案 B（接生产，接 theory）** | 同 A 但先接 `theory-reference` ⇒ **不建议**：它依赖已止损的 LLM 线，且需要 `Qwen3-14B` 本地权重（现役只有 v9 失败归档） |
| **方案 C（归档）** | `skill_router/` + `mcp/` 整体归档，`skills/*.yaml` 保留（外部客户端仍消费）但标注"本地运行时已归档"。成本 ≈0.5 人日 |
| **方案 D（维持现状）** | 不动，继续"看起来在用" |
| **建议结论** | **方案 A，但只接 `daban`**。理由：① `daban_top_picks` 与 `predict.py` 同源（29 维、3-4 秒），是全项目**唯一已有生产数据支撑的自然语言入口**，接上它立刻有真实用途；② 接生产会**倒逼** P1-2/P1-3 的契约与路由修复（否则接了也不可信），一次投资解决两个问题；③ **D 必须否决** —— "维持看起来在用"是四个选项里最贵的：它既不产生价值，又持续消耗每轮审计与文档同步的注意力（本轮就发现 14 处失效引用、8 个死 MCP 引用）。若最终选 C 而非 A，**也必须走归档而不是选 D**。 |
| **决策截止** | **11-08**（P1-1/P1-2 开工前必须定，否则测试基座不知道该覆盖哪些 skill） |
| **风险** | 接生产 ⇒ `skills/daban.yaml` 的声明必须先按 P1-2 变成真契约，否则等于把一个声明不全的入口接到生产。**顺序：P1-1 → P1-2 → 再接生产。** |

### D4 · 若接生产，先接 `daban` 而非 `theory-reference`

| 字段 | 内容 |
|---|---|
| **决策依据** | `daban`：数据源 = 生产日线 + 现役 LightGBM 模型 + `predict.py` 同源算子；耗时 3-4 秒；**只出排序研究、不下单**（声明已标注）；与 `predict.py` 的差异已明确（`predict.py` 出 Top5 全榜 + 报告，`daban_top_picks` 出候选打分）。`theory-reference`：需要 `etc/obsidian/math/` 的 7 本教材讲义 + `book_paths` 映射 + LLM 生成 ⇒ 依赖已止损的 LLM 线（v9/v9r 两代坍缩），且 `book_paths` 是 yaml 里唯一被真实消费的两个特例字段之一（另一个是 `decision_logic`）⇒ **它已被验证过能工作，但只对特定问题域有意义**，不是通用入口。 |
| **方案 A（先 daban）** | 入口 = `make skill-daban "600519 怎么看"` → `daban_top_picks` → 输出排序研究 + "不是买入指令"横幅 |
| **方案 B（先 theory）** | 入口 = 讲义库问答；**需先解决 LLM 可用性问题**（本地权重只有失败归档）⇒ 实际会退回 MCP-only 分支，价值大幅缩水 |
| **方案 C（两个都接）** | 并行接；成本 ×2，且 theory 需要先解决 LLM 后端 |
| **建议结论** | **方案 A**。理由：① 与生产决策栈（LightGBM + 规则）一致，不引入 LLM 依赖；② 数据链路已在生产跑、每天被 canary 巡检；③ 输出有"只作排序研究、不是买入指令"的明确边界，不制造新的实盘误解；④ 价值密度最高（3-4 秒出结果）。**theory-reference 建议降级为"按需脚本"**（`make skill-theory`），不进主入口。 |
| **决策截止** | 与 D3 同批，**11-08** |
| **风险** | 接入后必须保留"研究口径"横幅与"不下单"声明，否则会与 `daban-ticket` 票据线混淆（后者是人工委托单） |

---

## 📊 归属总表（26 项全覆盖，无遗漏）

| 分组 | 编号 | 项 | 台账归属 | 落地物（测试） | 责任人 | 时限 | 状态 |
|---|---|---|---|---|---|---|---|
| P0 | P0-1 | 同步哨兵 | KI-COMPETE-001 | `tests/test_sync_sentinel.py` | R-仓库+R-部署 | 11-02 10:00 | ☐ 未开始 |
| P0 | P0-2 | 系统级告警 | KI-OPS-002 | `tests/test_onfailure_scope.py` | R-仓库+R-部署 | 11-02 10:00 | ☐ 未开始 |
| P0 | P0-3 | 路径穿越 | KI-MCP-003 | `tests/test_mcp_code_validation.py` | R-仓库 | 11-01 23:40 | ☐ 未开始 |
| P0 | P0-4 | timer-install 白名单 | KI-OPS-001 | `tests/test_timer_install_scope.py` | R-仓库 | 11-01 23:40 | ☐ 未开始 |
| P0 | P0-5 | TimeoutStartSec | KI-OPS-003 | `tests/test_systemd_timeout_explicit.py` | R-仓库 | 11-01 23:40 | ☐ 未开始 |
| P1 | P1-1 | skill_router 测试（先行） | KI-SKILL-001 | 3 个测试文件 | R-仓库 | 11-04 | ☐ 未开始 |
| P1 | P1-2 | yaml 变真契约 | KI-SKILL-002 | `test_skill_router_contracts.py` | R-仓库 | 11-09 | ☐ 未开始 |
| P1 | P1-3 | 路由可预期化 | KI-SKILL-002 | `test_skill_router_route.py` | R-仓库 | 11-09 | ☐ 未开始 |
| P1 | P1-4 | LLM 降级双跑 | 新建 | `test_skill_router_route.py` | R-仓库 | 11-09 | ☐ 未开始 |
| P1 | P1-5 | MCP schema 校验 | KI-MCP-002 | `test_mcp_schema_validation.py` | R-仓库 | 11-08 | ☐ 未开始 |
| P1 | P1-6 | 死依赖清理 | KI-COMPETE-002 | `test_no_retired_unit_refs.py` | R-仓库 | 11-02 | ☐ 未开始 |
| P1 | P1-7 | Makefile 口径 | KI-MAKE-001/002 + 新建 KI-MAKE-003 | `test_makefile_production_parity.py` | R-仓库 | 11-07 | ☐ 未开始 |
| P1 | P1-8 | 涨停判定单源 | KI-SINGLE-004 | `test_limit_single_source.py` | R-仓库 | 11-12 | ☐ 未开始 |
| P1 | P1-9 | 名字/池单源 | KI-SINGLE-001/002/003 | `test_names_single_source.py` | R-仓库 | 11-15 | ☐ 未开始 |
| P1 | P1-10 | state 批量落盘 | KI-PERF-001 | `test_state_batch_persist.py` | R-仓库 | 11-11 | ☐ 未开始 |
| P1 | P1-11 | plan_e 拆库 | KI-IMPORT-001 / KI-SIZE-001 | `test_no_scripts_cross_import.py` | R-仓库 | 11-29 | ☐ 未开始 |
| P2 | P2-1 | 文档漂移批量修正 | KI-DOC-001/OBS-001/OBS-002 | `test_docs_consistency.py` | R-仓库 | 11-04 | ☐ 未开始 |
| P2 | P2-2 | 裸写残留清理 | KI-ATOMIC 延伸（新建） | `test_sem001_convergence.py` | R-仓库 | 11-12 | ☐ 未开始 |
| P2 | P2-3 | find_double_up 向量化 | KI-PERF-002 | `test_find_double_up_golden.py` | R-仓库 | 11-16 | ☐ 未开始 |
| P2 | P2-4 | predict IO 优化 | KI-PERF-003 | `test_predict_io_equivalence.py` | R-仓库 | 11-14 | ☐ 未开始 |
| P2 | P2-5 | editable install | KI-PATH-001 | `test_no_hardcoded_syspath.py` | R-仓库 | 11-25 | ☐ 未开始 |
| P2 | P2-6 | CI 护栏 | KI-SKILL-001 / KI-DOC-001 | `test_docs_consistency.py` + `test_skill_mcp_contract.py` | R-仓库 | 11-26 | ☐ 未开始 |
| 决策 | D1 | 容器栈演示化 | （新建） | — | 决策人：jiuben | 11-08 | ☐ 待拍板 |
| 决策 | D2 | 执行研究线归档 | KI-EXEC-001 | — | 决策人：jiuben | 11-15 | ☐ 待拍板 |
| 决策 | D3 | skill 层去留 | （新建） | — | 决策人：jiuben | 11-08 | ☐ 待拍板 |
| 决策 | D4 | 先接 daban | （新建） | — | 决策人：jiuben | 11-08 | ☐ 待拍板 |

> **新增台账条目建议**（3 条）：`KI-MAKE-003`（`make ci` 含 canary → CI 必红）· `KI-MCP-004`（`tools.json` 与代码的 description/inputSchema 漂移）· `KI-SKILL-003`（skill 层零生产调用方 / 整层孤岛）。按 R3 规则新建，先 grep 查重（`make ledger-probe`）。

---

## ⚠️ 风险提示

1. **本计划尚未实施任何一项**，今日仍在生效的 P0 是 **KI-COMPETE-001（数据竞态）· KI-OPS-002（系统级告警黑洞）· KI-MCP-003（路径穿越）· KI-OPS-001（告警死循环）** —— 前两项在每个交易日全程生效。
2. **P0-1 / P0-2 需要 root**，而本机进程树 `NoNewPrivs=1` ⇒ 必须在 `systemd-run --user --pty --wait` 的 nnp=0 作用域里用 zenity askpass 提权，**密码由桌面弹窗输入**。若届时无人在桌面前，这两项会顺延到下一个交易日 —— 而下一个交易日 18:05 的 predict 仍可能在"无哨兵"状态下运行（由 precheck_freshness 部分兜底）。
3. **哨兵若设计不当会误杀正常出榜**：P0-1 的双通道 + `--dry-run` 观察期是为此设置。**首次上线建议先只读观察 1 个交易日**，确认判定逻辑再开拦截。
4. **P1-2 让 yaml 变真契约会让存量 yaml 集体变红**（`news-summary.topics` 等 runtime 不消费的字段）：必须与"yaml 与实现对齐"同批落地，否则 CI 会从"绿"直接变"红"，而这会让整个 P1 批次停摆。
5. **P1-11 是最大项且有 6 个消费者**：必须与 P1-1 的测试同批推进；**单独拆会失去回归能力**，这是本项目已经吃过一次的教训（重构过程中新增吞错）。
6. **D3 若选 D（维持现状）是最贵的选项**：它不产生价值，却让每轮审计继续为失效引用与死字段付费；即使最终不接生产，也应走归档（C）。
7. **数字会漂移**：本文数量为 2026-11-01 23:05 快照（git `b4ebf40`）。执行时请以 `make ledger-report` + `make quality-trend` 的实时输出为准。

---

## 🔧 下一步建议

1. **今晚按 P0 串行路径做完 5 项的仓库改动**（≈2.5 人时），每项配测试，`make gate` + `make fix-tests-check` + 全量 pytest 过一遍再提交。
2. **明早 10:00 前用一次 root 会话部署 P0-1 / P0-2**（systemd/system 三个文件一起 cp + daemon-reload），这是本周唯一需要提权的窗口。
3. **11-02 16:30 后看第一个 sync 窗口**：确认 `data/.state/sync-done` 生成、18:05 的 predict 确实在其之后启动 ⇒ P0-1 达成。
4. **明天（11-02）拍板 D1 / D3 / D4**（三项都在 11-08 截止，但越早越好：D3 决定 P1-1/P1-2 的测试覆盖范围，D1 决定 P1-7 改 `pipeline` 的写法）。
5. **P1-1 周一开工**：三个测试文件是后续 6 项改造的地基，也是"改行为有人知道"的唯一保障。

---

生成时间：2026-11-01 23:05:00
生成者：CodeBuddy（整改与决策执行计划 · 基于逆向分析）
代码快照：git `b4ebf40`（工作树干净，本计划未提交）
覆盖条目：26 / 26（P0 5 · P1 11 · P2 6 · 待决策 4）
数据日期：2026-11-01
