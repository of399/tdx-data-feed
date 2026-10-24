# AGENTS.md · tdx-data-feed 作业约定

只收「从代码读不出来、但会改变动作」的约定。架构与上手见 `README.md`、`docs/ARCHITECTURE.md`。

## 命令（都在仓库根执行）

- Python 走仓库自己的 venv：`venv/bin/python`、`venv/bin/pytest`。系统 python3（3.12）没装本仓库依赖。
- **例外：v5 数学服务不在主 venv 里跑。** `v5/mcp_servers/math_sympy_http.py`（系统单元 `math-sympy-http.service`，8002）用第二套解释器 `venv-math/bin/python`，建法 `bash v5/scripts/install_math_venv.sh`（幂等，清单在 `v5/requirements-math.txt`）：sympy 1.14 的 `parse_latex` 要求 antlr4-python3-runtime 4.11，而主 venv 的 4.9.3 被 `omegaconf==2.3.1`（`llamafactory` 带入）以 `==4.9.*` 锁死，两约束在同一解释器里不可同时满足。`pip check` 看不见这个冲突（antlr 对 sympy 是 optional extra），所以它的干净输出**不是**「数学服务能解析 LaTeX」的证据；实况是 sympy 段一路抛 ImportError、回退链掉到 llm。守卫 `tests/test_math_venv_latex_parsing.py`。
- 装依赖：`make install`。结尾 `pip check` 报的 mootdx 声明 httpx<0.26 与实装 0.28 是已知声明性冲突，不是新引入的故障。
- 测试有两个互不覆盖的集合：`make test` 只跑 `v5/tests/`（6 个文件），主测试集在根 `tests/`（48 个文件），必须显式 `venv/bin/pytest tests/ -q`。改单个模块用 `venv/bin/pytest tests/test_plan_e_random_timing.py -q`。
- 门禁分层：`make lint` 与 `make typecheck` 只覆盖 `v5/`；`make lint-all` 才含 `scripts/`，但它放宽 E501 并以 `|| true` 收尾，退出码恒 0，不要把它当通过证据。
- 改 `systemd/` 下任何单元后必须 `make timer-install`，再 `systemctl --user daemon-reload`；`.path` 单元还要 restart，处于 start-limit-hit 的要 reset-failed，否则 systemd 加载的仍是旧文件。

## 回测成交口径（不可协商）

- 同一根 K 线双触发时结算**亏损**侧（日内高低点证明不了先后）；跳空穿越触发价时按当日**开盘**成交，不要抹回阈值价；出场价回灌逐日收益，使资金曲线与 return_pct 同源。
- 唯一实现是 `scripts/plan_e_backtest.py` 的 `_settle_from_entry`，回测与随机择时基线共用它。新脚本复用它，不要另写判定顺序——出场阶梯一分叉，基线比较就失去意义（守卫 `tests/test_plan_e_random_timing.py`）。
- `scripts/plan_e_sim_live.py` 已对齐同一口径。引用 2026-10-24 之前的报告或 README 里的 3.16% / 35.3% / 2.71 之前，先确认它是保守修正前口径；当前口径文件只有 `data/plan_e/plan_e_backtest_v2_latest.md`。

## 显著性推断四条约定（已定，勿重开讨论）

- Deflated Sharpe 的 SR 必须是每期值；年化值只能经 `dsr_p_from_annual_sharpe` 换算，否则 z 被放大 √252，0.08 的夏普会伪装成 p=0.010。
- 置换检验默认统计量是 lag-1 自相关（`quant/multifactor/cv_utils.py` 的 `permutation_return_test`）：持仓按天摊平天然造出正自相关，小 p 只说明曲线被平滑过，**不是**择时 alpha。
- 胜率二项检验的原假设是赔付结构隐含的盈亏平衡胜率（`breakeven_win_rate`），不是 50%。止盈 +30% / 止损 −10% 下任何零边际策略都显著偏离 50%，测 50% 只是赔付结构的同义反复。
- 随机择时基线（`--random-timing-trials`，默认 300）只随机化入场日、标的池不动，属**条件**推断：标准误来自同票日际波动，比跨标的的 σ≈20.9%/笔 小一个量级，既不能证明选股有效，也不能外推到没见过的票。
- 四者的完整措辞由 `plan_e_backtest.py` 生成的报告段落承载；`docs/math_upgrade_plan_20261015.md` 正文已按此更正，只有 M1 变更记录留旧措辞。

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

## 风险边界（动手前读）

- 这个 checkout 与并发的其他代理会话共享同一工作树与 git 索引：`git add`、`git commit` 都**必须带显式路径**，不要用 `-A` 或 `.`。看到陌生脏文件或红色测试，先判断是不是别的会话的改动，不要顺手修或提交。
- `data/`、`logs/`、`models/` 都在 gitignore 里：就地改写训练语料或数据前先做带日期备份，git 救不回来。
- 不提交 `.env*` 或任何凭据；推送前扫一遍 diff。远端是 SSH 且没有 gh CLI，验证推送结果用 `git ls-remote`。
- force push、`reset --hard`、删分支前先量化分叉、给出备份 ref，再等确认。
