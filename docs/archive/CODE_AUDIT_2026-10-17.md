# 代码审计报告 (2026-10-17)

> 审计范围：结构/模块/依赖、代码质量、重复代码、性能、错误处理、依赖管理。
> 方法：实测（wc/grep/AST 解析）+ code-explorer 探索，所有结论附文件:行号证据。
> 原则：按用户偏好"保守评估 + 不轻易改动"，建议按 ROI 分级，不推荐过度工程。

## 一、总体结论

| 维度 | 评分 | 一句话 |
|---|:---:|---|
| 模块分层 | 🟢 良好 | tdxfeed→quant→scripts 单向依赖，**零循环** |
| 代码组织 | 🟡 中等 | 23 个顶层目录，v5/ 是平行第二宇宙，scripts 靠平铺 import |
| 重复代码 | 🔴 严重 | plan_e_* 13 个文件；`_norm_code`×4、`is_st`×4、报告拼接×10+ |
| 函数粒度 | 🔴 严重 | 6 个 200+ 行巨型函数（最大 297 行）|
| 错误处理 | 🟡 中等 | 139 处 `except Exception`，其中 **34 处静默吞错** |
| 性能 | 🟡 中等 | predict 已并行 ✅，但每股票 2 次全量 read_parquet |
| 依赖管理 | 🔴 严重 | 无 lock 文件；**8 个声明依赖 0 文件 import** |
| 测试覆盖 | 🔴 严重 | ~10 个测试 vs 55k 行代码 |

---

## 二、代码结构与依赖关系

### 2.1 分层（唯一亮点）

```
tdxfeed/（数据源，零外部依赖）
   ↑
quant/（因子/执行，仅 quant/datafeed.py:15-17 引 tdxfeed）
   ↑
scripts/（应用层，20 处经 scripts/_common.py:17-22 桥接）
```

**无循环依赖** ✅。但落地粗糙：

- **v5/ 完全独立**：零 import 主仓、**全目录 0 个 `__init__.py`**、靠 `sys.path.insert` 硬编码（全仓 **27 处**，如 `v5/README.md:219`）
- **scripts 平铺互引**：`scripts/compare_plan_e_signals.py:19` 等 4 文件直接 `import plan_e_backtest`；`plan_e_sim_live.py:216` 还导入私有函数 `from predict import _enrich_industry_features`
- **根目录散落 3 个游离脚本**：`min_sync.py`、`min_sync2.py`、`train_monitor.py`

### 2.2 双轨现象

| 双轨 | A | B |
|---|---|---|
| MCP | `mcp/servers/`（3 个）| `v5/mcp_servers/`（多个）|
| systemd | 根 `systemd/` 12 个单元 | `v5/systemd/` 19 个单元 |
| 特征计算 | `tdxfeed/factors_polars.py` + `factors_candidates.py` | `scripts/feature_engineering.py`(395行) + `tdxfeed/precompute/indicators.py` |

### 2.3 遗留/死代码（git mtime + 引用判断）

| 目录/文件 | 状态 | 证据 |
|---|---|---|
| `analysis/snow/` | 一次性产物（2026-09-28）| 无代码引用 |
| `transform/`（v4 管线）| 遗留 | Makefile 零 target 引用 |
| `tools/desktop_auto_clicker.py` | **与量化无关** | — |
| `agents/` + `skills/`（5 yaml）| 半弃用实验层 | Makefile 零引用，被 `.codebuddy/agents/*.md` 取代 |
| `mcp/servers/` | 半维护 | Makefile 零 target |
| `systemd/plan_e_v3_premarket.*` | 疑似遗留 | 未列入 timer-enable 列表 |
| `scripts/_v111_analysis.py` 等 | 历史快照 | 文件名即版本号 |

---

## 三、代码质量

### 3.1 巨型函数（>80 行，AST 实测）

| 函数 | 行数 | 位置 |
|---|---:|---|
| `run_mf_backtest()` | **297** | `quant/multifactor/engine.py:254` |
| `main()` | **254** | `scripts/predict.py:531` |
| `main()` | **254** | `scripts/plan_e_backtest.py:1424` |
| `simulate_one_month()` | 234 | `scripts/plan_e_journal.py:256` |
| `emit_report()` | 196 | `scripts/monitor_plan_e_live.py:466` |
| `summarize()` / `backtest_one()` | 171/144 | `plan_e_backtest.py:504/203` |

### 3.2 Docstring 覆盖率

| 文件 | 覆盖率 |
|---|---:|
| `scripts/predict.py` | 72%（18 函数 13 个）|
| `scripts/plan_e_backtest.py` | **49%**（51 函数 25 个）|
| `quant/multifactor/engine.py` | **20%**（15 函数 3 个）|

### 3.3 重复代码（最大重构目标）

| 重复逻辑 | 实现份数 | 位置 |
|---|:---:|---|
| `_norm_code` | **4** | compare_plan_e_signals.py:24 / oracle_plan_e_ceiling.py:25 / plan_e_confidence.py:29 / llm_signal_plan_e.py:260 |
| 北交所 920 判断 | **3** | monitor_plan_e_live.py:118 / compare_plan_e_signals.py:32 / oracle_plan_e_ceiling.py:56 |
| `is_st` 判定 | **4 种** | find_double_up_c.py:28 / _v2.py:44 / find_double_30d.py:82 / predict.py:144 |
| Markdown 报告拼接 | **10+ 文件** | plan_e_backtest.py:1543-1641（单文件 7 个 header 分支）等，全是 `L.append(f"# ...")` 同构 |
| 分钟线同步 | **2 份** | `min_sync.py:25-27` vs `min_sync2.py:23-25`（`get_core_symbols()` 逐字重复）|
| 版本后缀文件 | 12 个 | `find_double_up_c_v2.py`、`quantize_awq_fixed.py`、`v6_seq_only.py` vs `v6_seq_fixed.py`（219 行仅差 4 字符）等 |

---

## 四、性能瓶颈

### 4.1 predict.py 全市场推理（实测 12 min / 6882 只）

```python
# scripts/predict.py:316,330 —— 每只股票：
raw = pd.read_parquet(fp)        # 全量日线 parquet（~1000 行）
adj_df = pd.read_parquet(adj_fp) # 全量复权 parquet（再 ~1000 行）
# 但只取最近 ~20+25 日做特征
```

- **6882 只 × 2 次全量 read_parquet ≈ 13,764 次文件 IO**，每个只读尾部 5% 数据
- ProcessPoolExecutor(8) ✅ 已并行，瓶颈在 IO 而非 CPU
- **优化路径**：pyarrow `read_row_group`/columns 裁剪（只读 date/close/volume 等所需列 + 尾部 rows），或预先合并单一 wide parquet —— 预期 12 min → **3-5 min**
- **验证保障**：`tests/test_stage1_factors_parity.py` 已存在，可防特征漂移

### 4.2 其他

- `load_all_stocks()` 每进程重复加载 LightGBM 模型（小，可忽略）
- 无明显内存泄漏点；`plan_e_backtest.py` 全量 pandas in-memory 可接受（数据量级小）

---

## 五、错误处理

| 问题 | 数量 | 危害 |
|---|---:|---|
| `except Exception:` | **139 处** | 宽泛捕获掩盖真实错误 |
| 其中 `except → pass`（静默吞错）| **34 处** | **故障不可追溯**——与 v9 watchdog 盲训 9h 教训同源 |

边界条件抽查：`predict.py` 有 `--no-freshness-check` 旁路（好），仙股数据陈旧未过滤（Top 3/4 是 09-10/08-31 数据——已在上轮打板报告人工剔除，**代码层未防**）。

---

## 六、依赖管理

### 6.1 版本声明 vs 实装（py3.14 venv）

| 声明（requirements.txt）| 实装 | 风险 |
|---|---|---|
| `polars>=1.0.0` | **2.0.0** | 大版本跳跃，API 不保证兼容 |
| `numpy>=1.26.0` | 2.5.3 | 2.x 大版本 |
| `transformers>=4.46.0` | 4.57.6 | 已经历 v9 yaml 键踩坑 |
| `llamafactory>=0.9.0` | 0.9.5 | yaml 不支持 `test_dataset/split/torch_compile` 键（v9 配置注释实证）|

### 6.2 无用依赖（声明了但 **0 文件 import**）

`plotly`、`akquant`、`schedule`、`seaborn`、`gguf`、`ollama`、`deepspeed`、`trl` —— **8 个**。
其中 `akquant` 标注"阶段1因子工程"但已被 `tdxfeed/factors_polars.py` 替代。

### 6.3 无 lock 文件

仅 `requirements.txt`（全 `>=` 宽约束）。**py3.14 ABI 教训**（torchvision 错配）与 v9 键不兼容均源于此。

---

## 七、优化建议（按优先级）

### P0 —— 高收益/低风险（纯加法，可立即做）

> ✅ **P0 已于 2026-10-17 执行完毕**（commit `777ca8c`，35 文件 +344/-78）：
> - P0-1：删 6 个 0-import 依赖（**二次验证修正**：akquant/gguf 有引用保留，初版正则漏了 `import pkg.sub`）+ 关键包上限 pin + `requirements.lock`（257 包）+ Makefile 漂移源移除 + 新增 `make sync-lock`
> - P0-2：33/37 处吞错改留痕（排除 v5/tests 4 处故意异常路径）；意外发现 `tdx_verification.py:55` 原本就是 py2 语法错（死文件实锤）顺手修复；24 文件编译全过 + canary 冒烟 err=0
> - P0-3：git mv 归档 transform/、tools/、根目录 3 游离脚本、_v111_analysis.py → `_archive/`；顶层目录 23→18

| # | 建议 | 预期收益 | 潜在风险 |
|---|---|---|---|
| **P0-1** | **依赖清理 + 锁定**：requirements.txt 删 8 个 0-import 包；`pip freeze > requirements.lock`；关键包改精确 pin（polars==2.0.0 等）| 防环境漂移重演（py3.14 ABI 教训）；安装提速；认知负担↓ | 极低（删的都是没用的）|
| **P0-2** | **静默吞错加日志**：34 处 `except→pass` 改为 `except ... : log.warning(...)`（不改变控制流）| 故障可追溯（v9 盲训同源问题）| 低（仅加日志）|
| **P0-3** | **归档死代码**：`transform/`、`tools/desktop_auto_clicker.py`、根目录 3 个游离脚本、`scripts/_v111_analysis.py` → `_archive/` 或直接删（git 有历史）| 顶层目录 23→~18；新会话认知负担大幅↓ | 极低（git 可恢复）|

### P1 —— 中收益/中风险（需小步验证）

| # | 建议 | 预期收益 | 潜在风险 |
|---|---|---|---|
| **P1-1** | **抽公共工具库**：`scripts/_common.py` 统一 `_norm_code`/`is_st`/北交所判断（4+4+3 处 → 1 处），13 个 plan_e 文件逐步切换 | 单一真相源；下次改规则只改一处 | 中：触碰 13 个文件，需逐文件跑通；建议一次只切 2-3 个文件 |
| **P1-2** | **predict.py 读取裁剪**：read_parquet 加 `columns=` 裁剪 + 尾部 rows 读取；先跑 `test_stage1_factors_parity.py` 验证 | 全市场 12 min → 3-5 min（每日省 7-9 min，周一开盘前窗口更宽）| 中：特征 parity 必须用已有测试守住；分两步（先裁 columns 再裁 rows）|
| **P1-3** | **data freshness 代码层防御**：predict.py 加"数据陈旧 N 天即剔除并告警"（仙股 Top 3/4 问题根治）| 杜绝陈旧数据进 Top 20 | 低 |
| **P1-4** | **巨型函数拆分**（只拆热点）：`run_mf_backtest`(297) / 两个 `main`(254) 拆为 5-6 个 ≤60 行函数 | 可读、可单测 | 中：纯重构需测试兜底（先补该函数的 golden 输出快照）|

### P2 —— 长期（v9/实盘稳定后，单独立项）

| # | 建议 | 收益 | 风险 |
|---|---|---|---|
| P2-1 | plan_e_* 13 文件整合为核心库 + 薄 CLI；统一 markdown 报告 builder | 消灭最大重复源 | 高（大重构，必须分 3-4 批）|
| P2-2 | v5/ 去留决策：独立 repo 或正式化为带 `__init__.py` 的包，消灭 27 处 sys.path 硬编码 | 双轨合一 | 高（v5 有 19 个 systemd 依赖）|
| P2-3 | 测试覆盖：10 → 50+（优先 predict 特征、plan_e 汇总、engine 回测）| 重构安全网 | 低（纯加法）|

### 🚫 不建议（过度工程，与"保守评估"偏好冲突）

- 全仓 type-hints 化 / 引入 monorepo 工具 / lint 严苛化全家桶
- 微服务化 / 配置中心化大迁移（etc/ 统一虽好但收益不抵风险）
- 在 v9 修复期做 P1-1/P1-4 以上的大动作

---

## 八、与近期事件的关联

| 审计发现 | 关联事件 |
|---|---|
| 34 处静默吞错 | v9 watchdog 盲训 9h（sanity 全 RC_ERROR 未被发现）|
| 无 lock 文件 | py3.14 ABI 错配、llamafactory 0.9.5 键踩坑 |
| predict 未防陈旧数据 | 本周末 Top 20 出现 09-10/08-31 仙股（人工剔除）|
| 巨型函数 + 低测试覆盖 | 每次修复都靠"重跑全流程"验证，无法快速回归 |

**结论**：这不是"烂代码"，而是**快速迭代单用户项目的典型技术债**——分层是对的、并行是对的，但 13 个 plan_e 文件 + 无锁依赖 + 静默吞错构成三大雷区。**P0 三项零风险可立即做；P1 在 v9 修复验证后做；P2 留给稳定期。**

## 附：证据索引

- 结构/依赖：`scripts/_common.py:17-22`、`quant/datafeed.py:15-17`、v5 目录扫描
- 重复：见 3.3 表全部 file:line
- 巨型函数：AST 解析实测（predict/plan_e/journal/monitor/engine/factor_significance 六文件）
- 异常：grep 实测 139/34 处
- 依赖：`pip list` vs requirements.txt 逐项比对
- 性能：`scripts/predict.py:316,330,385-393`
