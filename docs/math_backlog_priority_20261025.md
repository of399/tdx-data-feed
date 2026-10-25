# 数学子系统待办优先级重排（2026-10-25）

本文件取代 `v5/README.md` 的「待办优先级」表。旧表与 `v5/workbench/seed.json` 都冻结在
`ab3760a`（2026-09-25），两者的 todo/done 标记与代码现状双向失真，因此先做一次逐项核对，
再按核对结果重排。**下面每一条都附了可复跑的命令**——本文件是带日期的一次性快照，
不是现状规格；引用前先重跑对应命令。

## 一、逐项核对结果

| 旧表条目 | 旧表说法 | 实测现状 | 复跑命令 |
|---|---|---|---|
| D6.6 持久化 SymPy server | 待办，1 天，单调用 < 50ms | **已实现并在运行**。`math-sympy-http.service`（系统单元，`/etc/systemd/system/`，pid 1370799，今天 09:55:35 起）监听 127.0.0.1:8002；热调用实测 5.1 / 17.4 / 25.0 / 36.0 ms | `systemctl is-active math-sympy-http`、`curl -s 127.0.0.1:8002/health` |
| D3.1 推导链 DAG | 待办，2 周，"解锁全新能力" | **代码已入库**（`DerivationDAG` 在 `v5/mcp_math/derivation.py:64`，端点 `/tools/math_derivation_chain` 返回 200、17–36ms）。缺的不是建模：`derivation.py:190-210` 先判 `trivially_equal`，实测 4 组"可达"公式全部 `steps=0 nodes=0 explored=0`，只有不可达那组才真正跑了 BFS（`explored=1 rules_used=['expand'] reason=max_depth_exceeded`） | 见本文附录 A 的两条 curl |
| D2.5 DeepSeek-R1 兜底 | 待办，2 天，+20% 推理成功率，"依赖 vLLM 启动" | **已实现且线上确实在打**。兜底链 `sympy → ask → numeric → llm` 在 `v5/mcp_math/strategy_selector.py:11`，llm 层用 Ollama `deepseek-r1:14b`；`/metrics` 实测 `fallback_strategy_used` 计数 sympy=13 / llm=12 / numeric=8。**依赖前提过时**：`vllm-qwen3-judge` 两个作用域都 inactive，不影响该链路 | `curl -s 127.0.0.1:8002/metrics \| grep fallback_strategy_used` |
| D4 知识对齐 | 待办，1 周，链接质量 +30% | **确实未做**。板子里拆成 D4.1 概念本体 / D4.2 双语词典 / D4.3 MathBERT / D4.4 多模态，`roi` 各 1.0（全表最低档）；指标「跨语言对齐」baseline 0 / current 0，即测量口径本身还没建 | `venv/bin/python -c "import json;d=json.load(open('v5/workbench/seed.json'));print([t['title']+':'+t['status'] for t in d['tasks'] if t['title'].startswith('D4')])"` |
| D1.1 latex2sympy2 切换 | 待办，2 天，+15% 解析，"等新版兼容 Python 3.14" | **被上游永久阻塞，不是工作量问题**。PyPI 最新版仍是 1.9.1，发布于 **2023-04-23**，硬 pin `antlr4-python3-runtime==4.7.2`；本仓 `venv-math` 是 antlr 4.11.1 + sympy 1.14.0（`v5/requirements-math.txt:24`），装它等于把 antlr 拽回 4.7 并砸掉 sympy —— 这正是 venv-math 存在的原因（AGENTS.md） | `curl -s https://pypi.org/pypi/latex2sympy2/json` |

顺带纠正的两条失真（板子标 todo，代码已实现）：

- `D6.3 超时分级 simplify` → `v5/mcp_servers/math_sympy.py:105` 的 docstring 就是这个实现。
- `D1.2 pylatexenc AST 解析` → `v5/mcp_servers/math_sympy.py:177`，依赖已 pin 在 `v5/requirements-math.txt:20`（pylatexenc 2.11）。

## 二、重排后的优先级

| 顺位 | 做什么 | 成本 | 理由 |
|---|---|---|---|
| **P0** | 刷新 backlog（本文件 + `v5/workbench/seed.json`） | 20 分钟 | 不做的话每轮排优先级都要重跑一遍上面的核对 |
| **P1** | D3.1 决定"要不要绕过 `trivially_equal` 短路"，并补一条断言 `steps > 0` 的红测试 | 半天 | 唯一"能力已在、实测产不出链"的项；`v5/tests/test_math_sympy.py:237-244` 只断言 stdio 侧 20 个 tool 注册名，既不覆盖 HTTP 路径，也从未产出过一条非平凡推导链 |
| **P2** | 新建 50 题多步推理测集（记为 D2.6） | 半天～1 天 | `+20%`、`+30%` 这类数字现在全部不可证伪；板子里「多步推理成功率」baseline 0 / current 0，`v5/tests/` 只有 `test_llm_ollama_reason_budget.py` |
| **P3** | 修观测口径 + 追一条运维红 | 各 1～2 小时 | `derivation_cache` 报 `{"total":0,"total_hits":165,"hit_ratio":165.0}`、`math_cache hit_ratio 1.947`，命中率>1 说明公式取错分母；`tdx-verification.service` 今天 10:00:01 以 exit-code 1 失败，而其 timer 仍 enabled+active |
| **P4** | D4 知识对齐 | 1 周 | 成本最高、`roi` 最低、且收益指标从没离开 0 |
| — | D6.6 关单 | 0 | 目标已达成，慢的是 llm 兜底层（`math_strategy_route_and_execute` avg 3.72s / p95 12.31s），不是持久化层 |
| — | D1.1 移出待办 | — | 只剩"vendored 重生成语法"这一条路，远超 2 天，需要单独立项再估 |

排序原则：把"能证明收益的那一层"（P1/P2，合计不到两天）排在任何新能力之前。代价是推理能力本身晚半天动。

## 三、这次没改的东西与原因

- **`v5/README.md` 的旧表原样保留**：该文件工作树里有 17 行未提交改动（MCP client 配置段、Ollama snap 抢端口段），属于 `mcp-llm-ollama` 那次未入库的工作，与本任务无关。本仓库是多会话共享工作树，`git commit` 一旦落到这个文件就会连带把别人的改动一起提交，所以等那批改动入库后再改这张表；在此之前，**以本文件为准**。
- **改了 `seed.json` 不等于工作台板子会变**：`v5/workbench/server.py:581` 的 `POST /api/seed` 才会把 seed.json 灌进 `workbench.db`，而它先对 7 张表做 `DELETE`——重灌会丢掉板子上的手工改动。所以这里刻意没有调用它，看新优先级请显式重灌或直接读 `seed.json`。
- **工作台的运行状态（2026-10-25 实测，别把 8000/8010 当成它）**：`workbench.db` mtime 停在 2026-09-17 20:35；`v5/workbench/server.py` 默认起在 `127.0.0.1:9000`（`server.py:729`），当前既无进程、`:9000` 也未监听。在听的是两个别的程序——`:8000` 为 `/home/jiuben/mcp-shell-server.py`（`curl` 返回 401），`:8010` 为 `uvicorn app.main:app`，工作目录 `~/CodeBuddy/20260916194646/workbench/backend`。也就是说 401 不是本仓库工作台鉴权失败，而是端口根本不属于它。
- **`v5/README.md:155` 那句「D1.1（latex2sympy2 切换）**未实现**：Python 3.14 + antlr4 4.x 与 latex2sympy2 1.9.1 不兼容」仍然成立**（同一措辞另见 `:194` 和 `:343` 的旧表行），本文只是把「等新版」这个前提证伪了：1.9.1 已是最新版，2023-04-23 发布后再没动过。

## 附录 A：复跑 D3.1 的两条命令

```bash
# 1) 可达（预期 ok=true 但 steps=0，即被 trivially_equal 短路）
curl -s -X POST http://127.0.0.1:8002/tools/math_derivation_chain \
  -H 'Content-Type: application/json' \
  -d '{"start_latex":"(x+1)**2","target_latex":"x**2+2*x+1"}'

# 2) 不可达（预期 reason=max_depth_exceeded 且 explored>0，即 BFS 真跑了）
curl -s -X POST http://127.0.0.1:8002/tools/math_derivation_chain \
  -H 'Content-Type: application/json' \
  -d '{"start_latex":"(x+1)**2","target_latex":"x**2+3*x+1","rules":["expand","simplify","factor"]}'
```

注意 schema 的必填键是 `start_latex` / `target_latex`（`v5/mcp_servers/math_sympy.py:1033-1053`），
传 `start` / `target` 会得到 500，那是参数错、不是服务坏。
