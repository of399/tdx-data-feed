# 审计轮次摘要 · 2026-11-01

> 本报告是**台账机制落地后的第一轮**，用来验证"每次都是新漏洞"的错觉能否被消除。
> 读法：栏 1 说明本轮换了什么尺子；栏 2 把发现拆成 **新增（NEW）/ 复现（RECURRING）/ 消失（RESOLVED）**。
> 台账：`config/known_issues.yaml`（36 条）· 工具：`scripts/audit_ledger.py` · 模板：`docs/templates/AUDIT_REPORT_TEMPLATE.md`

## 栏 1 · 口径差异（本轮 vs 上一轮）

> 填写规范：逐行写清**扫描范围**变化；无变化写「与上轮一致」。
> 目的：让读者能区分「问题变多」与「扫得更全」。

| 维度 | 本轮 | 上一轮 | 差异 | 是否影响结论 |
|---|---|---|---|---|
| 扫描范围 | **台账化：36 条已知问题 detect 全量复扫** | 6 份报告自由扫描（≈145 条发现）| 从"每次重扫全仓"改为"只扫已知问题 + 显式登记新问题" | **是**——本轮唯一口径变化，影响是**结论可复现** |
| 复检条目 | 35 条（排除 ARCHIVED）；FIXED 条目按守卫校验 | — | 新增 FIXED 判定口径 | 是（守卫缺失会显式报出）|
| 静态检查 | ruff 4 类阻断 + 全规则仅报告 | 同 | 与上轮一致 | 否 |
| 运行时巡检 | canary 8 维 + onfailure(user scope) | 同 | 与上轮一致 | 否 |
| 排除范围 | docs/archive/、.codebuddy/memory/ | 本轮新增 | 归档与记忆日志不再计入漂移命中 | 是（降噪）|

**口径变化说明**：本轮把"发现"从**自由扫描**改为**台账复扫 + 显式登记**。扫描条数由 145 → 35 不是"问题变少了"，而是"**已知问题被固定下来，新问题必须显式登记**"。

## 栏 2 · 发现分类：新增 vs 复现

> 填写规范：**新增**=台账中不存在、本轮首次发现（须同时完成新建条目 R3）；
> **复现**=台账中已有、本轮再次被检出（只更新 last_seen/seen_count，R2）；
> **消失**=台账中已有、本轮未再检出（可考虑置 FIXED，但 R5 要求先绑 guard）。

| 分类 | 条数 | 清单 |
|---|---:|---|
| **NEW 本轮新增** | 0 | — |
| **RECURRING 历史复现** | 35 | KI-COMPETE-001, KI-COMPETE-002, KI-ATOMIC-001, KI-ATOMIC-002, KI-ATOMIC-003, KI-ATOMIC-004, KI-SILENT-001, KI-SILENT-002, KI-MCP-001, KI-MCP-002, KI-MCP-003, KI-OPS-001, KI-OPS-002, KI-OPS-003, KI-SKILL-001, KI-SKILL-002, KI-SINGLE-001, KI-SINGLE-002, KI-SINGLE-003, KI-SINGLE-004, KI-PATH-001, KI-PATH-002, KI-PERF-001, KI-PERF-002, KI-PERF-003, KI-SIZE-001, KI-IMPORT-001, KI-DOC-001, KI-DOC-002, KI-OBS-001, KI-MAKE-001, KI-MAKE-002, KI-RULE-001, KI-OBS-002, KI-EXEC-001 |
| **RESOLVED 本轮消失** | 0 | — |
| **FIXED 已修且守卫在位** | 1 | KI-SINGLE-005 |
| **FIXED 但守卫缺失** | 0 | — |

### 复现条目明细（按 severity）

| ID | Sev | 主题 | 复发 | 守卫 | 期限 | 标题 |
|---|---|---|---:|---|---|---|
| KI-ATOMIC-001 | P0 | atomicity | 1 | NONE | 2026-11-10 | plan_e_today.csv 非原子写，读者可能读到半截文件 |
| KI-ATOMIC-002 | P0 | atomicity | 1 | NONE | 2026-11-10 | positions.json 裸 write_text，无原子写无备份，崩溃即丢持仓 |
| KI-ATOMIC-004 | P0 | atomicity | 2 | NONE | 2026-11-10 | 训练脚本默认覆盖现役模型文件，与 predict 读取存在竞态 |
| KI-COMPETE-001 | P0 | concurrency | 1 | NONE | 2026-11-10 | predict 与系统级 sync 跨 manager 无序，可能读到半新半旧数据 |
| KI-MCP-001 | P0 | timeout | 1 | NONE | 2026-11-10 | MCP 客户端 timeout 参数从未生效，readline 无限阻塞且持锁 |
| KI-MCP-003 | P0 | contract | 1 | NONE | 2026-11-10 | parquet_reader 的 code 参数无格式校验，存在路径穿越 |
| KI-OPS-001 | P0 | ops | 1 | NONE | 2026-11-08 | make timer-install 白名单漏 obsidian-sync，其修复提示形同无效 |
| KI-OPS-002 | P0 | ops | 1 | NONE | 2026-11-12 | onfailure 告警只查 user scope，系统级单元失败不可见 |
| KI-SILENT-001 | P0 | silent-failure | 4 | NONE | 2026-11-20 | 盲吞错 except Exception（ruff BLE001 目录级豁免，新代码同样豁免） |
| KI-SINGLE-004 | P0 | single-source | 3 | tests/test_limit_single_source.py | 2026-11-15 | 涨停判定 9 处实现（含内联 limit-0.001），常量来源不一致 |
| KI-ATOMIC-003 | P1 | atomicity | 1 | NONE | 2026-11-15 | journal.csv 读-改-写追加且无文件锁，并发下丢行 |
| KI-COMPETE-002 | P1 | concurrency | 1 | NONE | 2026-11-10 | plan-e-double-up 依赖已退役的 tdxfeed-sync.service，成为死依赖 |
| KI-IMPORT-001 | P1 | size | 1 | NONE | 2026-12-05 | 13 条 scripts→scripts 跨脚本 import |
| KI-MAKE-001 | P1 | make | 1 | NONE | 2026-11-15 | make predict 与生产 predict 口径不一致（模型/Top/守卫全不同） |
| KI-MAKE-002 | P1 | make | 1 | NONE | 2026-11-15 | make journal-buy/journal-sell 是已废止的建仓入口 |
| KI-MCP-002 | P1 | contract | 1 | NONE | 2026-11-20 | 6 个 MCP server 均无 JSON Schema 运行时校验，参数错误被吞成内部错误 |
| KI-OBS-002 | P1 | docs | 1 | NONE | 2026-11-15 | obsidian-math-verify skill 的 env 名与 Makefile 实际读取的不一致，变量被静默忽略 |
| KI-OPS-003 | P1 | ops | 1 | NONE | 2026-11-08 | predict-daily 等单元缺 TimeoutStartSec，user manager 默认 90s 卡在边缘 |
| KI-PATH-001 | P1 | hardcode | 5 | tests/test_no_hardcoded_syspath.py | 2026-12-05 | 73 个文件含 sys.path.insert（Round1 时 27 处，规模反而扩大） |
| KI-PERF-001 | P1 | perf | 1 | tests/test_write_atomicity.py | 2026-11-20 | tdxfeed/state.py 每只股票整文件重写 manifest，O(n²) |
| KI-RULE-001 | P1 | rule | 2 | NONE | 2026-11-25 | ruff 对 BLE001 做目录级豁免，等于新代码也可盲吞错 |
| KI-SILENT-002 | P1 | silent-failure | 3 | NONE | 2026-11-20 | except 分支直接 pass，失败不可追溯 |
| KI-SINGLE-001 | P1 | single-source | 6 | tests/test_names_single_source.py | 2026-11-25 | norm_code 仍有 3 份实现，代码归一口径可能分叉 |
| KI-SINGLE-002 | P1 | single-source | 3 | NONE | 2026-11-25 | is_st 存在 3 种语义实现 |
| KI-SIZE-001 | P1 | size | 4 | NONE | 2026-12-20 | plan_e_backtest.py 2003 行、main 265 行，事实上的第二个库 |
| KI-SKILL-001 | P1 | skill | 1 | NONE | 2026-11-20 | skill_router 零测试，路由与契约无任何回归保护 |
| KI-SKILL-002 | P1 | skill | 1 | NONE | 2026-11-25 | 技能 yaml 的 schema/timeout/priority/tools 全是死字段 |
| KI-DOC-001 | P2 | docs | 1 | NONE | 2026-11-15 | 文档中 agents/ 目录引用失效（真实为 skill_router/） |
| KI-DOC-002 | P2 | docs | 1 | NONE | 2026-11-15 | canary 维度文档写 7 维、实际 8 维 |
| KI-EXEC-001 | P2 | size | 2 | NONE | 2026-12-20 | 执行研究线 overall_go=false，但相关脚本与模块仍在主仓且未归档 |
| KI-OBS-001 | P2 | docs | 1 | NONE | 2026-11-15 | obsidian-git.json 在 skill 中内联副本与仓库源文件不一致 |
| KI-PATH-002 | P2 | hardcode | 3 | tests/test_no_hardcoded_data_paths.py | 2026-12-05 | 硬编码绝对路径散落多文件 |
| KI-PERF-002 | P2 | perf | 1 | NONE | 2026-12-05 | find_double_up_c 逐窗口 numpy 切片，全市场约 880 万次调用 |
| KI-PERF-003 | P2 | perf | 1 | NONE | 2026-12-05 | predict.py 全量读取日线 parquet，约 840MB 无用 IO |
| KI-SINGLE-003 | P2 | single-source | 1 | tests/test_universe_path_single_source.py | 2026-12-05 | load_universe 有 7 份副本，清单路径口径分散 |

**复发风险**：29/35 条复现问题**仍无自动守卫**，下一轮很可能再次被检出。

## 台账更新记录

- 本轮复检 35 条；更新 last_seen/seen_count：35 条（可用 `--update` 自动回写）
- 新建条目：见栏 2 的 NEW 清单（每条须含 detect 规格，否则无法复扫）
- 关闭条目：须满足 R5（status=FIXED + 指向真实存在的 guard 文件）
