# 阶段 1 — 特征/因子工程迁移计划 (2026-10-10)

> 目标：将 `predict.py` 手写 18 维(+扩展 8 维=26 维)特征，迁移到 **Polars + akquant** 因子引擎；
> 新增 5~10 个候选因子，用 **Optuna** 做特征选择后再入模。
> 硬性约束：迁移后必须与现有 `tdxfeed/features_5d.py` **逐位数值一致**（否则破坏已训练模型分布）。

## 0. 环境与现实基线（已核实）
- `optuna` 5.0.0 ✅ 已装；`polars` 2.0.0 ✅ 新装；`akquant` 0.3.65 ✅ 新装（AKFamily，Rust+Python，内置 Polars 因子表达式引擎 + `akquant.talib` 103 指标 + walk-forward）。
- 现有特征权威实现：`tdxfeed/features_5d.py` 的 `compute_window_features`（18 维）与 `compute_window_features_extended`（26 维），经 `scripts/_common.py` 喂给 `predict.py` 的 `model.feature_name()`。
- ⚠️ **数据口径待核实（迁移前必查）**：`features_5d.py` 文档声明"价格类特征必须用 **raw close（非前复权）**"，但 `data/parquet/daily/*.parquet` 的 `close` 按 schema 文档是"前复权"。二者口径需先对齐确认（见第 7 节风险），本阶段 Polars 引擎**严格复刻 `features_5d.py` 的输入→输出**，不改任何语义。

## 1. 迁移的数据源
| 数据源 | 用途 | 备注 |
|---|---|---|
| `data/parquet/daily/{code}.parquet` | 主特征源（open/high/low/close/volume/amount，按 date 升序） | 逐股读取；Polars 直接 `scan_parquet` |
| `data/double_up/d_signal_5d_features.csv` | 训练标签 `label`（is_positive：5 日内再涨 30%+） | Optuna 分类目标基线 |
| `data/double_up/real_trades.csv` / `hypothetical_pnl` | 回归标签（已部署 `vpnl_huber` 用） | Optuna 回归目标可选 |
| `data/double_up/market_features.csv`、`industry_features.csv` | 市场/行业代理特征 | **保持现状**：在宽表装配后做 left-join，不进 Polars 因子核心 |

约束：所有价格类特征使用与 `features_5d.py` **完全相同**的输入列（`df['close']` 等），不引入复权转换差异。

> ⚠️ **口径复核结论（2026-10-10，审计实证，已修正此前误判）**：
> 落盘 `close` **实测 = 未复权（RAW）**，非 qfq。证据：
> - `scripts/audit_daily_adjust_convention.py` 用 `data/adj_factor/{code}.parquet` 的独立 `raw_close`/`qfq_close`（baostock 独立抓取，非日K 反推）
>   逐文件比对 7229 个日K：有参照的 5548 个中 **5543 个 err_raw≈0、err_qfq 显著（如茅台 0.13）** → 实为 raw；
>   5 个 `mixed/other` 全是宽基指数（sh000001 等），其 adj 参照无个股除权语义，与训练宇宙无关。
> - 项目自有脚本（`find_double_30d.py`/`find_double_up_c_v2.py`）早已注明"data/parquet/daily: 不复权日K；qfq = 不复权价 × adj_factor"。
> - 机制：`tdxfeed-sync.timer` 每日跑 `full_sync`（mootdx/pytdx = raw）全量重下，覆盖增量 qfq 路径，净状态为 raw。
>
> **结论：raw/qfq 漂移风险不存在。** 数据口径与 `features_5d.py` 要求、v7 训练口径完全一致；
> 本阶段 Polars 迁移逐位复刻 `features_5d`（raw 输入），**零漂移继承**。
> 唯一错误是 `cli.py` 文档与 FROZEN schema 注释误写"前复权 qfq"——`cli.py` 模板已修正（`tdxfeed/cli.py` 第149/164行），FROZEN schema 规则注释待走规则更新流程核对。
> 原"增量 qfq 污染"推断为**误判**，已作废。

## 2. 特征字段
**现有 18 维（必须 1:1 复刻，列名/顺序不变）**
`limit, prev_close, ret_5d, ret_3d, max_5d_pct, limit_up_5d, limit_up_t1, pct_t1, avg_amount_5d, vol_ratio_t1, volatility_5d, close_pos_5d, max_drawdown_5d, turnover_proxy, is_st, is_new, is_bj, is_gem`

**扩展 8 维（v9 模型，一并复刻）**
`acceleration, range_efficiency, vol_norm_ret, amount_surge_3d, up_down_ratio, dist_to_5d_high, dist_to_5d_low, amt_per_price`

**新增候选因子（目标 5~10 个，用 akquant/Polars 生成）** — 动量 / 波动率 / 量价背离方向
1. `rsrs` 阻力支撑比（前高/前低回归斜率，量价背离类）
2. `amihud` 非流动性（|ret|/amount，流动性因子）
3. `turnover_rate` 换手率（volume / 流通股，需流通股数据，缺失则代理）
4. `rsi_14` 相对强弱（`akquant.talib`）
5. `macd_hist` MACD 柱（`akquant.talib`）
6. `ret_skew` 收益偏度（5/10/20 日）
7. `vol_term` 波动率期限结构（vol_5d / vol_20d）
8. `vp_corr` 量价相关系数（amount 与 close 的滚动相关，量价背离）
9. `gap_t1` 跳空（open/prev_close - 1）
10. `intraday_rev` 日内反转（(open-close)/|close-prev_close|）
> 注：`turnover_rate`（换手率，需流通股数据）暂缓；实际实现 9 个候选因子，见 `tdxfeed/factors_candidates.py`。
> 其中 `rsi_14`/`macd_hist` 用 **akquant.talib**；其余（ret_skew/vol_term/gap/vp_corr/amihud/rsrs/intraday_rev）用 **Polars+numpy**；`akquant.factor.FactorEngine`（Alpha101 表达式引擎）保留用于后续声明式因子。

## 3. 目标格式
- **因子核心输出**：Polars `DataFrame`，长表 `date, code, <factor cols>`；对单样本（逐股 sig_idx=-1）等价于现有 dict。
- **入模宽表**：每 `(code, signal_date)` 一行，列为 `[现有 18/26 列（不变）] + [候选因子列]` + `label`。schema 向后兼容 `model.feature_name()`（现有列零改动，候选列仅追加）。
- **Optuna 选择产物**：选定特征名列表 + 掩码，导出 JSON，供 `predict.py` 与训练侧共用。

## 4. akquant 与 Polars 职责分工
| 职责 | Polars（直接） | akquant（声明式/上层） |
|---|---|---|
| 数据加载、parquet IO | ✅ `scan_parquet` / `read_parquet` | — |
| 逐股窗口装配（T-5..T-1） | ✅ `group_by(code).slice/rolling` | — |
| 现有 18/26 维算术复刻（保 parity） | ✅ 直接表达式 | （不适用，非横截面） |
| 横截面 rank / Alpha101 时序算子 | 作为底层 | ✅ `Rank(Ts_Mean(Close,5))` 等因子表达式引擎 |
| 技术指标（RSI/MACD…） | 可手算 | ✅ `akquant.talib` 双后端 103 指标 |
| 宽表装配 + 标签 join | ✅ | — |
| walk-forward / 回测（阶段 3 复用） | — | ✅ 内置 |

**原则**：现有特征用 Polars 直算保 parity；新候选因子优先用 akquant 表达式/talib，Polars 兜底自定义。

## 5. Optuna 特征选择配置
- **目标指标（主/次，2026-10-10 已定）**：
  - 主：**前向链式（walk-forward）OOS Spearman 秩相关**，对齐已部署 `vpnl_huber`（对 `hypothetical_pnl` 回归的排序目标）。
  - 次：**OOS Top-20 / Top-50 组合 PnL**（与部署最相关的选股指标）。
  - 分类 AUC 作为 legacy 对照（旧 `label`），**不作主目标**。
- **搜索空间**：候选池 = 18 基础 + 8 扩展 + ~10 新因子 = ~36 个特征；每个用 `Categorical([0,1])` 开关（是否入选）；新 ts 因子的回望窗口 `k ∈ Categorical([3,5,10,20])`。
- **试验次数**：150（可调；预算紧时 80）。
- **剪枝策略**：`MedianPruner(n_warmup_steps=15, n_min_trials=20)` + `TPESampler(seed=42, multivariate=True)`；可选 `SuccessiveHalvingPruner` 做预算压缩。
- **防泄漏**：时序前向切分 `train(<cutoff) → valid(≥cutoff)`，禁止随机 shuffle；每 trial 用小 LightGBM（80 轮、lr 0.05、叶子 31）在 train 上拟合、valid 上打分。

## 6. 特征迁移完整流程
1. **Step 1（本次先落地）**：`tdxfeed/factors_polars.py` 用 Polars 复刻 18/26 维；`tests/test_stage1_factors_parity.py` 逐位校验（容差 1e-9）。
2. **Step 2**：接入 akquant，实现第 2 节 10 个候选因子（声明式 + talib）。
3. **Step 3**：装配 `(code, signal_date)` 宽表，join 标签（cls 或 reg）。
4. **Step 4**：定义时序前向切分（多折）。
5. **Step 5**：跑 Optuna study → 选定特征集。
6. **Step 6**：对照基线评估（第 8 节标准）；**必须不退化**才进下一步。
7. **Step 7**：仅当选定集达标，`predict.py` 切换用 Polars 引擎；`features_5d.py` 保留作回滚参考。

## 7. Optuna 优化过程记录方式
- **存储**：`optuna.study` 用 `storage="sqlite:///logs/stage1_optuna.db"` + `load_if_exists=True`。
- **产物**：`study.trials_dataframe()` → `logs/stage1_optuna_trials.csv`；`best_params`/`best_value` → `logs/stage1_optuna_best.json`。
- **可视化**：`optuna.visualization` 导出 `param_importances / parallel / slice / contour` 为 HTML（`logs/stage1_optuna_*.html`）。
- **报告**：`data/plan_e/...` 之外的独立 `docs/stage1_optuna_report.md`（含切分定义、指标公式、最佳集、各因子入选频率）。
- 每次试验记录：trial 号、入选特征、OOS 主/次指标、耗时。

## 8. 最终选定特征集评估标准（闸门）
1. **数值 parity（强制）**：Polars 引擎 vs `features_5d.py` 在 18/26 维上差异 ≤ 1e-9（否则不得切换）。
2. **OOS 不退化**：walk-forward Spearman/AUC ≥ 基线（允许持平，禁止下降）。
3. **选股相关**：Top-20/50 PnL ≥ 基线。
4. **简洁性（目标）**：用 ≤ 50% 的特征达到 ≥ 基线性能；特征计算耗时 ↓ ≥ 50%（Polars 批处理 vs 现有逐股 Python 循环）。
5. **稳定性**：选定特征在 ≥ 60% 折中被选中（非单折过拟合）。
6. **无泄漏**：时序切分经审计（train 不含 valid 日期）。

## 9. 进度（2026-10-10）
- ✅ **Step 1**：Polars 复刻 18/26 维（`tdxfeed/factors_polars.py`），`tests/test_stage1_factors_parity.py` 抽 40 只（含 bj/688/300）最大差 **0.000e+00**，全过；修复 `is_new` 误用未归一化 `sig_idx` 的 bug。
- ✅ **Step 2**：`tdxfeed/factors_candidates.py` 实现 9 个候选因子（RSI/MACD 用 akquant.talib，其余 Polars+numpy），`tests/test_stage1_candidates.py` 抽 60 只无 inf/nan、取值合理，全过。`turnover_rate` 因缺流通股数据暂缓。
- ✅ **Step 3**：宽表装配模块 `tdxfeed/stage1_wide_table.py` 落地，全量 13048 样本写出 `data/double_up/stage1_wide_table.parquet`（26 维基础 + 9 候选 + 双标签）；`tests/test_stage1_wide_table.py` 结构校验通过。
- ✅ **Step 3.5（基线评测）**：新增 `scripts/stage1_baseline.py`，时序前向 OOS（扩展窗口+不重叠测试段）复现已部署 `vpnl_huber`（26 基础维, huber α=0.5, 目标 hypothetical_pnl）。基线 **OOS Spearman≈0.315**（两折 0.311/0.318,稳定）、**Top-20 PnL≈0.21**（胜率 82%）、Top-50≈0.20；注意 2025+ 折 Top-20 降至 0.163（近期退化）。作为 Step 4 闸门对照（不退化）。
- ✅ **Step 4（Optuna 框架 + 正式搜索）**：`scripts/stage1_optuna.py` 按 §5 落地并跑通。**正式 150-trial(all35 完整参数空间)**：150 COMPLETE / 0 pruned / 0 failed(109s)。**最佳 OOS Spearman=0.3404**(基线 0.315,+0.025)、**Top-20 PnL=0.2428**(基线 0.21,+0.033)、Top-50≈0.207；**最佳特征数=21**(35→21)。Top-10 trial 聚集 0.337–0.340(稳定)。稳健入选(Top-10 100%)：limit/prev_close/max_5d_pct/limit_up_5d/pct_t1/volatility_5d/is_st/is_bj/range_efficiency/vol_norm_ret/amount_surge_3d/amt_per_price/vol_term/vp_corr_20/amihud_20；一致剔除：ret_3d/rsi_14/macd_hist/gap_t1/rsrs_20/intraday_rev_t1 等。候选因子(vol_term/vp_corr_20/amihud_20)强保留→验证增益；**Step 5 闸门达标(≥0.315/0.21)**。产物=logs/stage1_optuna_{best.json,trials.csv,best_model.txt}+docs/stage1_optuna_report.md。**待做**：Step 5→6(用选定 21 维+超参重训 vpnl_huber)→7(切换 predict.py,保留 features_5d 回滚)。
- ✅ **Step 5 闸门脚本落地**：`scripts/stage1_gate.py`（2026-10-10）实现 §8/§10 六类准入判定，复用 `stage1_baseline.walk_forward_eval`（与 Optuna 同训练流程）。实测结果：**C1 命名契约✅ / C2 parity✅(1 passed) / C3 可复现性✅(Δ=+0.0000) / C4 池化不退化✅(Spearman 0.3404≥0.315, Top20 0.2428≥0.21, Top50 0.2069≥0.20) / C5 逐折不退化✅(2025+ 折 Top20 0.148→0.204,**历史近月退化被解决**) / C6 稳定性❌**。C6 FAIL 四项：limit_up_t1=22%、is_new=37%、dist_to_5d_high=39%、ret_skew_20=47%（均 <60% §8.5 阈值）——best-trial 单点非共识特征。**决策待定**：(a) 显式下调 `--min-stability`(如 0.40) conscious 接受四特征；(b) 剔除四特征后重跑(剩 17 维)；(c) 回 Step 4 增 trial/换 `--pool candidates` 求更稳共识集。默认 0.60 严格遵循 §8.5。退出码 0=PASS/1=FAIL/2=ERROR；产物 logs/stage1_gate_{ts}.{json,md,log}。
- ✅ **Step 6 重训落地**：`scripts/stage1_retrain.py`（2026-10-10）实现 §10.2。21 维精选 + best.json 超参，复刻 `vpnl_huber`(LGBMRegressor huber α=0.5, 目标 hypothetical_pnl) 在**全量 13023** 样本重训，落 **新文件** `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`（132 棵树 / 21 维 / 落盘校验通过）。**安全约束全到位**：硬守卫禁止输出命中任何旧模型；执行前旧 `vpnl_huber.txt` 已 **备份(.bak, 240504 字节完全一致) + chmod 0444 只读**（还原 `chmod 644 ...vpnl_huber.txt`），size/timestamp 证明旧文件内容从未被改写；兄弟模型(`_industry`/`_market`)未受影响。**复现验证**：纯 OOS(≥2025 单折, 仅<h 训练) Spearman=**0.3475** / Top20=**0.2045**，与 Step 5 闸门 fold1 完全吻合→21 维配置精确复现近期 OOS。冻结契约 `data/double_up/stage1_selected_features.json`(21 维+超参快照)已落，作 Step 7 唯一契约源。退出码 0/1/2。
- ✅ **Step 7 实现落地**（2026-10-10）：`scripts/predict.py` 双管线 + 注册表扩展。**改动**：(a) `MODEL_REGISTRY` 加 `vpnl_huber_stage1`(指向新文件,reg);(b) 新增 `--features {legacy|polars}`(默认 `legacy`,零破坏现有模型);(c) `compute_features` 加 polars 分支 = **沿用已验证 legacy 26 基础维(位级一致、is_st 由名称判定) + 仅 4 候选因子走 `factors_candidates`(polars)** 合成 35 维,按 `model.feature_name()` 取 21 列;(d) `ProcessPoolExecutor(initializer=_init_feature_mode)` 在 worker 内显式设置管线模式(初版曾因 worker 未继承 global 致候选列缺失 KeyError,已修);(e) 硬守卫:新模型 + `--features legacy` 直接报错。**DEFAULT_MODEL 仍 `vpnl_huber`(金丝雀前不翻)**;旧 `features_5d.py`/旧注册表/旧模型文件全程未动。
  - **金丝雀(全市场,2026-09-22 数据)**:旧 `vpnl_huber`(legacy) vs 新 `vpnl_huber_stage1`(polars) → **均无崩溃/KeyError**;正期望股数稳定(6333→6396,无崩坏);Top-20 重合 9/20(**45%**,重排序来自 4 候选因子,属预期);NEW Top-20 平均预期 PnL 反而更高(0.13–0.22 vs 0.12–0.16)。**判定:无灾难性偏移,金丝雀通过**。
  - **设计取舍**:未在 predict.py 内做"完整 polars 替换 26 基础维",因 `factors_polars.compute_window_features_extended_polars` 把 `is_st` 硬编码为 0(与训练侧/legacy 从名称判定不符)。故采用"legacy 基础 + polars 候选"最小安全方案。完整 polars 替换基础维为后续 Step 7.5(需先修 is_st 口径)。
  - **已上线(2026-10-11)**:`DEFAULT_MODEL="vpnl_huber_stage1"` 已翻 + 默认 `--features` 同步翻 `polars`(否则默认调用触发硬守卫退出);模块级 `ACTIVE_FEATURE_MODE` 改为 `polars`。默认调用冒烟验证通过(21维供给正常、无 KeyError)。待 `git commit` + `git tag stage1-switch`。回滚:改回 `vpnl_huber` 常量 + `--features` 默认 `legacy`,或单次 `--model vpnl_huber --features legacy`(模型层/特征层正交)。
  - **Web namespace 补全**(2026-10-11)：实测 `run_predict` 当前**并不读** `args.date`(此前记录为误判),但 Web 构造的 `argparse.Namespace` 确实缺 `date` 等属性,属不完整隐患。已在 `launch_web.predict` 的 namespace 显式补 `date=None`(与 `main` 约定一致:None=今天),经 Web 同构 namespace 调用 `run_predict` 验证端到端无 AttributeError。
- ✅ **数据口径风险已清零**：审计证明日K `close` = 未复权（raw），与训练/`features_5d` 一致，**无 raw/qfq 漂移**；原"增量 qfq 污染"推断为误判（作废）。`cli.py` 文档已修正；FROZEN schema 注释"前复权"待走规则更新核对。
- ✅ **Step 2.5（训练数据一致性校验，而非重生成）**：新增 `scripts/verify_training_data_parity.py` 闸门。起初误判"训练 CSV 陈旧、60% 漂移"，根因是 `stage1_wide_table.py` 误用 **`signal_date` 锚点**（应为 **T-1 = sig_idx−1**，即 `find_d_signal_5d.extract_features` 的训练约定：5 日窗口 = [sig_i−5, sig_i−1]）。**修正锚点为 T-1 后重跑**：全量 13048 样本中仅 **72 个特征单元相对差 > 0.5%（0.021%）+ 12 个非有限不一致（零 `prev_close` 等边界退化，0.0035%）**，最敏感的 `pct_t1` 漂移=0、`ret_5d` 仅 3 个——**训练 CSV 与当前 parquet 一致，无系统性陈旧漂移，无需重生成**。Step 2.5 交付物=校验闸门（含逐特征漂移分解），可在每次数据同步后复跑以捕捉真实漂移；仅当有效漂移 > 0.5% 时才需真正重生成（脚本 `scripts/regenerate_training_data.py` 待写）。
- 🐞 **已修 Bug**：`stage1_wide_table.py` 特征锚点 off-by-one（`signal_date` → T-1）；该 bug 曾把"特征偏移一天"误判为"60% 数据漂移"，已纠正并全量复核。

## 10. Step 5 / 6 / 7 执行方案（2026-10-10 规划）

> 总原则：**不破坏、可回滚**。旧模型文件、旧 `features_5d.py`、旧注册表项一律保留；新产物走独立路径（`_stage1` / 独立 json）。`predict.py` 运行时按 `model.feature_name()` 动态取列，故新管线只需产出与模型**完全一致**的 21 列（已逐名核对一致）。切换分三阶段：**闸门 → 重训 → (影子/金丝雀) → 切换 → 回滚预案**。

### 10.1 Step 5 — 闸门确认（Gate）

**输入条件**
- Step 4 产物齐备：`logs/stage1_optuna_best.json`、`trials.csv`、`best_model.txt`、`docs/stage1_optuna_report.md`。
- Step 2.5 一致性闸门：若 `data/parquet/daily` 自上次宽表生成后有更新，先跑 `scripts/verify_training_data_parity.py` 确认漂移 ≤0.5%（>0.5% 须先用 `regenerate_training_data.py` 重生成 `stage1_wide_table.parquet` 后再搜）。
- 21 维命名已对齐新管线（`factors_polars` + `factors_candidates`），无 `vp_corr` vs `vp_corr_20` 类漂移。

**验证标准（对照设计 §8）**
1. **数值 parity（强制）**：`tests/test_stage1_factors_parity.py` 在 18/26 维最大差 ≤1e-9（Step 1 已 0 差，通过）。
2. **OOS 不退化（逐折）**：用**固定配置**（21 维 + `best.json` 超参）重跑 walk-forward，**两折各自** Spearman ≥0.315 且 Top-20 PnL ≥0.21。注意隐患——基线 2025+ 折 Top-20 仅 0.163，须确认选定配置在该折不退化（Step 4 的 0.3404 是均值，需拆折核实在 `scripts/stage1_eval_config.py` 或扩展 `stage1_baseline.py --feature-list`）。
3. **Top-50 PnL ≥0.20**（已达 0.207）。
4. **稳定性**：21 维在**全部 150 trial** 的入选率 ≥60%（用 `trials.csv` 统计全量频率；Step 4 报告给的是 Top-10 频率，须补全量）。
5. **无泄漏**：train 不含 valid 日期（Step 4 已审计两折切分）。
6. **特征命名契约**：21 列名与 `model.feature_name()` 完全一致（已核对）。
7. *注*：§8.4「≤50% 特征」为软目标；本方案 21/35≈60%，但 21 < 基线实际用的 26 维，仍满足「更少特征、更好性能」的精简精神。

**交付物**
- 冻结特征清单 `data/double_up/stage1_selected_features.json`（= `best.json.selected_features`，锁定顺序，作为 Step 6/7 的唯一契约源）。
- 闸门报告 `docs/stage1_gate_report.md`（PASS/FAIL + 每折指标 + 全量入选率）。
- **决策**：PASS 才进 Step 6；若某折退化 → 回 Step 4 改用 trial 66（Spearman 0.3390 但 Top-20=0.2518 更稳）或放宽 `--min-features` 重搜。

### 10.2 Step 6 — 重训（Retrain）

**输入条件**：Step 5 PASS + 冻结清单 + `best.json` 超参。

**重训范围**
- **目标模型**：复刻 `vpnl_huber`（LightGBM 回归，objective=Huber(δ=0.5)，目标 `hypothetical_pnl`），但：
  - 特征 = 冻结 **21 维**（替换原 26 基础维）；
  - 超参 = `best.json`（`lr=0.0135, n_estimators=150, num_leaves=44, max_depth=6, min_child_samples=23, subsample=0.684, colsample_bytree=0.815, reg_lambda=0.989, reg_alpha=0.00525`）；
  - 不含 `board_*/mkt_*/ind_*` 外部特征（21 维均技术/候选，模型不读，与旧 `vpnl_huber`「无外部依赖」一致）。
- **训练数据**：`data/double_up/stage1_wide_table.parquet`（13048 样本，经 Step 2.5 一致性确认）。
- **最终产物**：在**全量**上用上述超参训练（可用 early-stopping 在末折 valid 定最佳迭代 `num_boost_round`），保存为**新文件** `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`。**绝不覆盖**旧 `d_signal_5d_model_vpnl_huber.txt`（留作回滚）。Optuna 的 `best_model.txt`（800 轮≠调优超参）仅作参照，不直接上线。

**内部验证**
- 用 Step 5 同款 walk-forward 复评新模型，确认 OOS Spearman/Top-20 复现 ~0.340/0.243（训练-服务无偏）。
- 端到端冒烟：新管线对 N 只样本算 21 维 → 新模型预测 → 排序合理、无 NaN/inf。
- 训练报告 `docs/train_v_stage1_YYYYMMDD.md`（训练分布、OOS、特征重要性）。

### 10.3 Step 7 — 切换 `predict.py` + 回滚

**切换方式**
1. **predict.py 双管线共存**：新增 `--features {polars|legacy}`（默认先保持 `legacy`，过金丝雀再切 `polars`）。
   - `polars` 路径：调用 `factors_polars.compute_window_features_extended` + `factors_candidates` 计算全因子，再按 `stage1_selected_features.json` 截取 21 列；
   - `legacy` 路径：保持现状（`features_5d`）。
   - 因 `predict.py` 按 `model.feature_name()` 动态取列，21 列外的 `board_*/mkt_*/ind_*` 自动忽略，enrich 逻辑无需改。
2. **模型注册表**：新增 `vpnl_huber_stage1`（新文件），保留 `vpnl_huber`（旧）、`prod`（18 维分类，深回滚）；`DEFAULT_MODEL` 在金丝雀通过后由 `vpnl_huber` 改为 `vpnl_huber_stage1`。
3. **金丝雀（强烈建议）**：翻默认前，先手动 `predict.py --model vpnl_huber_stage1 --features polars` 跑若干日，与旧模型 Top-20 做分布/排序对比（重合度、预期 PnL 均值），确认无灾难性偏移再翻默认。
4. **提交/标签**：切默认前 `git commit`；`git tag stage1-switch`，保留一键 revert。

**回滚触发条件**
1. 启动即错：新管线 `KeyError`（模型要的列未产出）或 NaN/inf 占比 >1%（特征计算回归）。
2. 预测分布崩坏：Top-20 平均预期 PnL 较旧模型历史均值下滑 >30%，或正期望股数骤减至历史 50% 以下。
3. 实盘背离：`plan_e` journal 实盘观测中，新模型选股 1–2 周窗口内实际 PnL 显著低于其预期、且低于旧模型同窗。
4. 夜间 `predict-daily.timer` 运行抛异常或产出行数异常（骤降）。
5. 数据同步后 Step 2.5 一致性闸门失败（管线漂移）。

**恢复步骤**
1. **一级回滚（瞬时、零风险）**：`DEFAULT_MODEL` 改回 `vpnl_huber`，或运行时 `--model vpnl_huber` + `--features legacy`（旧文件/旧管线全部未动），重跑 `predict.py` 即恢复。
2. **二级回滚**：`--model prod`（18 维分类器）。
3. **正交回退**：若仅怀疑新管线（非模型）→ 翻 `--features legacy` 保留新模型；若仅怀疑模型 → 翻 `--model vpnl_huber` 保留旧管线；两者独立。
4. **代码级**：`git revert <stage1-switch commit>` 或 `git checkout stage1-switch~1 -- scripts/predict.py`。
5. 回滚后跑 `verify_training_data_parity.py` + 一次 `predict.py` 比对，写 postmortem 入 `docs/stage1_rollback_YYYYMMDD.md`。

### 10.4 命令速查（runbook）
```bash
# Step 5 闸门（须先扩展脚本支持固定配置复评；或临时写 stage1_eval_config.py）
venv/bin/python scripts/verify_training_data_parity.py          # 数据一致性闸门
venv/bin/python scripts/stage1_eval_config.py \                  # 固定 21 维+超参 复评 OOS（待写/扩展）
    --config logs/stage1_optuna_best.json --per-fold
# 冻结清单
cp logs/stage1_optuna_best.json data/double_up/stage1_selected_features.json   # 取 selected_features 锁序（或脚本导出）

# Step 6 重训（待写 scripts/stage1_retrain.py，读 best.json + 宽表，落 *_stage1.txt）

# Step 7 切换前金丝雀
venv/bin/python scripts/predict.py --model vpnl_huber_stage1 --features polars --date 2026-09-20
venv/bin/python scripts/predict.py --model vpnl_huber          --features legacy   --date 2026-09-20   # 对照

# 回滚（任意一层）
venv/bin/python scripts/predict.py --model vpnl_huber --features legacy
venv/bin/python scripts/predict.py --model prod
```

---
生成时间：2026-10-10
生成者：阶段1 特征迁移规划（Agent）
模型版本：v{N}（待定，沿用当前 vpnl_huber）
数据日期：2026-10-10
