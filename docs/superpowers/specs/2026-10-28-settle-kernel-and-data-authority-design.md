# 结算内核收敛 + 数据/路径权威层 设计（2026-10-28）

状态：待实施
范围：审计清单第 5、6 项（回测结算逻辑、因子/parquet/MCP 双栈）
拍板记录（2026-10-28，用户）：
1. 结算内核形态 = **双内核 + 共用组件**；
2. 迁移范围 = **11 处全量收编**；
3. 训练标签换口径 → **本轮一并重训**；
4. 数据层 = **连存量数据一起重写**，经实测收窄为「只重建 `derived`，其余存量不动」；
5. 双栈范围 = 权威层 + 因子/MCP 小收敛，**不合并** `features_5d`/`factors_polars` 与两套 MCP 目录。

---

## 1. 背景与目标

审计发现两类结构债，决定了「改坏了会不会被发现」：

- **结算口径有 11 份实现**，其中 3 处已漂移且无跨实现守卫；同一逻辑被复制后悄悄不等价（双触发结算方向、跳空成交价）。
- **parquet 读路径没有权威**：83 处 / 47 个文件的绝对路径、4 份 date32 写入实现，且 `resolve_parquet` 已实测失效（见 §2.2）。

目标：
1. 结算逻辑收敛为两个内核 + 一组共用组件，**默认口径只有规范一种**，非规范口径必须在调用点显式声明；
2. 数据读写只有一个入口，路径/日期/列约定可机械校验；
3. 所有收敛都有可自动化的等价或差异证据，且能在无 `data/` 的 CI 里跑。

---

## 2. 现状取证

### 2.1 结算实现（11 处）

| # | 实现 | 口径 | 问题 |
|---|---|---|---|
| 1 | `scripts/plan_e_backtest.py` `_settle_from_entry` | SL-first、跳空按 open、逐日回灌 | 规范 |
| 2 | `scripts/plan_e_sim_live.py` `simulate_trade` | 同 1 | 已对齐 |
| 3 | `scripts/plan_e_journal.py` `simulate_one_month` | **TP 先判、恒按阈值价、自然日计时** | 与文件头注释自相矛盾 |
| 4 | `scripts/daban_backtest.py` `_backtest_chase` | SL-first，但跳空仍记 `entry*(1-sl)` | 跳空未按 open |
| 5 | `scripts/train_pnl_proxy.py` `attach_pnl` | TP 优先 + 抹平，注释自称对齐 sim_live | 训练标签带乐观偏差 |
| 6 | `mcp/servers/backtest.py` | T+1 open、SL-first、跳空按 open | 第四份独立实现 |
| 7 | `scripts/v3_simulate.py` / `v3_simulate_rolling.py` | 收盘价判 TP/SL、TP 优先 | 研究口径，未声明 |
| 8 | `tdxfeed/daban/paper.py` backfill/成交感知 | T+1 open 买、T+1 close 卖、涨停不可买、跌停顺延 | 打板族 |
| 9 | `tdxfeed/daban/execution_backtest.py` | 无 TP/SL、跌停顺延 | 打板族 |
| 10 | `tdxfeed/daban/topk_pnl.py` | T+1 open 买、T+2 open 卖 | 打板族 |
| 11 | `scripts/daban_event_table.py` | 与 #9 逐行复制 | 未收敛的重复 |

现有守卫只覆盖同模块内等价与硬编码期望值，**没有任何跨实现等价比对**。

### 2.2 parquet / 路径（全量扫描）

| 表 | 文件数 | `date`/`datetime` 类型 | 处置 |
|---|---|---|---|
| `parquet/daily` | 5236 | 100% `date32[day]` | 不重写，加守卫 |
| `adj_factor` | 5221 | 100% `date32[day]` | 不重写 |
| `hfq_factor` | 5221 | 100% `date32[day]` | 不重写 |
| `parquet/min1/5/15/60` | 8386 / 4693 / 5221 / 5221 | 全字符串（格式一致） | 不重写，锁格式 |
| `parquet/derived` | 5200 | 5198 `int64` + 1 `date32` + 1 `timestamp` | 写端修好后整表重建 |
| `parquet-vol-backup` / `audit` / `factors` / `daban` / `double_up` / `valuation` | — | 混合 | **显式排除**，非 schema 契约范围 |

绝对路径字面量：**83 处 / 47 文件**（`scripts/` 56/25、`v5/` 20/16、`tdxfeed/` 6/5、`mcp/` 1/1）。

已实测的生产 bug：
```
resolve_parquet('sh600519') -> 600519.parquet  exists=False
resolve_parquet('sz300750') -> 300750.parquet  exists=False
```
`quant/multifactor/factors.py` 与 `scripts/factor_engine.py` 各有一份「裸名优先」的解析器；日线文件已全部带市场前缀，于是：
- `scripts/factor_engine.py` 负样本分支取不到任何沪深标的（静默 `continue`）；
- `scripts/sim_daily_compare.py` 空跑；
- 两者退出码均为 0。

### 2.3 因子与 MCP

- `features_5d`（pandas）与 `factors_polars`（polars）是**迁移期双写**，有 1e-9 parity 测试，但该测试依赖 `data/`，CI 中跳过。
- `scripts/feature_engineering.py` 另有第三份 8 维扩展因子实现（无测试）；它同时是缺失中间文件 `d_signal_5d_features_v9.csv` 的生产者。
- `get_limit` 存在两种不同契约，不是同义重复：
  - `features_5d.get_limit` = **判定阈值**（0.095/0.195/0.295，留 0.5% 缓冲，含 ST 并轨日期规则）；
  - `tdxfeed/daban/limit_price.limit_ratio` = **精确比例**（0.10/0.20/0.30，用于计算涨跌停价）。
  - `quant/multifactor/factors.get_limit` 抄了阈值版但**丢了 ST/日期分支**，使 `zt_density_20d` 在 2026-07-06 前的 ST 票上少数涨停。
- `mcp/servers`（6 server / 23 工具，行情与财务）与 `v5/mcp_servers`（math 20 工具 + llm 6 工具）**工具集不重叠**，属有意分层；真正重复的是 math 的 stdio/HTTP 双传输。业务 server 中仅 2 个有契约测试。

### 2.4 与既有规格的冲突及解决

`docs/superpowers/specs/2026-10-26-adjust-layer-hfq-design.md` 记录「不动 stage1/vpnl 链」，理由是 `_v9.csv` 不在盘且**无对照基线**。本设计的处理：动代码前先冻结 v1 基线（阶段 0），`_v9.csv` 不是标签链的必需环节（`train_pnl_proxy` 直接从 `d_signal_5d_features.csv` 算标签；`stage1_wide_table.build_wide_table(src_csv=...)` 可换源）。因此该结论在补上基线后可安全重开。

---

## 3. 结算内核设计

### 3.1 落点

新包 `tdxfeed/execution/`。依赖方向：`tdxfeed.daban` / `scripts/` / `mcp/servers` → `tdxfeed.execution`；`quant/multifactor` 只在需要时导入该包；`tdxfeed` 不反向依赖 `quant`，无环。

```
tdxfeed/execution/
  limits.py   # 涨跌停比例/阈值/价格/一字板判定
  costs.py    # 费用与滑点模型
  bars.py     # 规范化的日线视图
  single.py   # 内核 A：单笔 TP/SL
  hold.py     # 内核 B：打板持有期
  result.py   # TradeResult
```

### 3.2 共用组件

```python
# limits.py
def limit_ratio(code, *, is_st=False, on_date=None) -> Decimal
def limit_threshold(code, *, is_st=False, on_date=None, buffer=0.005) -> float
def limit_up_price(prev_close, ratio) -> float
def limit_down_price(prev_close, ratio) -> float
def open_is_limit_up(open_price, prev_close, code, *, is_st, on_date) -> bool
```

- `limit_ratio` 收编 `tdxfeed/daban/limit_price.limit_ratio`（精确比例，含 ST 并轨日期规则）；
- `limit_threshold` 由 `limit_ratio` 派生（`ratio - buffer`），收编 `features_5d.LIMIT_MAP`；
- `features_5d.get_limit`、`daban/limit_price.limit_ratio`、`quant/multifactor/factors.get_limit` 全部改为委托，**函数名保留**；
- ST 判定收进 `tdxfeed/names.py`（`load_universe()` 读 `data/all_a_stocks.json`、`is_st_name`），替掉 scanner / predict / daban 各自副本。

```python
# costs.py
@dataclass(frozen=True)
class CostModel:
    commission: float = 0.0003
    min_commission: float = 0.0
    stamp_sell: float = 0.0
    slippage: float = 0.0010
    def buy(self, price, shares) -> Fill
    def sell(self, price, shares) -> Fill
```

费用参数由调用点显式传入，不再散落 `*0.0013` 之类的字面量。

### 3.3 内核 A：单笔 TP/SL

```python
@dataclass(frozen=True)
class SingleTradePlan:
    judge: Literal["intrabar", "close_only"] = "intrabar"
    tp: float = 0.30
    sl: float = -0.10
    hold_days: int = 10
    sl_first: bool = True

def entry_index(bars, signal_idx: int, lag: int) -> int | None
def settle_single_trade(bars, entry_idx: int, plan, costs, *, buyable=None) -> TradeResult
```

**入场时点不进内核**：它是策略差异，不是结算口径。plan_e 在 T+1 确认后于 T+2 开盘入场（`lag=2`），`plan_e_sim_live` 在信号日次日开盘入场（`lag=1`），二者本来就不同——由调用方算出 `entry_idx`，内核只从该根开始结算。T+1 跟随过滤等条件同样留在调用方。

默认语义（非规范值必须在调用点显式写出）：
- 入场后逐根判定；`sl_first=True` 时同一根 K 线双触发结算**亏损侧**；
- 跳空穿越触发价按当日 **open** 成交：`min(sl_level, open)` / `max(tp_level, open)`；
- 超时按第 `hold_days` 根**收盘**成交；
- 返回逐笔成交 + 每日收益增量，资金曲线与 `return_pct` 同源。

`buyable` 回调保留给「开盘涨停不可买」等策略性过滤，默认不施加。

### 3.4 内核 B：打板持有期

```python
@dataclass(frozen=True)
class HoldPlan:
    buy: Literal["next_open", "signal_close", "signal_limit_up"] = "next_open"  # signal_limit_up = T 日涨停价买入
    sell: Literal["next_open", "next_close", "t_plus_2_open"] = "next_close"
    sl: float | None = None
    max_defer: int = 3

def settle_hold_trade(bars, signal_idx: int, plan, costs, limits, *, name="") -> TradeResult
```

固定语义：
- 开盘涨停 / 一字板 → `status=unbuyable_open_limit`，不入场；
- 卖出日跌停（或停牌）→ 顺延至下一个可交易日，最多 `max_defer` 日，`status` 保留 `deferred` 标记；
- 费用含印花税（仅在卖出侧）。

### 3.5 迁移清单与期望差异

| # | 实现 | 目标 | 调用点声明 | 数值是否变 |
|---|---|---|---|---|
| 1 | `plan_e_backtest._settle_from_entry` | A | `entry_idx = t1_idx + 1`（信号 T+2 开盘） | 否（报告 md5 必须不变） |
| 2 | `plan_e_sim_live.simulate_trade` | A | `entry_idx = 信号日 + 1`（T+1 开盘） | 否（入场时点本就是另一策略，保留） |
| 3 | `plan_e_journal.simulate_one_month` | A + 组合账本 | 规范默认，沿用其自身入场日 | **是**（SL-first、跳空按 open、交易日计时） |
| 4 | `daban_backtest._backtest_chase` | A | 规范默认 | **是**（跳空按 open） |
| 5 | `mcp/servers/backtest.py` | A | `entry_idx = 信号日 + 1`（T+1 开盘） | 否 |
| 6 | `train_pnl_proxy.attach_pnl` | A | `entry_idx = 信号日 + 1`（与 sim_live 同入场约定） | **是**（训练标签） |
| 7 | `v3_simulate` / `v3_simulate_rolling` | A | `judge="close_only"`, `sl_first=False`（显式声明原口径） | 否 |
| 8 | `daban/paper.py` | B | `buy="next_open"`, `sell="next_close"` | 否 |
| 9 | `daban/execution_backtest.py` | B | `sell="next_open"`（无 TP/SL） | 否 |
| 10 | `daban/topk_pnl.py` | B | `sell="t_plus_2_open"` | 否 |
| 11 | `daban_event_table.py` | 并入 #9 | — | 否（本就逐行复制） |

**等价证明**：
1. #1 迁移后 `plan_e_backtest` 全量报告 md5 与阶段 0 一致，不一致即停；
2. #2/#5/#8/#9/#10 用「随机 bars + 固定种子」跑旧新逐笔对照，diff 表入报告；
3. #3/#4/#6 的数值变化逐笔列出，并说明变化来源（双触发方向 / 跳空成交价）；
4. 迁移完成后保留 `tests/test_settle_kernel_parity.py` 作回归。

---

## 4. 数据与路径权威层

### 4.1 两个新模块

`tdxfeed/paths.py` —— 只定位，不 fallback：

```python
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))  # 沿用既有环境变量
def daily_path(code) -> Path         # 带市场前缀：sh600519.parquet
def minute_path(code, period) -> Path  # 带市场前缀：sh600519.parquet
def adj_factor_path(code) -> Path    # 裸 6 位：000001.parquet
def hfq_factor_path(code) -> Path    # 裸 6 位：000001.parquet
def derived_path(code) -> Path       # 带前缀 + 后缀：sh600000_indicators.parquet
def finance_path() -> Path
```

**命名规则按表不同，逐个编码，不做「统一命名」抽象**：`parquet/daily` 与分钟表带 `sh/sz/bj` 前缀，`adj_factor` / `hfq_factor` 是裸 6 位，`derived` 带前缀且以 `_indicators` 结尾。这是实测事实，不是风格选择。

- 删除「裸名优先、找不到返回不存在路径」的行为；
- 调用方传 prefixed stem（如 `fp.stem`）也必须能解析。

`tdxfeed/parquet_io.py` —— 只读写，日期口径集中：

```python
def read_daily(src, columns=None) -> pd.DataFrame   # date→datetime64、升序、按 date 去重保留最后
def write_daily(df, code) -> Path                   # 强制 date32 + 原子写（tmp→rename）
def read_minute(code, period, parse_time=False) -> pd.DataFrame  # 默认保留字符串
def read_derived(code) -> pd.DataFrame
```

- 收编 4 份 date32 写入实现：`tdxfeed/writers.py::_write_parquet_date32`、`tdxfeed/incremental.py`（v2 写入）、`scripts/download_hfq_factor.py`、`tdxfeed/amount_fix.py` 调用点；
- 写入端统一 `YYYY-MM-DD HH:MM:SS` 分钟时间格式（与现存 23k 文件一致）。

### 4.2 调用点迁移

| 目录 | 处理 |
|---|---|
| `scripts/` 25 文件 | 改导入 `tdxfeed.paths`；`quant/factors.resolve_parquet` 与 `scripts/factor_engine.py` 的同名函数改委托 |
| `tdxfeed/` 5 文件 | 直接使用 `paths.DATA_ROOT` / `parquet_io` |
| `mcp/` 1 文件 | 同上 |
| `v5/` 16 文件 | 保持与主包零耦合，新建 `v5/scripts/_paths.py`（读同一 `TDX_OUT_ROOT`） |

守卫 `tests/test_no_hardcoded_data_paths.py`：全仓 `.py` 扫描 `data/parquet`、`data/adj_factor` 字面量，命中即失败；白名单仅 `tdxfeed/paths.py` 与 `v5/scripts/_paths.py`。

### 4.3 存量数据处理

| 表 | 动作 | 理由 |
|---|---|---|
| daily / adj_factor / hfq_factor | **不重写**，加 schema 守卫 | 已 100% `date32` |
| min1/5/15/60 | **不重写**，写端锁格式 + 守卫抽样 | 已是统一字符串格式 |
| derived | 写端改 date32 后**整表重建 5200 个**（`process_one` 的 mtime 短路需 `force=True`） | 唯一仍留 int64 的生产中间表；重建前备份清单 |
| 其余目录 | **不动**，在契约文档中显式排除 | 备份与研究产物，不在业务读路径 |

`parquet-vol-backup`（275）与 `audit`（360）等混合类型目录不纳入契约，避免为「看起来统一」去重写不可逆的历史产物。

### 4.4 验收性验证

- `tests/test_parquet_schema_contract.py`：采样（或全量 footer 扫描）生产表，断言类型；`data/` 缺失时 skip；
- `scripts/factor_engine.py` 负样本取数从 0 → 正常（实跑前后行数对比）；
- derived 重建后 `train/scripts/build_v8_dataset.py` 能读出且日期正确。

---

## 5. 因子与 MCP 收敛

### 5.1 因子

1. **保留双写**：`features_5d` 为 26 维基础特征权威，`factors_polars` 为迁移期复刻；新增**合成 fixture 版 parity 测试**（60 根 bar，无 `data/` 也能跑），让双写漂移进入 CI。
2. **涨跌停规则单一来源**：见 §3.2；`quant/factors.get_limit` 改为委托后，`zt_density_20d` 在并轨前 ST 票上会变值，只影响 `plan_e_backtest --mf` 报表，**不影响 predict 默认模型，不触发重训**；重跑一次 mf 报表并附 diff。
3. **删除第三份副本**：`scripts/feature_engineering.py` 的 8 维自算改为调用 `factors_polars`，先做逐位 parity 抽检；脚本保留对 `d_signal_5d_features_v9.csv` 的产出能力。

### 5.2 MCP

1. **契约测试 2/6 → 6/6**：新增 `tests/test_mcp_contracts.py`：
   - `mcp/schemas/tools.json` 与各 server `list_tools()` 的名称、数量一致；
   - 每个工具 `inputSchema` 为合法 JSON Schema，必填项与实现一致；
   - 纯工具（calc 6 个）用固定参数真调用并断言结果；
   - 依赖 `data/`、vault 的 server 在数据缺失时只跑 schema 断言。
2. **math 双传输共享工具表**：抽 `v5/mcp_servers/math_tool_specs.py`（`name → inputSchema`），stdio `list_tools` 与 HTTP 工具文档均由其生成；parity 测试锁「stdio 工具集 = HTTP 可派发集」。`/health`、`/metrics`、`/batch` 属传输端点，不入工具表。
3. 两套 MCP 目录**不合并**（工具集不重叠）。

---

## 6. 分阶段实施

### 阶段 0 · 冻结对照（约 2 小时）

1. 跑 `scripts/stage1_baseline.py` + `scripts/stage1_gate.py`，记录当前逐折 Spearman / Top-20 PnL 为 v1 基线；
2. 备份到 `data/double_up/_pre_kernel_20261028/` 与 `data/plan_e/_pre_kernel_20261028/`；
3. 记录 `plan_e_backtest` 全量报告 md5。

### 阶段 1 · 抽内核 + 迁规范实现（0.5-1 天，零行为变化）

新建 `tdxfeed/execution/` → 迁 #1（md5 必须不变）→ 迁 #2。

### 阶段 2 · 迁移会改数的实现（约 2 天）

按 §3.5 顺序迁 #3 #4 #5 #6 #7 #8 #9 #10 #11；每处产出旧新 diff 表；#4 的下游 4 个研究脚本（`daban_filter_oos` / `daban_limit_topk_eval` / `daban_min60_exec` / `daban_relay3_robustness`）重跑并重生报表。

同步项：`mcp/schemas/tools.json` 随 #5 的函数签名更新；`tests/` 中硬编码旧行为的用例（`test_plan_e_sim_live_exit`、`test_daban_paper`、`test_plan_e_backtest_exit_fills` 等）按新口径先写 RED 再改实现。

### 阶段 3 · 重建标签与模型（0.5-1 天，CPU）

1. 标签写新文件 `d_signal_5d_features_vpnl_v2.csv`（旧文件不动）；
2. `stage1_wide_table.build_wide_table(src_csv=<v2>)` → `stage1_wide_table_v2.parquet`；
3. 冻结的 21 维 + 超参（`stage1_selected_features.json`）重训 → `..._stage1_v2.txt`；
4. **验收闸门**：逐折 Spearman 与 Top-20 PnL 不低于 v1 基线（含 2025+ 折）；达标才把 `predict.py` 默认模型切到 v2，不达标保留 v1 并如实记录退化；
5. v1/v2 同日输出对照，选股差异逐条解释。

### 阶段 4 · 数据/路径与因子/MCP（约 2 天）

`paths.py` + `parquet_io.py` → 迁移 47 文件 → derived 重建 → `limits_rule`/`names` 收编 → 因子 parity 与第三副本删除 → MCP 契约与 math 工具表 → 守卫。

---

## 7. 验收判据（Definition of Done）

1. `git grep` 不再有内核之外的结算循环；
2. 硬编码 `data/parquet` / `data/adj_factor` 路径 0 命中（白名单 2 个文件）；
3. 11 处迁移各有旧新 diff 表，其中规范实现报告 md5 不变；
4. v1 基线 + v2 逐折对比表齐全，模型切换有明确结论；
5. `make test` 两套全绿；无数据 CI 演练全绿（新增守卫必须被收集，通过数下限相应上调）；
6. 全部定时脚本（`plan-e-today` / `plan-e-journal` / `plan-e-double-up` / `daban-ticket` / `predict-daily`）手动跑过一轮且退出码 0；
7. AGENTS.md 增加三条约定：结算走 `tdxfeed/execution`、parquet 走 `paths`/`parquet_io`、`limit_ratio` 与 `limit_threshold` 不可互换。

## 8. 风险与回滚

- `data/` 内所有就地改动前先做带日期备份（gitignore 救不回来）；
- 模型切换 = 新文件名 + `predict.py` 默认项一行，回滚改回一行；
- 内核按阶段提交，每阶段可独立回退；不 force push；
- 共享工作树：`git add` / `git commit` 一律带显式路径，不碰其他会话的脏文件。

## 9. 明确不做

- 合并 `features_5d` 与 `factors_polars`（迁移期双写 + parity 更安全）；
- 把「精确比例」与「判定阈值」并成一个函数（语义不同，会误用）；
- 合并 `mcp/servers` 与 `v5/mcp_servers`；
- 重写 23k 个分钟文件与 backup/研究产物目录；
- 调整 `plan_e`/`daban` 的策略逻辑或恢复实盘入口。

## 10. 实施期更正（2026-10-28）

实施精读代码后发现规格 §3.5 有 5 行判断需要更正，其余按规格执行：

| 规格行 | 规格原写 | 实测更正 |
|---|---|---|
| #2 `plan_e_sim_live` | 数值不变 | **数值微变**：① `TIME` 窗口差一根——`simulate_trade` 判 `future[1..10]` 后在 `future[10].close` 出场，`plan_e_backtest` 判 `entry+1..entry+9` 后在 `entry+9.close` 出场，统一到 plan_e 后持有期短一根；② 成本口径从「减 `2*(fee+slip)` 的加法式」统一到 plan_e 的乘法式，差约 5e-6。逐笔 diff 入报告。 |
| #5 `mcp/servers/backtest.py` | 收编到 A，数值不变 | 只收编**阶梯**（`check_exit`）。`max_hold` 到期、`cooldown`、挂单/撤单状态机是策略差异，留在调用方。数值除一种 K 线形状外不变：**开盘已在止盈线之上、日内又跌破止损线**时，旧实现先判「开盘越线」记 `take_profit_gap@开盘`，内核按 `sl_first` 记 `stop_loss@止损价`。方向保守（盈利笔记成亏损笔），2% 日波动下 20000 次随机走行 0 例，已接受并钉死用例（`test_gap_up_beyond_take_profit_plus_intraday_stop_settles_loss_side`）。 |
| #9 `daban/execution_backtest.py` | 收编到 B | 它吃的是**行式事件表**（`next_open`/`next_close`/`next_lim_dn` 列），手里没有 bar 序列，无法构造 `settle_hold_trade` 的入参。实际收编范围＝`net_return` 的**卖出侧**算术（`CostModel.sell`）；`_exit_price` 的跌停判断读事件表预计算的 `next_lim_dn`（数据列，不是规则计算），不动；买入侧也不套 `round_trip`（限价单无买入滑点，双边滑点每笔差 5bp）。结算仍行式。`#11 daban_event_table.py` 是这张表的生产者，本轮 0 改动。 |
| #10 `daban/topk_pnl.py` | 收编到 B，数值不变 | 数值**微变**：成本改委托 `round_trip`，逐笔 `|Δ| ≤ 1e-7`（旧式买入佣金写成 `(1-fee)` 乘子，`round_trip` 写成 `1/(1+commission)`，差 `commission²`）。成交价/时点/复权/跳过原因不变；两处标签级变化：入场根停牌且出场根缺失时内核先报 `suspended`（旧实现先报 `no_exit`），日期字段改走 `date_str` 归一。另需给 `HoldPlan` 加 `sell_offset`（CLI `--exit-offset` 允许 >2）。 |
| §4.2 硬编码路径数 | 47 个文件 | 五张表（daily/min*/derived/adj_factor/hfq_factor）口径实测 **50** 个（`scripts` 41 / `tdxfeed` 9 / `mcp` 2），另 `v5` 19 个走独立 `_paths.py`。阶段 3+4 的守卫以实测清单为准。 |

---

## 11. 实施期更正（阶段 2，2026-10-28）

阶段 2（重建标签与模型）的四条实施差异，规格原文不改，以此节为准：

1. **文件名：就地重写，不写 `_v2`。** 规格阶段 3 写「标签写新文件 `d_signal_5d_features_vpnl_v2.csv`、模型 `..._stage1_v2.txt`（旧文件不动）」；实际是**就地重写**同名 CSV / 宽表 / 模型。理由：下游（`stage1_wide_table`、`predict.py`、报告）都按单一路径读，v2 文件名要求把新路径参数穿到每一处；回滚安全性由前置快照保证——`data/double_up/_pre_settle_kernel_20261028/` 五件套 + `MANIFEST.md` 的 md5 清单 + 一键 `cp -p` 回滚路径（`docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md` §5）。
2. **验收闸门：跨宇宙不可比，改用同宇宙消融。** 规格阶段 3 的闸门「逐折 Spearman / Top-20 不低于 v1 基线」在本轮**跨宇宙**条件下不可比：数字算得出来（0.2483 < 0.3475 按字面会被读成「回滚」），但两侧测试集构成不同——v1 基线是 13023 有效样本（含 bj/ETF）、测试窗口 4067 行，本轮是 9981 行沪深、测试窗口 2695 行。实测旧标签在新宇宙上的同协议 Spearman 是 0.2594（132 棵树）/ 0.2606（117 棵树），同样低于 0.315 闸门——该闸门对**任何标签版本**都不成立。本轮判据改为「同宇宙、只换标签」的消融：固定 9981 行、21 维特征、超参与树数，只换标签列，标签效应 Spearman ≈ −0.012 / Top-20 ≈ ±0.01（结果与复跑代码见 `docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md` §4）。
3. **v1/v2 同日输出对照：用匹配对照代替，且只作 sanity。** 规格阶段 3 第 5 项要求「v1/v2 同日输出对照，选股差异逐条解释」；实际以**匹配对照**（旧模型 vs 新模型同 holdout）代替。该对照的泄漏**不对称**：新模型在全量 9981 行（含 ≥2025）上重训，见过被评分的新标签；旧模型只见过旧标签。因此它只能排除「大幅退化」，不能证明新标签更好，**不是主判据**。
4. **§7 DoD 4「v1 基线 + v2 逐折对比表」改判据。** 本轮 `scripts/stage1_retrain.py` 用单次时间切分（`< 2025` 训练、`≥ 2025` 打分），没有多折，逐折对比表无从产生；v2 文件名也已作废（第 1 条）。该条 DoD 由三项替代证据承担：**同协议 v1 基线逐位复现**（0.3475 / 0.2045，见研究文档 §4）、**同宇宙消融分解**（主判据，第 2 条）、**匹配对照**（sanity，第 3 条）；「模型切换有明确结论」落地为研究文档 §5 的保留决策与其量化代价。

---

## 12. 实施期更正（阶段 3+4，2026-10-28）

本节记录「数据与路径权威层」（阶段 3+4）实施期的偏差，编号与三份计划文档开头的更正表一致：`docs/superpowers/plans/2026-10-28-settle-kernel-phase1.md` §3.5 与 `docs/superpowers/plans/2026-10-28-label-retrain-phase2.md` §6 的阶段 3 两项（编号 C14 / C15）已记在 §11，本节不重复；本阶段自身为 C4′–C13。规格正文保持批准时的原文，冲突时以本节为准。

| 编号 | 原文 | 实测 |
|---|---|---|
| C4′ | §4.2 的 47 文件统计 | 守卫口径（与 §10 的「50 个」不冲突）：**收编前**（Task 2 基线）`tests/test_no_hardcoded_data_paths.py` 的 AST 口径命中 **91 文件 / 120 行**（`scripts` 59/81、`tdxfeed` 6/7、`quant` 2/2、`mcp` 3/7、`train` 2/3、`v5` 19/20），**收编后全仓为 0**；§10 记的 50 个是「含五张表名的文件数」口径。91/120 里 tracked 部分是 89 文件 / 117 行（`train/` 在 `.gitignore:44`，CI 扫不到，那 2 个文件只在工作树改过、不入库），计划表的 90/119 是派发前预跑数，比实测少一个文件（`scripts/recompute_pnl_labels.py:30`）。另有 5 处字符串拼接型（`PK + sym`）守卫抓不到，靠 Task 3/6 的人工清单兜。 |
| C5 | §5.2 math 双传输工具集一致 | stdio 20 个，HTTP 23 个，HTTP 多 `math_concept_link` / `math_bilingual_translate` / `math_ontology_stats`（三个独有项在 HTTP 现序下标 9..11，不是拼在末尾，顺序按现状钉死）。parity 写成集合相等 `HTTP == stdio ∪ HTTP_ONLY`，外加长度 20/23。 |
| C6 | §4.3 derived 是 int64 | derived 的 `date` 实为 `timestamp[ns]`（重建前快照可验）；重建目标统一到 `date32[day]`。 |
| C7 | §4.1 第 4 个写入端是 `amount_fix.py` | `amount_fix.py` 不写盘；真正的第 4 端是 `scripts/download_universe.py`，写的是 int64 日期（收编后已改走 `write_table`）。 |
| C8 | §4.3 重建 5200 个 | `derived` 重建产出 **5,211** 个（canonical `daily` 5,236 中，25 只行数不足 60 个交易日，按 `compute_one` 的设计不产表）；全部 `date32[day]` / 29 列；与 daily 的行数逐只相等；无 `.v1.bak` 衍生物。重建前快照 `data/parquet/derived_pre_settle_20261028/`（`cp -al` 硬链接，5,200 文件，`MANIFEST.txt` 记 `created_at` / `git_head` / 文件数 / 体积）。 |
| C9 | §4.1 `finance_path()` | 实际命名 `finance_parquet()`。 |
| C10 | §4.1 分钟表写 `YYYY-MM-DD HH:MM:SS` | 时间列名是 `datetime`（`string`），`write_table` 只归一 `date` 列。 |
| C11 | §5.1-3 第三份副本改为调用 `factors_polars` | 改调 `features_5d.compute_window_features_extended` 并取 26 键；`factors_polars` 是迁移期复刻且只接受 polars DataFrame。 |
| C12 | §5.1-2 委托后 ST 票会变值 | 委托不带 `is_st`（无按日 ST 历史），主板阈值不变、ST 偏差仍在；实际变值的是 `689` 段，全库只有 `sh689009` 受影响（`paths.daily_files()` 里 `sh689*` 恰好 1 只）。 |
| C13 | §5.2-1 tools.json 与注册表一致 | 名称与顺序 6/6 一致；另有 `inputSchema` 漂移 **5 个**工具、description 漂移 **9 个**工具（并集 10 个：`read_finance` / `read_xdxr` / `list_codes` / `calc_macd` / `calc_kdj` / `calc_rsi` / `calc_boll` / `calc_adj_factor` / `search` / `add_doc`，json 侧更详细），本轮只锁名称/数量/必填自洽。 |

C8 的口径：验收判据是「`num_rows >= 60` 的 code 全部有 derived」，不是「文件数 == daily 文件数」。

C12 的遗留：`quant/multifactor/factors.get_limit` 没有 ST 分支，`zt_density_20d` 在 2026-07-06 并轨前的 ST 票上仍会少数涨停。补齐它需要**按交易日的 ST 历史**，仓库里只有 `data/all_a_stocks.json` 的当前名称快照（`tdxfeed.names.load_universe()`），拿当前名称回填全历史会把并轨后的日期也按 5% 判。因此本轮不做，列入「明确不做」，等有 ST 变更流水再开。

### 遗留与观察项

1. `tdxfeed/writers.py` 的 `write_day` 已是死代码（零调用），且它按整根 bar 覆写存在 1023 根截断陷阱——不要在后续任务里重新接回。
2. `tdxfeed/full_sync.py:232` 写 manifest 时 `last_date` 落的是 bar 对象的 `repr`（实测为 `[datetime.datetime(2026, 10, 28, ...)]` 一类），5,235 条里 **5,227 条**格式坏；这是**已知债务**，读取方不得依赖该字段做新鲜度判断。
3. `derived` **没有调度在刷新**：2026-10-29 重建时发现它已陈旧约 17 个交易日，`sh600000 1023→6415`、`sz000001 1030→7171` 属历史截断修复。本轮只重建一次，刷新调度与新鲜度守卫未做。
4. 下游 `train/scripts/build_v8_dataset.py`（**不入库**，`.gitignore` 的 `train/` 规则）吃 derived，derived 更新后其 dataset 需重跑；阶段 2 的对照结论内部自洽（新旧标签同源特征），不受影响。
5. 契约措辞统一为「derived 与 daily **同集**（不足 60 个交易日除外）」。
6. ST 判定为什么保留两份自写实现：`tdxfeed/daban/scanner.py:378` 与 `scripts/daban_min60_features.py:134` 仍各写一遍，且**多带「退」（退市整理）分支，与 `is_st_name`（只认 `ST`）不等价**。机械替换成 `is_st_name` 会把「退」票从 daban 的过滤/阈值判定里放出来，改变选股结果，所以保留为 daban 专属语义；收编只做了核心链路（predict / 因子）那一份。不是漏了。
7. `quant/multifactor/factors.get_limit` 无 ST 分支（C12 已述），补它需要按交易日的 ST 历史，本轮不做——`zt_density_20d` 在并轨前的 ST 票上仍会少数涨停，属已知偏差，不是收编遗漏。
8. `scripts/download_bj_daily.py` 是退役脚本，仍写 timestamp 日期（不走 `write_table`），不要当它是活路径。
9. `mcp/servers/finance_deep.py:52` 在**导入期** `mkdir`，CI 会建 `data/parquet/finance_deep/`；无害，但属副作用，记录在案。
10. `v5/README.md` 的 math 工具数滞后（已把四处计数改为 stdio 20 / HTTP 23 并指向 `math_tool_specs.py`）；M1/M2 表格是设计期记录，未列全。
11. 两份带日期的评审快照（`docs/WORKFLOW_REVIEW_2026-10-16.md`、`docs/WORKFLOW_REVIEW_2026-10-16-v2.md`）里「`Writers.write_day` + `write_parquet` 增量双写」的描述已在 2026-10-28 的写入口收编中失效（`Writers.write_parquet` 已删、`write_day` 成死代码）。AGENTS.md 已声明带日期 review 是一次性快照，正文不重写，只在文首各加一行提示；现状见 `AGENTS.md`「数据路径与表口径」与 `tdxfeed/parquet_io.py`。
