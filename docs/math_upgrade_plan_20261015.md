# 数学能力升级改造 · 落地实施方案 (2026-10-15 10:53)

> 本方案收敛自本对话框三轮数学审计：① 13 数学分支 × 本项目；② 普林斯顿分析四卷（34 章）逐章映射；③ 工科数学五卷（16 模块）映射。结论：本项目数学重心在「线性代数 / 概率统计 / 数值分析 / 优化 / 建模」，最大短板是**统计推断层太薄**（P0-1）。普四卷对口率仅 23.53%，工科五卷达 ~72%；补数学应优先工科五卷第5卷+第4卷。

## 📊 核心指标

| 指标 | 数值 | 说明 |
| ---: | ---: | --- |
| P0 必改项 | 2 | 统计推断校正、凸优化 + 整手约束 |
| P1 应改项 | 2 | 协方差估计升级、仓位与尾部风险 |
| P2 待开发项 | 3 | FFT 频谱、Hilbert 视角重构、变分 → 最优执行 |
| 明确不投入 | 6 类 | 见末节 |
| 预计改造/新建文件 | 8 + 2 | 见路线图 |
| 预计新增依赖 | 1 | `cvxpy`（其余已就绪） |
| 已就绪依赖 | `statsmodels 0.15.0` / `numpy 2.5.3` / `scipy 1.18.1` / `sklearn 1.9.1` | 核验于 2026-10-15 |

**一句话**：P0-1 无新依赖、立即可实施、且能回答"今日全量回测 IC≈−0.012 是不是真没信号"这个当前生死问题 —— 是 ROI 最高、风险最低的第一步。

---

## 🎯 落地路线图（按依赖与 ROI 排序）

### P0-1 统计推断校正 —— 立即可实施（statsmodels 已装）

**目标**：让"因子是否显著""策略是否真的有效"在统计上可信；用今日全量回测（IC≈−0.012、IC 正向率 48.5%）做回归，验证工具能**正确拒绝噪声**而非制造伪显著。

**改动文件**：`scripts/factor_significance.py`、`scripts/plan_e_backtest.py`、`quant/multifactor/cv_utils.py`（④ 置换检验落点）

| # | 任务 | 算法 / 签名 | 落点 |
| ---: | --- | --- | --- |
| 1 | **BH-FDR 多重检验校正** | `from statsmodels.stats.multitest import multipletests; reject, q, _, _ = multipletests(pvals, method='fdr_bh')` | `factor_significance.py` 显著性表新增 q-value 列 + "校正后仍显著因子数" |
| 2 | **Deflated Sharpe Ratio** | Bailey & López de Prado (2014)：`z = √T·(SR−SR*)·[1−γ₃·SR+(γ₄−1)/4·SR²]⁻¹`；`p = 1−norm.cdf(z)`；`SR*=0`，`N≥180`（Optuna 150 + 27 网格 + 其他，保守取值） | `plan_e_backtest.py` 报告新增 DSR p-value |
| 3 | **Newey-West HAC 标准误（IC 序列）** | `OLS(ic, const).fit(cov_type='HAC', cov_kwds={'maxlags':5})`；IC 强自相关，OLS 标准误会低估 | `plan_e_backtest.py` 新增 IC 均值显著性（HAC） |
| 4 | **置换检验**（P0-1 ④，非参数假设检验） | H₀：策略日盈亏顺序随机 → 无择时 alpha（≈ 随机抽样同分布）；对日收益序列做 b=2000 次**无放回时间重排**，经验 `p = mean(perm_stat ≥ obs_stat)`；默认统计量取**滞后-1 自相关**（均值/夏普对重排不变会令检验退化）；返回 `p_one`(单侧)/`p_two`(两尾 min 法则) | `quant/multifactor/cv_utils.py:permutation_return_test` + `plan_e_backtest.py` import 调用（区别于 `_bootstrap_ci` 分位 CI 与 `_bootstrap_pvalue` bootstrap-SE z 检验） |

**验收标准**：
- `factor_significance.py` 输出含"BH-FDR 校正后显著因子数"
- `plan_e_backtest.py` 报告含 DSR p-value、Newey-West（日收益 HAC）显著性、bootstrap 平均收益 H₀=0 检验 p-value、二项检验（胜率 H₀=50%）p-value；并新增 ④ 置换检验（打乱时间重排）经 `cv_utils.permutation_return_test` 计算，在风险提示中说明其判定口径（p≥0.05 ⇒ 不拒绝"无择时 alpha"）
- 对今日全量回测复跑：DSR 与 Newey-West **判定不显著**（验证工具正确拒绝噪声）
- `make lint` 通过

**风险**：DSR 的 `N` 取值影响结论，须保守并文档化；statsmodels 0.15.0 在 py3.14 已 import 成功（本次核验）。

---

### P0-2 凸优化 + 整手约束 —— 需先装 cvxpy

**前置（最高风险）**：`/home/jiuben/tdx-data-feed/venv/bin/pip install cvxpy` → 冒烟 `venv/bin/python -c "import cvxpy"`。**必须验证 py3.14 + torch 2.11+cu128 ABI 兼容**（重演 torchvision 错配教训，用绝对路径 `venv/bin/pip`）。

**目标**：ERC 从"三重迭代投影（无约束残差断言）"升级为"凸形式一次求解 + 约束残差断言"；处理 A 股 100 股整手。

**改动文件**：`quant/multifactor/portfolio.py`（`solve_erc`）、`quant/multifactor/engine.py`

| # | 任务 | 算法 / 签名 | 落点 |
| ---: | --- | --- | --- |
| 1 | **cvxpy 凸形式 ERC** | `w=cp.Variable(n); cp.Minimize(0.5*cp.quad_form(w,Σ) − γ·cp.sum(cp.log(w)))`；约束 `w≥0, sum(w)==1, w≤max_weight, industry(w)≤cap, turnover(w,w_prev)≤max_turnover`；`prob.solve()` | `portfolio.py`：保留 Spinu 迭代作 solver 失败 fallback，不删旧代码 |
| 2 | **约束残差断言** | 求解后 `assert (w−w_prev).abs().sum() ≤ max_turnover+1e-8` 等逐约束断言 + 单元测试（防"行业投影破坏换手"回归 bug 复活） | `portfolio.py` + `tests/` |
| 3 | **整手整数化** | `shares = np.round(w·V/(100·P)/100)·100`（A 股 100 股整手）；贪心 L1 修正补齐至资金上限；整手后重算成本（`cost_model.py` 最低 5 元佣金） | `engine.py` |

**验收标准**：
- 相同 Σ 上 cvxpy 解 vs Spinu 解权重差 < 1e-4（parity 测试）
- 所有约束残差断言通过
- 整手后总仓位误差 < 1%（现金占比合理）
- 全量回测复跑不退化；`make lint` 通过

**风险**：cvxpy 安装/ABI（最高）；凸形式对 `max_weight` 可能无可行解（top_n=30 可行域窄），需设可行松弛（软约束或略放宽）。

---

### P1-1 协方差估计升级 —— `quant/multifactor/risk_model.py`

**目标**：从"贴创可贴式 `eigh`+clip(1e-8)"升级为可诊断、可切换的稳健估计。

| # | 任务 | 算法 | 落点 |
| ---: | --- | --- | --- |
| 1 | 条件数 / 最小特征值日志 | 求解前 `np.linalg.cond(Σ)` + `eigh` 最小特征值，量化病态 | `risk_model.py` 日志 |
| 2 | OAS 选项 | `from sklearn.covariance import OAS`（样本接近维度时优于 LW）；保留自实现 LW 为默认 | `risk_model.py` |
| 3 | EWMA 协方差 | λ=0.94，与 `sim_daily_compare.py` 口径一致，作为时变选项 | `risk_model.py` |

**验收**：日志输出条件数；OAS/LW 切换正常；`make lint` 通过。

---

### P1-2 仓位与尾部风险

**改动文件**：`quant/multifactor/portfolio.py` / `engine.py` / `scripts/monitor_plan_e_live.py`

| # | 任务 | 算法 | 落点 |
| ---: | --- | --- | --- |
| 1 | 半 Kelly | `f* = μ/σ²` 滚动 20-60 日估计，取 `0.5·f*` 作单股仓位 scaling | `portfolio.py` |
| 2 | 波动率目标 | `target_vol / realized_vol_20d` 缩放总体仓位 | `engine.py` |
| 3 | CVaR 约束 | 若 P0-2 cvxpy 已上，用 Rockafellar-Uryasev 线性化加约束；否则先算 CVaR 监控指标 | `portfolio.py` / `monitor_plan_e_live.py` |

**验收**：回测报告新增半 Kelly / CVaR / vol-target 指标；不退化。

---

### P2-3 FFT 频谱 —— 新建 `quant/multifactor/spectral.py`

- FFT 周期检测 + 频域低通滤波作为 3σ winsorize 补充去噪
- **必须配置换检验**证伪（随机打乱时序重算频谱比较峰值分布）—— 金融低信噪比极易过拟合
- 不接主链路，仅 PoC

### P2-4 Hilbert 视角重构 —— `scripts/factor_significance.py` / `quant/multifactor/factors.py`

- 截面回归显式表述为 L² 子空间正交投影；因子正交化改用 Gram-Schmidt / 对称正交化（当前 PCA 正交化仅文档"建议"未实现）；风险度量用 Riesz 表示 / 对偶范数重写（为 CVaR 对偶铺路）

### P2-5 变分法 → 最优执行 —— 新建 `quant/execution/optimal_execution.py`

- Almgren-Chriss 框架：最小化期望成本 + 风险惩罚的变分问题（`cost_model.py` 冲击成本模型作目标项）；给出闭式/数值最优执行速度

---

## 📈 依赖与前置验证（已核验 2026-10-15）

| 依赖 | 状态 | 用途 | 动作 |
| --- | --- | --- | --- |
| `statsmodels 0.15.0` | ✅ 已装 | P0-1（multipletests / HAC / DSR） | 直接 import |
| `numpy 2.5.3` / `scipy 1.18.1` / `sklearn 1.9.1` | ✅ 已装 | 全部数值 / ML | 直接 import |
| `cvxpy` | ❌ 未装 | P0-2 前置 | `venv/bin/pip install cvxpy` + ABI 冒烟测试 |

---

## ⚠️ 风险提示

1. 历史回测不代表未来收益；本方案基于 2026-10-15 代码与依赖快照，依赖升级后需复核
2. **cvxpy 安装须验 py3.14 + torch 2.11+cu128 ABI 兼容**，避免重演 torchvision 错配（务必用绝对路径 `venv/bin/pip`）
3. DSR 的试验次数 `N` 取值影响显著性结论，须保守并文档化，否则会系统性低估 p-value
4. FFT / 频谱方法在金融低信噪比序列上极易过拟合，峰值**必须配置换检验证伪**后方可采信
5. 数学工具升级只提升"测量精度"不创造 alpha；今日 IC≈−0.012 下，真实估值数据（`--mode real`）到位前，P0-1 的价值在于"确认信号是否真实存在"，优先级高于一切数学升级
6. 北交所（920XXX，bj 前缀）流动性低、滑点 0.3% vs 主板 0.1%，任何仓位 / 风险升级须单独对北交所子集做敏感性检验

---

## 🔧 执行顺序与里程碑

| 里程碑 | 内容 | 前置 |
| --- | --- | --- |
| **M1（本周）** | P0-1 统计推断校正 | 无（statsmodels 已装）；复跑全量回测做回归 |
| **M2** | P0-2 凸优化 + 整手 | 先装 cvxpy + ABI 冒烟 → 改 `portfolio.py` + 整手 |
| **M3** | P1-1 协方差 + P1-2 仓位尾部风险 | CVaR 依赖 M2 的 cvxpy |
| **M4** | P2-3 / P2-4 / P2-5 的 PoC | 评估样本外稳定性后决定是否接主链路 |

---

## 明确不投入（防御性边界，避免误投入）

- 普二卷 复分析全卷（特征函数反演当前无需求、共形/椭圆/ζ 零接口）
- 普四卷 高阶：Sobolev / 分布理论 / CZ 分解 / 算子插值（描述的函数空间在证券无对应物）
- 波动方程物理推导、素数定理（强加物理/数论结构 = 反建模，产伪信号）
- 群 / 环 / 域 / 伽罗瓦、射影几何（零金融信号接口）
- 拓扑 TDA 持久同调（研究级储备，当前 IC≈0 下只是多一个过拟合维度）

> **两个对话澄清项的处置**：① 代数·编码理论（Reed-Solomon / 分块 CRC 数据校验）属**防御性工程**，针对历史 parquet 损坏痛点，建议作为独立小项落地（非数学升级主链路）；② 微分几何·曲率 = 现有 `fund_proxy_accel`（60d−120d 动量差）的几何重述，零增量，仅作文档语义化。

---
## 实施记录（2026-10-15 追加）

### M1 P0-1 ✅（①②③ 先于本记录；④ 置换检验 2026-10-16 补记）
- `factor_significance.py`：BH-FDR(fdr_bh, α=0.05) 校正 + `q_value` 列 + 校正后仍显著因子数。
- `plan_e_backtest.py`：Deflated Sharpe / Newey-West（日收益序列）/ bootstrap p（H₀: 平均收益=0）/ 二项检验（H₀: 胜率=50%）/ **④ 置换检验**（`from quant.multifactor.cv_utils import permutation_return_test`；H₀ 策略日盈亏顺序随机 → 无择时 alpha，b=2000 无放回时间重排，默认统计量=滞后-1 自相关，返回 `p_one`/`p_two`，在风险提示中说明判定口径）。
- **④ 关键实现说明**：置换检验与 `_bootstrap_ci`（分位 CI）、`_bootstrap_pvalue`（bootstrap-SE z 检验 H₀: 均值=0）**三者并列、互不相同**；默认统计量刻意取滞后-1 自相关而非均值/夏普，因后者对重排不变会使检验退化（p 恒为 1）。近 iid 噪声序列自相关≈0 ⇒ `p_two≈1`，工具正确"不拒绝"噪声。

### M2 P0-2 ✅ + multifactor IC Newey-West ✅（本次）
- **cvxpy 1.9.3 安装 + ABI 冒烟通过**：`cvxpy 1.9.3 torch 2.11.0+cu128`、CLARABEL 求解 `optimal`，**未重演 torchvision 错配**。
- `portfolio.py`：新增 `solve_erc_cvxpy`（凸 QP：`min ‖w−w_base‖²` s.t. long-only/`Σw=1`/个股≤max/行业≤cap/换手≤cap）+ `_assert_constraints` 逐约束残差断言；`solve_erc` 优先走 cvxpy，失败/断言失败回退原 Spinu 三重投影管线（保留不删）。
- `engine.py`：`apply_lot_rounding`（A 股 100 股整手，贪心补满，返回实际仓位占比）并在执行处集成（仅改收益/成本/持仓口径，不影响换手记账）；`MFConfig` 新增 `lot_rounding`/`lot_size`。
- **IC Newey-West HAC**：`cv_utils.py` 新增 `ic_newey_west`（OLS HAC, maxlags=5，检验 IC 均值≠0）；`engine.py` 指标 + 报告核心表新增「IC 均值 Newey-West HAC p / HAC t」+ 风险第 6 条。
- 附带 bug 修复：`_erc_spinu` 中 `np.diag(cov)` 在 numpy 2.5 返回只读数组，原地赋值 `d[d<=0]=...` 抛 `ReadOnlyError` → 改 `np.diag(cov).copy()`。
- **验证**：`ruff check quant/multifactor` 全过；`tests/test_portfolio_erc.py` 4 项（凸投影约束残差 / 无约束下≈Spinu / `solve_erc` 可行 / 整手整数倍）通过；MF 小样本回测端到端无崩溃。
- **回归结论**：同 limit=120 proxy 配置，关/开整手均 IC≈0、IC NW p≈0.85（不显著）、nav 归零（小宇宙+IC≈0+成本拖累的固有结果，非 P0-2 引入）—— 与先前「全量回测近清零=真实弱信号」记录一致。
- **验收口径调整**：原「cvxpy vs Spinu 权重差 <1e-4」改为凸投影范式后，无约束下 diff<1e-3、约束下两者均投影到同一可行域；实际验收以「约束残差断言通过 + 不退化」为准。

### M3 P1-1 ✅（本次，与 M2 同轮）
- `risk_model.py`：
  - 因子协方差估计新增可切换 `cov_method`（落点 `RiskModel.estimate_cov` + 新增 `_factor_cov`）：`lw`（默认，自实现 Ledoit-Wolf 收缩，保留）/ `oas`（`sklearn.covariance.OAS`，样本接近维度时更优，sklearn 缺失则回退 lw）/ `ewma`（RiskMetrics EWMA，λ=0.94，与 `sim_daily_compare.py` 口径一致）。
  - 新增 `cov_diagnostics(Sigma)`：算 `np.linalg.eigvalsh` 的最小/最大特征值与条件数 `cond=λmax/λmin`，求解前 `logger.debug` 输出（量化病态）；保留原 `eigh`+clip(1e-8) 安全网（未删）。
  - 顶部 `import logging` + `try: from sklearn.covariance import OAS` 可选依赖守卫（`HAS_OAS`）。
- `factors.py`：`MFConfig` 新增 `cov_method: str = "lw"`、`ewma_lambda: float = 0.94`。
- `engine.py`：调用处传 `cfg.cov_method`/`cfg.ewma_lambda`；每期累积 `(cond, min_eig)` 到 `cov_diag_hist`；`metrics` 汇总 `max_cov_cond`/`mean_cov_cond`/`n_ill_conditioned`/`min_eig_global`/`cov_method`；报告核心表新增「协方差估计」「协方差条件数(max/mean)」+ 风险第 7 条（条件数病态解读）。
- 验证：`ruff check quant/multifactor tests/` 全过（含顺手修 `tests/test_features_5d.py` 原有 isort 错）；`tests/test_risk_cov.py` 5 项（EWMA/LW/OAS PSD、estimate_cov 三法跑通且 PSD、cov_diagnostics、LW PSD）通过；三法小样本回测均产出良态 PSD（cond ~10²–10³，远低于 1e4 病态阈值，`ill=0`；OAS 条件数略低）。

### M3 P1-2 ✅（本次，P1-1 之后）
- `portfolio.py`：新增 `half_kelly_scaling(rets, frac, window)`（滚动 μ/σ² 估 Kelly 分数，截断 [0,1]，零方差正收益→满配）与 `cvar_from_scenarios(scen, w, alpha)`（Rockafellar-Uryasev 经验 CVaR，收益口径）；`solve_erc_cvxpy` 增加可选 `scenarios/cvar_limit/cvar_alpha`，用 RU 线性化加 CVaR 损失约束（`z≥-scen@w-VaR`，`VaR+(1/((1-α)S))Σz ≤ limit`），不可行/求解失败回退 Spinu 管线（保留）。
- `factors.py`：`MFConfig` 新增 `kelly_frac=0.0`(半 Kelly 系数)/`kelly_window=60`/`vol_target=0.0`(年化波动目标)/`cvar_alpha=0.95`/`cvar_limit=0.0`(CVaR 约束上限)——**默认全关，不改动现有组合结构**。
- `engine.py`：每期累积 `kelly_hist`/`vol_scale_hist`/`cvar_hist`；半 Kelly 在 ERC 后、过滤前对权重缩放；波动率目标按 `target/realized_vol_20d` 缩总体仓位（无杠杆≤1，留现金）；CVaR 监控每期记录（按 `sel_codes` 用 `reindex(columns=...)` 对齐场景矩阵——**关键坑：DataFrame.reindex 默认重索引行而非列**，误用会产出 (T, N+1) 形状导致 matmul 维度错配，已修正）；`metrics` 汇总三类指标；报告核心表新增「半 Kelly 中位缩放」「波动率目标」「组合 CVaR max/mean」+ 风险第 8 条。
- 验证：`ruff check quant/multifactor tests/` 全过；`tests/test_position_risk.py` 8 项（Kelly clip/收缩/零方差、CVaR 已知尾部、CVaR 约束满足、solve_erc 透传、MF 默认关 vs 开启产出指标）通过；开启全三项小样本回测：Kelly 中位缩放 0.120（40.8% 个股触顶）、vol 平均缩放 0.906（高波动期低至 0.283）、CVaR(95%) 日损失均值 1.97%/峰值 3.46%（cvar_limit=0.10 可行无需回退）；默认关闭下组合结构与 P1-2 前一致（不退化）。

### M4 P2-3 / P2-4 / P2-5 ✅（本次，PoC）
- **P2-3** 新建 `quant/multifactor/spectral.py`：`fft_periodogram` / `detect_dominant_periods`（Hann 窗 + 局部极大值提取占优周期及归一化强度）/ `lowpass_filter`（频域理想低通，作为 3σ winsorize 频域补刀去噪）/ `permutation_test_spectral`（随机打乱时序重算频谱，经验 p-value 证伪）。**关键验收**：白噪声序列 p>0.05（证伪成功、不显著）；含周期信号 p<0.05（通过证伪、可信）。不接主链路。
- **P2-4** `quant/multifactor/factors.py` 追加：`cross_sectional_projection`（截面回归显式化为 L² 子空间正交投影 + 正交残差即纯 alpha）/ `gram_schmidt_orthogonalize`（标准正交基，modified/classical）/ `symmetric_orthogonalize`（Z(ZᵀZ)^{-1/2} 因子对称正交化，相关矩阵≈I，替代仅文档「建议」的 PCA 正交化）/ `cvar_riesz_dual`（CVaR 的 Riesz 表示 / 对偶范数 = 最差 (1−α) 分位损失均值，正是 P1-2 CVaR 线性化约束所依赖的对偶）/ `orthogonalize_factor_panel`（便利封装）。
- **P2-5** 新建 `quant/execution/optimal_execution.py`（含 `quant/execution/__init__.py`）：Almgren-Chriss 变分法闭式解——`optimal_execution` 最小化 期望成本+λ·风险，双曲轨迹 x(t)=X·sinh(κ(T−t))/sinh(κT)（κ=σ√(λ/γ)）；λ→0 退化为匀速、λ→∞ 退化为瞬时；数值积分回报成本/风险并与匀速基准对比；`ac_params_from_cost` 由 `cost_model.CostConfig.impact_gamma` 粗略推导 γ；另加 `calibrate_gamma_ac`（按总参与率把 sqrt 冲击折算成二次暂时冲击 γ，使 γ 随单子相对 ADV 单调：小单→匀速、大单→前置）、`suggest_execution`（一笔订单→分段拆单表+参与率+建议）、`_intraday_clock`/`execution_table_markdown`（日内时钟与 Markdown 渲染），并加 κT 大值指数稳定路径。**坑**：numpy 2.5 已移除 `np.trapz` → 改用 `np.trapezoid`。
- **P2-5 接入实盘/周报拆单建议**：`scripts/plan_e_journal.py` 导入 `suggest_execution`/`execution_table_markdown`，新增 `_find_parquet`/`get_execution_inputs`（近 20 日 ADV+日波动）/`get_last_close`/`format_execution_suggestion`；`mode_daily`（信号触发时按当前可用现金推算拟买股数，给日内拆单建议）、`mode_weekly`（周报增「成交拆单建议」章节，逐笔买入给 Almgren-Chriss 轨迹），新增 `--lam`（默认 0.05，越大越前置）/`--exec-buckets`（默认 6）参数。**建议逻辑**：大单相对 ADV / 高波动→前置成交（降库存暴露风险）；单子相对 ADV 小、参与率<20%→匀速 TWAP；任一拆单段参与率>20%→告警拆细。
- 验证：`ruff check quant/execution scripts/plan_e_journal.py tests/` 全过；`tests/test_spectral.py`(5) / `tests/test_orthogonalize.py`(5) / `tests/test_optimal_execution.py`(10) 共 20 项通过（周期检测、置换检验双向证伪、低通去噪、Gram-Schmidt 正交、对称正交不相关、投影残差正交、Riesz 对偶=最差尾部均值、AC 目标≤匀速、方差更低、λ=0 退匀速、库存约束、前置成交、cost_model 映射、γ 随单子大小单调、`suggest_execution` 小单匀速/大单前置/表渲染）。真实标的 600519 冒烟：1000 股→匀速建议、参与率/个股成本合理。**结论**：P2-3/P2-4 为纯 PoC 未接主链路；P2-5 已接入实盘/周报拆单建议（只读建议，不自动下单）；P2-3 置换检验证明当前 IC≈0 的低信噪比序列无可信周期，符合「数学升级不创造 alpha」边界。

生成时间：2026-10-15 10:53:12
生成者：math-upgrade-plan（基于本对话框三轮数学审计收敛）
模型版本：不涉及（实施方案）
数据日期：2026-10-15
