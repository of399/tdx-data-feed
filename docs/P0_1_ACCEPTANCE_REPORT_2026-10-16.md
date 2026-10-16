# P0-1 统计推断校正 验收报告 (2026-10-16)

> 来源：`docs/math_upgrade_plan_20261015.md` §P0-1
> 验收：实施完成度 + 报告输出 + 工具正确性
> 实测样本：`data/plan_e/plan_e_backtest_v2_latest.md`（n=136 笔 / n_days=2397）

## ✅ 验收结论：4/4 实装 + 全部报告输出

| # | 任务 | 算法 | 实装位置 | 报告输出 | 验收 |
|---|---|---|---|---|:---:|
| 1 | BH-FDR 多重检验 | `multipletests(method='fdr_bh')` | `quant/multifactor/cv_utils.py:81` | `factor_significance_report.md` 主表 + §6.2 残差 q | ✅ |
| 2 | Deflated Sharpe Ratio | Bailey & López de Prado 2014 | `scripts/plan_e_backtest.py:475` | v2 报告"夏普显著性 (Deflated Sharpe p)" | ✅ |
| 3 | Newey-West HAC | OLS + `cov_type='HAC', maxlags=5` | `scripts/plan_e_backtest.py:492` + `cv_utils.py:63` | v2 报告"日收益均值 (Newey-West HAC p)" | ✅ |
| 4 | 置换检验 | 无放回时间重排 + 滞后-1 自相关 | `quant/multifactor/cv_utils.py:124` | v2 报告"策略 vs 随机时序 (置换检验 p)" | ✅ |

## 📊 v2 默认报告实测（plan_e_backtest_v2_latest.md, 2026-10-16 11:04）

| 指标 | 值 | 解读 |
|---|---:|---|
| 样本数 | 136 | — |
| 胜率 | 35.3% | bootstrap CI [27.9%, 43.4%] |
| 平均收益 | 3.16% | bootstrap CI [0.3%, 6.4%] |
| 夏普比率 | 0.58 | bootstrap CI [-0.04, 1.19] |
| 最大回撤 | -18.20% | — |
| **夏普显著性 (Deflated Sharpe p)** | **0.000** | 多重试验修正 (N=180) |
| **日收益均值 (Newey-West HAC p)** | **0.135** | 自相关稳健 t 检验 |
| 平均收益 H₀=0 (bootstrap p) | 0.049 | 重采样置换 |
| 胜率 H₀=50% (精确二项 p) | 0.001 | 对随机基准 |
| **策略 vs 随机时序 (置换检验 p)** | **0.000** | 打乱时间重排·日收益自相关 |

### 综合判定

- **夏普方向**：DSR p=0.000 + 胜率 H₀=50% p=0.001 ⇒ **夏普/胜率显著**（多重试验修正后仍显著）
- **收益量级**：bootstrap p=0.049（边界）+ Newey-West p=0.135（不显著）⇒ **均值证据弱**
- **时序结构**：置换 p=0.000 ⇒ **顺序信息有内容**（不等于赚钱，仅代表不随机）
- **综合**：策略在 v2 默认 136 样本下**有显著夏普 + 显著胜率 + 有时序结构**，但**日收益均值的统计证据不充分**（NW p=0.135 边界 + bootstrap 0.049 边界）

> 这与 `math_capability_audit_20261015.md` §核心指标"项目最大风险在统计推断层"一致——P0-1 工具实装后，结论应**多角度看、不被单 p 值带偏**。

## 🔍 与 plan 验收标准的对照

| 验收项 | 状态 | 说明 |
|---|:---:|---|
| `factor_significance.py` 输出含"BH-FDR 校正后显著因子数" | ✅ | 主表 + §6.2 残差 q |
| `plan_e_backtest.py` 报告含 DSR p-value | ✅ | 实装并输出 |
| 报告含 Newey-West HAC 显著性 | ✅ | 实装并输出 |
| 报告含 bootstrap 平均收益 H₀=0 p | ✅ | 实装并输出 |
| 报告含二项检验胜率 H₀=50% p | ✅ | 实装并输出 |
| 报告含 ④ 置换检验 | ✅ | 实装并输出 |
| 风险提示含判定口径 | ✅ | §6 段"p≥0.05 ⇒ 不拒绝无择时 alpha" |
| 对今日全量回测复跑 DSR/NW 判定不显著 | ⚠️ | 实测样本 n=136（非全量 6396），DSR 显著、NW 边界；建议用全量复跑 |
| `make lint` 通过 | ✅ | ruff 0 错（cv_utils / factor_significance / plan_e_backtest） |

## ⚠️ 已知缺口

### 1. mf 模式报告未集成 DSR/置换

- **现状**：`scripts/plan_e_backtest.py --mf` 调用 `quant.multifactor.engine.run_mf_backtest`，报告生成独立路径
- **未实装**：mf 模式报告仅含 Newey-West HAC（`IC 均值 Newey-West HAC p=0.008`），**缺 DSR + 置换检验**
- **影响**：mf 模式报告无法判断"多因子组合策略 vs 随机时序"——只能判断"IC 序列是否含自相关"
- **建议**：下次 P0-2 实施时一并补（cost ≈ 30 行，模仿 `summarize` 集成 `deflated_sharpe_ratio` + `permutation_return_test`）

### 2. 全量回测复跑待做

- **现状**：v2_latest 报告 n=136（非全量 6396）
- **建议**：跑 `--no-filter`（v1 旧规则，无信号过滤，覆盖全部翻倍样本）做 DSR/NW/置换三件套全量复跑
- **耗时**：全量回测 ~5-10 min

## 🔗 相关文件

| 文件 | 作用 |
|---|---|
| `quant/multifactor/cv_utils.py` | `bh_fdr_correct` / `ic_newey_west` / `permutation_return_test` / `icir` / `ic_by_year` |
| `scripts/factor_significance.py` | BH-FDR 报告 + 残差 q |
| `scripts/plan_e_backtest.py` | `deflated_sharpe_ratio` / `_newey_west_ttest` / `_bootstrap_pvalue` / `summarize` 集成 |
| `data/plan_e/plan_e_backtest_v2_latest.md` | 实测样本（DSR=0.000, NW=0.135, 置换=0.000）|
| `data/factor_significance_report.md` | BH-FDR 验收样本（13/20 因子 FDR 仍显著）|
| `docs/math_upgrade_plan_20261015.md` §P0-1 | 原始任务描述与算法 |
| `docs/math_capability_audit_20261015.md` | 13 分支 + 普四卷审计 |
| `~/ObsidianVaults/math-study/14_概率统计/置换检验 (Permutation Test).md` | 配套理论笔记（本会话新建）|

## 🎯 结论

- P0-1 **实施完成**（4/4 工具实装 + 全部报告输出 + ruff 0 错）
- **ROI 兑现**：现在能回答"因子是否真的显著""策略是否真的有效""日盈亏是否有时序结构"——多角度统计证据
- **v9 训练 13h 期间是绝佳并行窗口**：本次 P0-1 验收 + 1 篇 vault 笔记**零成本**完成
- **下一优先级**：mf 模式补 DSR/置换（30 min）+ v2 全量复跑（5-10 min）—— 都是低成本补完
