# 代码审计第二轮 · 改造后复核 (2026-10-18)

> 背景：2026-10-17 完成全量审计（`CODE_AUDIT_2026-10-17.md`）→ 方案（`CODE_AUDIT_REMEDIATION_PLAN_2026-10-17.md`）→ P0 三项（`777ca8c`）→ P1 七项（`c86cc26`）。
> 本轮为**改造后增量复核**：确认修复生效 + 找出残留/新增问题，不重复全量审计。

## 一、改造前后指标对比（实测）

| 指标 | 改造前 (10-17) | 改造后 (10-18) | 状态 |
|---|---:|---:|---|
| 静默吞错（except→pass）| 37 处 | **4 处**（v5/tests 故意异常路径）| ✅ 预期内 |
| 依赖 0-import 声明 | 8 个 | **0 个**（6 删 + 2 证实有引用保留）| ✅ |
| 锁文件 | 无 | `requirements.lock`（257 包）| ✅ 新增 |
| 死代码顶层目录 | transform/ tools/ mcp/ + 根目录散落 | 全部入 `_archive/`（7 项）| ✅ |
| predict.main() 行数 | 254 行 | **已拆 4 段**，退出巨型函数榜 | ✅ golden 验证 |
| `_norm_code` 重复定义 | 4 处 | plan_e 系 4 处已统一，**但新发现 2 处漏网**（见下）| 🟡 收尾中 |
| engine.py docstring | 20% | **80%** | ✅ |
| tests | 未跑 | **82 个全过**（改造后回归绿）| ✅ |
| freshness 防御 | 无 | 陈旧 >10 天剔除（688496 实测拦截）| ✅ |
| 巨型函数（>80 行，全量口径）| 仅测热点 12 个 | **全量实测 54 个**（新口径，见三）| 🟡 P2 |

## 二、本轮新发现（3 项）

### 新 1：`norm_code` 尚有 2 处漏网 + 1 处嵌套 `is_bj`（P1' 收尾，30 min）

昨日只切换了 4 个 plan_e 文件，全仓扫描仍有：

| 位置 | 类型 |
|---|---|
| `scripts/factor_engine.py:48` `def norm_code(code)` | 模块级重复（带类型注解版）|
| `quant/multifactor/factors.py:26` `def norm_code(code)` | 模块级重复（带类型注解版）|
| `scripts/plan_e_backtest.py:1089` `def is_bj(code)` | **函数内嵌套定义**（P2 整合时一并处理）|

**建议**：两处模块级版与 `_common.norm_code` 行为对比后切换（它们带类型注解，需确认逻辑等价——若含额外分支则保留并注释差异原因）。

### 新 2：`downloads/` 目录实际存在（昨日 explorer 误报"不存在"）

需查明内容与 mtime，若为安装包/历史下载物则归档（10 min）。

### 新 3：巨型函数全量口径 54 个（昨日只测 6 个热点文件=12 个）

完整榜单新增（均为**历史脚本**，不在日常链路上）：
- `scripts/v3_simulate.py:simulate()` 279 行
- `scripts/analyze_pre90.py:_write_report()` 190 行
- `scripts/find_double_up_c_v2.py:main()` 185 行
- `scripts/stage1_gate.py` / `stage1_retrain.py` / `download_valuation.py` 等

**判断**：日常链路（predict/plan_e_backtest 主流程）已在昨日处理或属 P2；这 54 个中**绝大多数是一次性历史脚本**——**不建议逐个拆分**（过度工程），仅在实际需要修改某脚本时顺带重构（" Opportunistic Refactoring"原则）。

## 三、复核确认（无需再动的部分）

| 项 | 结论 |
|---|---|
| report_writer.py 0 引用 | **预期内**——止血库等新报告采用，不回改旧的 |
| sys.path 硬编码 34 处 | v5 占多数——**P2 v5 去留决策时一并处理** |
| 顶层目录 22 个 | 比"预期 17"多的 4 个 = `_archive`(新增) + `analysis/` + `agents/` + `skills/` + `downloads/`——后三者是**半弃用实验层**（P0-3 有意保留，等 v5 决策一并处理）|
| tests 82 全过 | ✅ P0+P1 全部改动无回归 |

## 四、更新后的优化建议（优先级）

### P1'（新一轮收尾，合计 ~40 min，可做）

| # | 建议 | 收益 | 风险 |
|---|---|---|---|
| P1'-1 | 2 处 `norm_code` 漏网切换（先对比行为）| 单一真相源完成度 4/6→6/6 | 低（带注解版需逐行 diff 确认等价）|
| P1'-2 | `downloads/` 查明 + 归档 | 顶层 22→21 | 极低 |

### P2（不变，稳定期立项）

1. plan_e_* 13 文件整合（含 plan_e_backtest.main 254 行 + 嵌套 is_bj）
2. v5 去留决策（sys.path 34 处 + 双 systemd + agents/skills/analysis 一并）
3. engine.run_mf_backtest 306 行拆分（先补 mf 回测 golden）
4. is_st 4 种语义统一（需设计讨论：int 特征版 vs bool）

### 🚫 不做（过度工程）

- 54 个历史脚本巨型函数逐个拆分（Opportunistic 原则：改到才拆）
- 全仓 docstring 100%、type hints 全仓化

## 五、审计方法论沉淀（本轮验证的教训）

1. **审计判断会被实测推翻**——本轮 2 次：akquant/gguf 实际有引用、IO 实际非瓶颈（1.5ms/股）→ **任何"优化建议"先实测后执行**
2. **扫描口径要全量**——巨型函数昨日只测 6 热点文件（12 个），全量是 54 个；norm_code 昨日只扫 plan_e 系（4 处），全仓是 6 处 → **"重复代码"类结论必须全仓 grep**
3. **工具输出需交叉验证**——explorer 误报 downloads/ 不存在（可能被 .gitignore 干扰）→ 归档动作前必须 `ls` 实测
4. **改造后必须回归**——tests 82 全过是 P0+P1 零回归的唯一证据

## 六、结论

> 审计→方案→P0→P1→复核**全链闭环**：37 处吞错→4、8 个无用依赖→0、predict.main 已拆、tests 82 全绿。**新发现仅 3 项小尾巴**（P1' 40 min 可清）。代码库当前处于**健康可演进**状态，剩余债务集中在 P2（plan_e 家族 + v5 双轨），建议等 v9 修复/实盘稳定后立项。
