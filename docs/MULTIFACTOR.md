# 多因子选股升级方案设计文档（MULTIFACTOR v1）

> 模型版本：multifactor v1 ｜ 生成时间：2026-10-14 ｜ 数据日期：2026-10-14
> 配套入口：`scripts/plan_e_backtest.py --mf` ｜ 库目录：`quant/multifactor/`
> 设计原则：**以扩展代重建**——回测引擎不新建，只在既有回测底座上接「截面打分 → 风险预算权重 → 组合净值」新范式；在役方案 E 默认行为零变化。

---

## 一、现有回测能力盘点（已落地 vs 缺口）

基于 `scripts/plan_e_backtest.py`（1492 行）、`scripts/factor_engine.py`、`scripts/factor_significance.py` 的实际代码核对。

### 1.1 已落地能力（直接复用，不重复建设）

| 模块 | 函数 / 接口 | 说明 |
|---|---|---|
| 成本模型 | `backtest_one(fee, slippage, bj_slippage)` | 单边佣金 **万三** + 滑点 **0.1%**；北交所额外滑点压力档 `--bj-slippage 0.003`（叠加）。双边合计 `2×(fee+slip)`。 |
| 核心指标 | `compute_metrics(bt, daily_records)` | 返回年化、夏普、索提诺、最大回撤、卡玛、胜率、平均收益、期望收益、盈亏比、TP/SL/时间止损分布。组合净值由逐日收益 `cumprod` 还原。 |
| 敏感性 | `run_sensitivity` | hold/tp/sl 网格扫描，按夏普降序。 |
| 分层 | `run_stratify` | 沪深 / 北交所分层（告警北交所数据缺口）。 |
| 按年 | `run_by_year` | 按 `signal_date` 年份分层。 |
| Walk-Forward | `run_walk_forward` / `run_walk_forward_rt` | 固定参数扩展窗口滚动；`--retune` 训练窗内重选最优参数后 OOS（真 OOS）。 |
| Bootstrap CI | `_bootstrap_ci` | 2000 次重采样置信区间。 |
| 实盘回灌 | `--real` + `run_by_year_real` / `run_real_walk_forward` | 消费 `data/double_up/real_trades.csv`，n≥30 显著性闸口。 |
| 数据治理 | `require_fresh(max_age_days=2)` | 数据新鲜度校验；`--no-freshness-check` 可跳。 |
| 因子 IC | `factor_significance.calc_factor_significance` | 单因子 IC / RankIC / 分层胜率 / 单调性。 |

### 1.2 待补齐缺口（本方案新建模块）

| 缺口 | 现状 | 新建 |
|---|---|---|
| **成本不全** | 仅佣金+滑点；**无印花税（卖出千一）、无最低佣金（5 元）、无冲击成本** | `cost_model.py` |
| **范式单一** | 现役是「翻倍事件驱动」单票收益，**无截面多因子打分、无组合净值、无风险/仓位层** | `engine.py`（新范式）+ `factors.py` |
| **因子广度不足** | 仅有盘口/量价/趋势/波动率（事件样本内），**无估值 / 中期动量 / 情绪 / 资金流 / 基本面维度，无截面 IC 合成** | `factors.py` + `valuation_data.py` |
| **风险与组合空白** | 无 Barra 风险分解、无 ERC 风险预算、无约束优化（现役满仓×10%） | `risk_model.py` + `portfolio.py` |
| **过拟合防御不全** | 有 WF / 敏感性，但**缺 purged+embargo 时序交叉验证、IC 分年衰减检验、合成正则化对照** | `cv_utils.py` + `engine.py` |

> 结论：建议书中的 P0（回测+成本+过拟合底座）在本项目**部分已落地**（回测引擎、WF、敏感性、滑点、北交所压力档）；真正烧钱/待建的是 **U3 因子广度** 与 **U5/U6 风险与组合层**，以及 **U2 成本三项补齐**。升级重点据此聚焦。

---

## 二、盘口三因子 → 项目口径映射

现役打板方案的核心盘口三因子（换手率 / 封单比 / 板块强度）在 `factor_engine.calc_window_factors` 中以量价代理形式存在：

| 盘口因子 | 项目内代理口径 | 备注 |
|---|---|---|
| 换手率 | `amount_5d_avg` / `amount_20d_avg` / `volume_ratio_5d_20d`（量能/量比） | 成交额代理，缺股本分母时退化为量比 |
| 封单比 | `limit_up_5/10/20d`（涨停家数计数）、`continuous_limit`（连板数） | 封板强度代理 |
| 板块强度 | **未在 `factor_engine` 直接覆盖** | 本方案在 `factors.py` 新增「申万一级板块 N 日动量」作为板块强度截面因子 |

> 注意：现役因子是**事件样本内**时序因子，并非全市场截面打分。多因子模型需重算为**调仓日截面快照**（见 §三.2）。

---

## 三、五维因子定义（`quant/multifactor/factors.py`）

统一元信息：

```python
@dataclass(frozen=True)
class FactorMeta:
    name: str            # 如 mom_120d_ex5 / pe_ttm / lhb_netbuy_ratio
    category: str        # momentum | valuation | sentiment | fundflow | fundamental
    direction: int       # +1 越大越强, -1 越小越强（IC 校验后可覆盖）
    coverage: float      # 截面覆盖率；低于 0.6 在合成中剔除并披露
```

### 3.1 五维因子清单

| 维度 | 因子名 | 定义 | direction | 数据来源 |
|---|---|---|---|---|
| 动量 momentum | `mom_60d` / `mom_120d` | 60/120 日收益率 | +1 | daily |
| 动量 | `mom_120d_ex5` | 120 日收益剔除最近 5 日（防短期反转污染） | +1 | daily |
| 估值 valuation | `pe_ttm` / `pb_mrq` | 真实市盈率(TTM)/市净率 | -1 | akshare（real 模式） |
| 估值 | `val_proxy_size` | 对数总市值（替代估值锚） | -1 | daily→amount 推算 |
| 估值 | `val_proxy_liquidity` | 20 日成交额均值（流动性深度） | -1 | daily |
| 情绪 sentiment | `lhb_netbuy_ratio` | 龙虎榜净买入额 / 成交额 | +1 | `data/sentiment/lhb_*.parquet` |
| 情绪 | `zt_density_20d` | 20 日涨停密度（涨停天数/20） | +1 | daily |
| 资金流 fundflow | `fundflow_divergence` | 量价背离代理：价升量缩/价跌量增反向计分 | +1 | daily（量价） |
| 基本面 fundamental | `fund_proxy_accel` | 价格加速度代理（60 日斜率二阶导数） | +1 | daily |
| 基本面 | `roe_yoy_growth` | 真实 ROE 同比（real 模式） | +1 | akshare（real 模式） |

### 3.2 代理 / 真实双模（`valuation_mode: proxy | real | auto`）

- `proxy`：用对数市值、流动性深度、价格加速度等**纯量价代理**跑通全流程，当天可出结果。
- `real`：从 `data/valuation/` 读取 akshare 下载的真实 PE/PB/ROE；缺失则降级 proxy 并在报告披露。
- `auto`（默认）：`data/valuation/` 存在且完整 → 用 real，否则自动 proxy，零代码改动重跑。

### 3.3 合成方法（可解释优先）

1. 类内：各因子 `z-score`（截面去极值 winsorize 3σ）→ 等权求和得「类得分」。
2. 类间：滚动 250 日 **IC-IR**（信息比率）加权五类得分 → 综合截面打分 `score_cs`。
3. 覆盖率闸门：单因子截面覆盖率 < 0.6 在合成中剔除并披露（如情绪仅 2025-2026 覆盖）。

---

## 四、风险模型（`quant/multifactor/risk_model.py`）

Barra 式近似（不依赖 CNE5 订阅）：

- **结构**：`Σ = B' · Σf · B + D`
  - `B`：个股 ×（申万一级行业哑变量 + 风格因子）载荷矩阵，由**截面回归**估计（`statsmodels` OLS）。
  - `Σf`：风格/行业因子协方差，用 **Ledoit-Wolf 收缩**（`sklearn.covariance.LedoitWolf` 或 `statsmodels` 实现）保证正定。
  - `D`：对角特质风险（回归残差方差）。
- **风格因子**（由价格/成交构造，无需外部数据）：规模（对数市值）、价值（估值代理）、动量（中期收益）、波动率（20 日）、流动性（成交额）。
- **行业映射**：`data/industry/sw_third_list.csv` → `sw_first_list.csv` 三级→一级映射，做行业哑变量。
- **输出**：个股风险贡献 `RC_i = w_i · (Σw)_i`，用于 ERC 优化与暴露监控。

---

## 五、组合层（`quant/multifactor/portfolio.py`）

ERC（Equal Risk Contribution）风险预算，替代现役主观满仓×10%：

- **目标**：最小化各标的边际风险贡献偏差（`scipy.optimize.minimize`，SLSQP）。
- **约束**：
  - 个股权重 `≤ max_weight`（默认 8%）；
  - 申万一级行业暴露 `≤ max_industry_exposure`（默认 30%）；
  - 周换手 `≤ max_weekly_turnover`（默认 40%，相对上期组合）；
  - 回撤围栏：组合回撤超阈值时降仓至现金。
- **可复现**：固定随机种子 `seed=42`；权重归一化后取 Top-N（`top_n=30`）。

---

## 六、成本补齐（`quant/multifactor/cost_model.py`）

在现役佣金+滑点基础上补齐三项缺口，统一为组合级成本估算：

| 成本项 | 参数 | 公式 |
|---|---|---|
| 佣金 | 万三，双边 | `max(amount×0.0003, 5元)`（最低 5 元） |
| 印花税 | 卖出千一 | `sell_amount × 0.001`（仅卖出） |
| 滑点 | 主板 0.1% / 北交所 0.3% | 双边 `price × slip` |
| 冲击成本 | 参与率模型 | `γ × sqrt(participation_rate)`（参与率=单笔成交额/市场成交额） |

> 现役 `backtest_one` 的成本语义（佣金+滑点、北交所叠加）保留；`--mf` 模式调用 `cost_model` 计算组合调仓的真实成本拖累，并拆出「毛收益 vs 净收益」差额用于报告「成本拖累」指标。

---

## 七、过拟合防御（U4 补齐 + 复用）

| 手段 | 实现 | 复用/新建 |
|---|---|---|
| 样本外 | T+1 开盘成交（防未来函数） | 现役语义 |
| Walk-Forward | 扩展窗口滚动 + 训练窗重选参（`--retune` 思路） | 复用思路，engine 内实现 |
| 参数敏感性 | hold/tp/sl/权重上限网格 | 复用 `run_sensitivity` 思路 |
| **purged + embargo CV** | 时序交叉验证，剔除训练-测试泄漏窗口 | `cv_utils.py`（新建） |
| **IC 分年衰减** | 各因子逐年 IC / ICIR，衰减>50% 报警 | `engine.py`（新建） |
| **合成正则化对照** | 类间 IC-IR 加权 vs 岭正则加权，对照稳定性 | `engine.py`（新建） |

---

## 八、回测范式与有效性边界

### 8.1 新范式（截面 → 组合净值循环，`engine.py`）

```
调仓日(周五)截面快照 → 五维因子打分 score_cs → risk_model 估 Σ → portfolio ERC 权重 w
→ 次周一开盘成交(T+1) → 逐日组合净值(含 cost_model 成本) → 下一调仓日再平衡
```

- 复用 `compute_metrics`（组合逐日收益→指标）与 `_bootstrap_ci`（指标置信区间）。
- 分年 / 牛熊分层：对组合日收益序列按年份 / 市场状态（沪深300 20 日均线）切片后复用 `compute_metrics` 实现（比强耦合事件 API 更干净）。

### 8.2 口径约束（防未来函数 / 偏差）

- 前复权价（与翻倍样本口径一致）；剔除 ST / 停牌 / 上市 < 60 日。
- 基准：沪深300。
- 情绪数据仅覆盖 2025-2026，窗口外按缺失率剔除或中性化，报告披露。
- 估值 real 数据待 akshare 下载，未到位前 auto 降级 proxy。

### 8.3 失效条件（明确边界）

- 因子 IC 连续 2 年转负 / ICIR<0；
- 牛熊分层中熊市夏普显著为负且回撤超围栏；
- 组合换手触顶（>40%）却无超额 → 策略拥挤。

---

## 九、目录结构与文件职责

```
quant/multifactor/
├── __init__.py        # 包导出; MF_VERSION='v1'
├── factors.py         # 五维因子库 + FactorMeta + 合成(z-score/IC-IR)
├── valuation_data.py  # data/valuation 读取对齐 + 代理因子 + valuation_mode
├── risk_model.py      # Σ=B'ΣfB+D; 申万哑变量+风格因子; LW 收缩
├── portfolio.py       # ERC(SLSQP) + 约束(权重/行业/换手/回撤)
├── cost_model.py      # 佣金(最低5元)+印花税+滑点+冲击
├── cv_utils.py        # purged+embargo 时序 CV
└── engine.py          # 多因子回测引擎(截面→权重→T+1→净值循环)
scripts/
├── download_valuation.py  # akshare 下载(–probe 先行, 指数退避, 增量)
├── factor_engine.py       # [MODIFY] + --cross-section 调仓日快照
└── plan_e_backtest.py     # [MODIFY] + --mf 子命令(默认行为不变)
data/
├── valuation/             # [NEW] pe_pb_daily.parquet / fundamentals.parquet
├── factors/cross_section_*.parquet  # [NEW] 调仓日截面快照(long)
└── plan_e/plan_e_backtest_mf_v1_*.{csv,md}  # [NEW] 规范报告
```

> **Schema FROZEN**：估值/财务只写 `data/valuation/`；截面快照写 `data/factors/` 新文件名；绝不改 `daily`/`adj_factor` parquet。

---

## 十、报告规范（复用项目 Markdown 格式）

文件名：`data/plan_e/plan_e_backtest_mf_v1_YYYYMMDD_HHMMSS.{csv,md}`，含：

1. 核心指标：年化 / 夏普 / 索提诺 / 最大回撤 / 卡玛 / 胜率 / IC / ICIR / 换手 / **成本拖累**；
2. Top-N 持仓明细（代码 / 权重 / 行业 / 入选因子）；
3. 统计分布（因子覆盖率、行业暴露分布）；
4. 风险提示 5 条（数据边界、情绪覆盖缺口、估值代理降级、过拟合残留、实盘滑点上行）；
5. 下一步建议 + 生成时间 + 模型版本 + 数据日期。

> **不承诺绝对收益**；所有收益均为历史回测口径，实盘滑点/冲击可能高于假设。

---

## 十一、验证与零回归

- `make canary`（6 项）/ `make lint`（无错）/ `make status`（timer 正常）；
- 在役方案 E 默认参数路径完全不变，改后跑 `--today` 等快速路径与旧输出 diff 验证零回归；
- 端到端由 `tdx-quant-workflow` 校验：多因子链路与数据采集 / 方案 E 回测 / 预测流程衔接无冲突。

---

## 十二、数据边界声明（重要）

- **估值/基本面 real 数据**：依赖 akshare，本环境东财 `stock_zh_a_hist` 曾被阻断；下载脚本 `--probe` 探测，失败即降级 proxy，报告明确披露「代理因子为主」。
- **情绪数据**：龙虎榜 / 涨停池仅 2025-2026，窗口外 NaN 按缺失率剔除或中性化。
- 沪深池真实历史行情 2020-01 至 2026-10（5,235 个文件，`data/parquet/daily`；北交所已下线）。
