# AGENTS.md · tdx-data-feed 作业约定

只收「从代码读不出来、但会改变动作」的约定。架构与上手见 `README.md`、`docs/ARCHITECTURE.md`。

## 命令（都在仓库根执行）

- Python 走仓库自己的 venv：`venv/bin/python`、`venv/bin/pytest`。系统 python3（3.12）没装本仓库依赖。
- **例外：v5 数学服务不在主 venv 里跑。** `v5/mcp_servers/math_sympy_http.py`（系统单元 `math-sympy-http.service`，8002）用第二套解释器 `venv-math/bin/python`，建法 `bash v5/scripts/install_math_venv.sh`（幂等，清单在 `v5/requirements-math.txt`）：sympy 1.14 的 `parse_latex` 要求 antlr4-python3-runtime 4.11，而主 venv 的 4.9.3 被 `omegaconf==2.3.1`（`llamafactory` 带入）以 `==4.9.*` 锁死，两约束在同一解释器里不可同时满足。`pip check` 看不见这个冲突（antlr 对 sympy 是 optional extra），所以它的干净输出**不是**「数学服务能解析 LaTeX」的证据——判断能不能解析，要用 `venv-math` 真跑一次 `parse_latex`（守卫 `tests/test_math_venv_latex_parsing.py`）。同一份代码放进主 venv，症状是 sympy 段整片抛 ImportError、回退链静默掉到 llm，退出码仍是 0。这条不限于服务进程：**任何执行数学代码的地方**（跑数学 harness 的脚本、MCP 注册片段里的 `command`）都要用 `venv-math`，否则测出来的是「主 venv 能不能解析 LaTeX」——`v5/scripts/tdx_verification.py` 就因此从 2026-09-22 起天天红（33 份审计产物里 27 份 red），而 `make test` 与根 `tests/` 一直是绿的（那份 harness 有 132 条断言，`make test` 一条也收不到）。守卫 `tests/test_math_venv_latex_parsing.py` 扫全部跟踪源文件来兜这条，权威解释器取自 `math-sympy-http.service` 的 `ExecStart`。
- 改 `v5/systemd/math-sympy-http.service` 只是改了仓库里的文件，**线上不会自己变**。生效要 `cp` 到 `/etc/systemd/system/` + `systemctl daemon-reload` + `systemctl restart math-sympy-http` 三步，缺一步就是磁盘新、加载态旧或进程旧。守卫 `tests/test_math_unit_deployed_matches_repo.py` 分层抓这三种漂移（只读，不需 root）。本机从 Qoder 起的进程树带 `NoNewPrivs=1`，`sudo`/`pkexec` 都拿不到 root；要执行这三步，用 `systemd-run --user --pty --wait` 起一个 nnp=0 的作用域，在里面 `SUDO_ASKPASS=<zenity 脚本> sudo -A ...`（密码由桌面弹窗输入，不落命令行）。
- 装依赖：`make install`。结尾 `pip check` 报的 mootdx 声明 httpx<0.26 与实装 0.28 是已知声明性冲突，不是新引入的故障。
- 测试有两个互不覆盖的集合：`make test` 现在两个都跑（`tests/` + `v5/tests/`），`make test-v5` 只跑后者。改单个模块用 `venv/bin/pytest tests/test_plan_e_random_timing.py -q`。（两边的文件数会随提交变，别按数字判断覆盖范围。）
- CI 的门禁作业是 `.github/workflows/tests.yml`：只装 `requirements-ci.txt`（精简集，不含训练栈与行情源），跑同一批用例；`tests/` 里引用 `data/` 或绝对路径的模块由根 `conftest.py` 在 `data/` 缺失时收集期跳过（本机 `data/` 在，一个都不跳）。演练方式：`TDX_TEST_DATA_ROOT=/nonexistent venv/bin/pytest tests/ -q`。守卫 `tests/test_ci_data_skip.py`。作业末尾有"通过数下限"断言，空跑不会变绿。
- `v5/tests/` 里打 HTTP 端点的集成测试**自起自停**：`v5/tests/conftest.py` 的 `math_api` fixture 按 `v5/systemd/math-sympy-http.service` 里的解释器与 Environment 现起一个实例、端口向内核要，所以 `make test` 绿不依赖线上 8002，也不需要 root。要验线上部署本身，显式设 `MATH_API_BASE=http://127.0.0.1:<线上端口>` 跑同一批用例。守卫 `tests/test_math_api_self_hosted.py`。
- 门禁分层：`make lint` 只覆盖 `v5/`；`make lint-all` 含 `scripts/`，**阻断**语法错误与未定义名（E9,F63,F7,F82），其余约 200 条存量规则只报告不阻断；CI 的 `lint.yml` 同一分法。`make typecheck` 对 v5 的存量 mypy 错误仍不阻断（只在没装 mypy 时报错），别把它当通过证据。
- 改 `systemd/` 下任何单元后必须 `make timer-install`，再 `systemctl --user daemon-reload`；`.path` 单元还要 restart，处于 start-limit-hit 的要 reset-failed，否则 systemd 加载的仍是旧文件。
- **用户级 ≠ 无人登录也在跑**：`systemd/` 下的单元活在 `--user` 管理器里，靠 `loginctl show-user jiuben` 的 `Linger=yes` 撑着（2026-10-29 实测为 yes）；关掉 linger，`predict-daily` / `canary-health` / `daban-ticket` 这些会在会话退出后一起停。排查「timer 没触发」先看这一项，不要只看 `systemctl --user list-timers`。层级按文件位置判：`systemd/` = 用户级，`systemd/system/` = 系统级，同名单元两边都可能存在。
- `systemd/system/` 是**系统作用域**单元的仓库镜像（`/etc/systemd/system/` 下引用本仓库的那批：tdx-sync / tdx-monitor / tdx-verification / audit-log-rotate / daily-fallback-report / evolution_daily_report / scheduler_agent / suggestion_agent / tdx-autonomy / tdx-evolution / pushplus_bridge）。`make timer-install` **不碰**它们，改了也只改仓库、线上不变；部署要 root（走上面那条 `systemd-run --user` + zenity 路径）。漂移守卫 `tests/test_systemd_system_units_mirror.py` 双向比对：/etc 多出引用仓库的单元而仓库没镜像、或两边字节不一致，都红。`math-sympy-http.service` 不在镜像里（它在 `v5/systemd/`，有自己那套分层守卫）。
- 每日全市场同步只有一个调度源：系统级 `tdx-sync.timer`（工作日 16:30，脚本 `python -m tdxfeed.full_sync`）。用户级 `tdxfeed-sync.timer`（18:00）已于 2026-10-28 退役，文件留在 `systemd/` 只为回滚，**不要再进 `make timer-enable`**。`predict-daily` 不再 `Requires=` 同步单元，数据是否到位由它的 `ExecStartPre=scripts/precheck_freshness.sh` 判定（同步没跑成 → exit 2 → 整轮不跑 + OnFailure 告警）。
- 同步成功后由 `tdx-sync.service` 的两行 `ExecStartPost=-` 续跑：`python -m tdxfeed.precompute.indicators --incremental`（派生表，全市场约 1 分钟，实测 `compute_one` 约 0.008 秒/只 × 5211）与 `python -m tdxfeed.adj_factor`（复权因子增量，**全量约 60 分钟**：5221 只 × 约 0.6 秒，含除权后整表重拉的标的；不是 5 分钟）。`-` 前缀表示后置任务失败不把整轮同步判红——数据是否真的新鲜由 canary 的 `data_files` 检查兜底（`tests/test_canary_table_freshness.py`），而 `data_files` 在 `PAGING_CHECKS` 里，所以停更仍会 exit 1 告警。
- `data/adj_factor` 的 `date` 列是 **`date32[day]`**（不是 string、也不是 timestamp），写回时保持 `datetime.date` 对象，别 `astype(str)`；历史由 `scripts/download_adj_factor_b.py` 一次性回填，日常更新只走 `tdxfeed/adj_factor.py`（旧脚本命中已存在文件即跳过）。`data/.state/manifest.json` 的 `last_date` 是 int `YYYYMMDD`。
- canary 的退出码就是告警开关：`PAGING_CHECKS`（`tdxfeed_sync` / `data_files` / `timers`）出现 warn 也会 exit 1，其余 warn（GPU/磁盘）只进 journal——长期项每 5 分钟推一次就是刷屏。同步新鲜度取自 `tdx-sync.timer` 的 `LastTriggerUSec`，不要退回 `ActiveEnterTimestamp`（oneshot 被 GC 后恒为空，年龄检查会整段跳过）。

## 回测成交口径（不可协商）

- 同一根 K 线双触发时结算**亏损**侧（日内高低点证明不了先后）；跳空穿越触发价时按当日**开盘**成交，不要抹回阈值价；出场价回灌逐日收益，使资金曲线与 return_pct 同源。
- `scripts/plan_e_backtest.py` 的 `_settle_from_entry` 是内核 A 的薄封装，回测与随机择时基线共用它。新脚本复用它，不要另写判定顺序——出场阶梯一分叉，基线比较就失去意义（守卫 `tests/test_plan_e_random_timing.py`）。
- `scripts/plan_e_sim_live.py` 已对齐同一口径。引用 2026-10-24 之前的报告或 README 里的 3.16% / 35.3% / 2.71 之前，先确认它是保守修正前口径；当前口径文件只有 `data/plan_e/plan_e_backtest_v2_latest.md`。
- 唯一实现已收进 `tdxfeed/execution/`：内核 A（`single.py`，单笔 TP/SL 两层：`check_exit` 判阶梯、`settle_single_trade` 跑窗口）与内核 B（`hold.py`，打板持有期），共用 `limits.py` / `costs.py` / `bars.py` / `result.py`。新脚本从内核取口径，不要另写判定顺序。`scripts/plan_e_backtest.py::_settle_from_entry` 是内核 A 的薄封装（委托守卫 `tests/test_plan_e_backtest_exit_fills.py::test_settle_from_entry_delegates_to_kernel`），随机化口径回归在 `tests/test_settle_kernel_parity.py`。

## 显著性推断四条约定（已定，勿重开讨论）

- Deflated Sharpe 的 SR 必须是每期值；年化值只能经 `dsr_p_from_annual_sharpe` 换算，否则 z 被放大 √252，0.08 的夏普会伪装成 p=0.010。
- 置换检验默认统计量是 lag-1 自相关（`quant/multifactor/cv_utils.py` 的 `permutation_return_test`）：持仓按天摊平天然造出正自相关，小 p 只说明曲线被平滑过，**不是**择时 alpha。
- 胜率二项检验的原假设是赔付结构隐含的盈亏平衡胜率（`breakeven_win_rate`），不是 50%。止盈 +30% / 止损 −10% 下任何零边际策略都显著偏离 50%，测 50% 只是赔付结构的同义反复。
- 随机择时基线（`--random-timing-trials`，默认 300）只随机化入场日、标的池不动，属**条件**推断：标准误来自同票日际波动，比跨标的的 σ≈20.9%/笔 小一个量级，既不能证明选股有效，也不能外推到没见过的票。
- 四者的完整措辞由 `plan_e_backtest.py` 生成的报告段落承载；`docs/math_upgrade_plan_20261015.md` 正文已按此更正，只有 M1 变更记录留旧措辞。

## 训练标签口径

- `hypothetical_pnl` 由 `scripts/train_pnl_proxy.compute_hypothetical_pnl` 产出，已委托 `tdxfeed/execution/` 的内核 A。2026-10-28 起标签口径为：入场次根起判定、同根双触发结算亏损侧、跳空按开盘；成本也从加法 `gross − 2×(fee+slip)` 改为内核 `CostModel.round_trip` 的乘法。**此前训练的模型与报告都是旧口径**，横比前先看标签版本（`data/double_up/_pre_settle_kernel_20261028/` 是旧版快照）。本轮重训的对照与取舍见 `docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md`。
- 标签 CSV 仍是 13064 行，其中 **3083 行（bj 1539 + ETF 1544）按设计为 null**（没有日线）。统计任何比率都必须用**非空分母**（9981），不能用 13064——本轮踩过两次：51.9% 正收益率、12957 行合并叉积。
- 重算入口是 `scripts/recompute_pnl_labels.py`（只换标签列 + 写前闸门）；**不要**指望 `scripts/train_pnl_proxy.py` 的 `main()` 重算——它非空率 >90% 时会复用旧列。

## 实盘边界

- 方案 E 与 daban 已于 2026-10-24 退出实盘入口：`--mode daily` 不再逐笔算下单量，盘前清单仅作研究产出；`--mode buy/sell` 只用于人工记账。不要删这两个模式，也不要归档已退役的单元文件。
- 恢复 `plan-e-today` / `plan-e-journal` 定时器恢复的是清单自动生成，不等于接入实盘。`plan_e_journal.py` 里 `PLAN_E_PREMARKET_LIST_ACTIVE = False` 是有意关闭的（18:10 那轮读的是当日收盘，entry_date 落在明天，清单必被新鲜度守卫丢掉），不要当缺陷打开。

## 模型与蒸馏

- 源码里出现的教师 tag 一律不可信，包括写死的默认值：`tdx-lora:latest` 在本仓库没有任何生产者（无 Modelfile、无 create 步骤），本机也早已不存在。跑之前用 `ollama list` 或 `make deploy-list` 挑一个真实存在的 tag，并先确认当前这份脚本从哪儿读它——显式传环境变量的入口是后加的，旧版本会无视它继续用写死的 tag。
- 蒸馏「跑成功」不等于答案非空：`main()` 自己不做非空校验，它只把 `call()` 交回来的东西写进行并打 `DISTILL_DONE`——所以整片空语料的形状是「退出码 0 + 行数对得上」。这条静默与否取决于 `call()` 的实现、会随版本漂，口径因此固定：**长跑前先用小批量 `LIMIT` 出一批，人工确认 output 非空再放开**。
- `scripts/distill_outputs.py` 的输出是**追加**续跑（按已写行数跳过），skeleton 变更后要把输出指到新文件，否则两套口径混进同一个语料。

## 历史产物不是现状

- `_archive/`、`data/`、`logs/`、`models/`、`downloads/` 不用于推断架构；`docs/` 下带日期的 `CODE_AUDIT_*`、`COMPARE_*`、`*_RESEARCH_*` 是一次性研究快照，写作历史结论而非现状规格。
- 根 `skills/*.yaml` 是给 LLM 工具调用用的技能声明，与 Qoder 客户端加载的 Skill 目录 `.qoder/skills/` 无关；后者是**入库**的（回测口径自检 `tdx-backtest-significance` 在那里），改口径措辞要改那份 `SKILL.md`；`.qoder/repowiki/wiki_plan.yaml` 只影响知识生成，不是运行配置。

## 数据路径与表口径

- **表路径**（daily / min / derived / finance / xdxr / adj_factor）只从 `tdxfeed/paths.py` 取：脚本侧 `from _common import ...` 是它的再导出桥，`v5/scripts/_paths.py` 是零耦合孪生（v5 不 import tdxfeed）。业务代码不写 `data/parquet/...` 字面量，守卫 `tests/test_no_hardcoded_data_paths.py`。**名单与模型路径尚未统一**：`paths.universe_path()` 已有，但 `scripts/predict.py`、`mcp/servers/*` 等仍自拼 `data/all_a_stocks.json`（见规格 §12 遗留项 13）。
- 表名**不统一**，按既存事实：日线/分钟表文件带市场前缀（`sh600000.parquet`），`adj_factor` / `hfq_factor` 是裸 6 位（`600000.parquet`），`derived` 是前缀 + `_indicators`（`sh600000_indicators.parquet`）。要枚举标的池用 `paths.daily_files()`——它排除 `.v1.bak.parquet` 迁移备份；自己 `glob("*.parquet")` 会把备份当股票。
- **`data/hfq_factor` 是陈旧陷阱（2026-10-29 实测）**：5221 个文件的末日全部停在 **2026-09-24**（落后十余个交易日），**没有任何调度**在刷新它；`scripts/download_hfq_factor.py` 命中已存在文件即返回 `"cached"`，不会更新。消费方只有 `scripts/verify_v2.py` 与研究脚本，**不要当权威复权序列用**——权威是 `adj_factor`。回填与调度留到后续批次。
- **在役的日线与派生表**写入只走 `tdxfeed.parquet_io.write_table`（`date` 列归一成 `date32[day]`，tmp→`os.replace` 换 inode）：`full_sync`（`Writers.merge_write`）、`scripts/download_universe.py`、`scripts/migrate_v1_to_v2.py`、`tdxfeed/amount_fix.py`（委托 `Writers._write_parquet_date32`）、`tdxfeed/precompute/indicators.py`、`tdxfeed/incremental.py`（`_write_v2`）都经它；守卫 `tests/test_write_entry_single_source.py` 钉死三个生产脚本不得再出现 `.to_parquet` 直写。派生表备份依赖这个换 inode 语义，`cp -al` 出来的快照才不会被原地改写。**其余表不归这条约定**：分钟表（`tdxfeed/kline.py`）、`finance`/`xdxr`（`tdxfeed/fin_parquet.py`）是裸 `to_parquet`，既无 `date32` 归一也无换 inode；`adj_factor_ext`（`tdxfeed/adj_factor_history.py`）有 tmp→`os.replace` 但日期落 `int64`；`adj_factor` 系列与研究脚本产物写法各异；退役的 `scripts/download_bj_daily.py` 写日线同样是裸 `to_parquet` + timestamp 日期（见规格 §12 遗留项 8）。分钟表的时间列**不叫** `date`、是 `string`（`YYYY-MM-DD HH:MM:SS`），原样写。
- 复权因子的日常刷新走 `venv/bin/python -m tdxfeed.adj_factor`（`make data-adj` 是同一入口）；`scripts/download_adj_factor_b.py` 只是一次性回填，命中已存在文件即跳过，拿它当更新器用不会刷新任何数据——2026-10 停更五周就是这么来的。这条链不归 `write_table` 契约：tmp→`os.replace`，`date` 保持 `date32[day]`，别按字符串处理。
- 判涨停用 `limit_threshold`（精确比例减 0.5% 缓冲），算挂单价用 `limit_ratio` + `limit_up_price`（精确比例、Decimal 取整到分）。两者是两套合同，不可互换；两者都在 `tdxfeed/execution/limits.py`。
- **估算成交额和真实成交额同列同 dtype**：`amount<=0 & volume>0` 时按 `close×volume(手)×100` 补，daily 仍是 7 列。要区分就读旁路清单 `data/.state/amount_estimated.json`（`tdxfeed.amount_fix.estimated_dates`，格式 `{"sh600000": [20260105, ...]}`），它由 `Writers.merge_write` / `incremental` / `backfill_daily_dir` 维护；清单是进程内缓冲、退出时落盘（`flush_estimated`），同一 symbol 整段替换，只有某次写入拿到真实 `amount>0` 才撤销标记——估算值留在表里，别指望靠值去猜。
- `data/`、`logs/` 不进 git。`derived` 全量重建的证据是 `data/parquet/derived_pre_<日期>/MANIFEST.txt` + 重建日志，不是「文件数看起来对」。

## 风险边界（动手前读）

- 这个 checkout 与并发的其他代理会话共享同一工作树与 git 索引：`git add`、`git commit` 都**必须带显式路径**，不要用 `-A` 或 `.`。看到陌生脏文件或红色测试，先判断是不是别的会话的改动，不要顺手修或提交。
- `data/`、`logs/`、`models/` 都在 gitignore 里：就地改写训练语料或数据前先做带日期备份，git 救不回来。
- 不提交 `.env*` 或任何凭据；推送前扫一遍 diff。远端是 SSH 且没有 gh CLI，验证推送结果用 `git ls-remote`。
- force push、`reset --hard`、删分支前先量化分叉、给出备份 ref，再等确认。
