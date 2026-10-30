# hfq 读侧权威：价格特征与收益率标签切复权口径（2026-10-30）

状态：**设计已确认**，2026-10-30。三条拍板 —— ① hfq 回填到上市日（回填并发定 3）；② 收益率切、涨停留 raw；③ daban 与 stage1 两条线都重训，**线上模型不切**。

前置文档：`docs/superpowers/specs/2026-10-26-adjust-layer-hfq-design.md`。本文沿用它的逐维分派判据与双跑协议，但**因子权威换成在役的 `data/hfq_factor`**；该文 §2.2 规划的 `adj_factor_ext` 咽喉与 2026-10-29 的撤回结论继续有效，本文不复活它。

> **2026-10-30 后续补记（§5.3 残余已处理）**：`scripts/find_double_up_hfq.py` 已用 `adjusted.frame(kind="hfq")` 重筛宇宙，产出 `data/double_up/double_up_hfq_2020_2026_detail.csv`（17,794 笔 / 1,621 只，剔除 20 笔跨 suspect 上跳），`factor_engine` 已切、v3 两个死常量已删；`find_double_up_c_v2.py` 与线上模型都未动。对照见 `docs/research/2026-10-30-hfq-universe-rescreen.md`。三条口径更正：① 旧 v2 的 bj 4,219 条不可复现，原因是 `data/parquet/daily` 没有 bj 日线，**不是** adj 修订；② §1 的「49 个 / 27 个」按「日收益差」在今日数据上扫描未能复现（≥3% 为 42 起 / 37 起负向），保留原数字但注明口径未复现；③ hfq 源侧存在 3 起上跳坏点（`sz302132` 2025-02-18 / `sz003038` 2022-03-30 / `sz000981` 2022-02-28），`factor_jumps` 已能识别，宇宙筛选按 suspect 上跳剔除，因子表本身未修。

目标：让价格特征与收益率标签拿到无除权跳空的连续价格，同时让涨跌停、封板、委托价继续对交易所 raw 绝对价；把这次口径切换做成**同一样本集上的可解释消融**，而不是换一批数据重来。

---

## 0. 为什么要换权威（一句话版本）

`data/adj_factor`（qfq 锚）在 49 个事件上与 hfq 不一致，其中 27 个把一次正常行情误判成除权、制造 ≥3% 的**假跌幅**；`data/hfq_factor` 只有 11 只 / 12 次因子下跳，且能逐条列出。同一家源（baostock）内部，hfq 与 raw 自洽、qfq 与 raw 不自洽。所以读侧权威取 hfq，qfq 表退回它原本的用途（`find_double_up_c_v2.py` / `clean_double_up_detail.py` 的翻倍筛选，本轮不动）。

---

## 1. 实测前提（2026-10-30 量出来的，不是引用旧文档）

| 事实 | 数值 | 出处 |
|---|---|---|
| hfq 表规模 | **5221 只 / 7,687,033 行**，首行 **全部 = 2020-01-02**，末行 = 2026-10-28 | `data/hfq_factor/*.parquet` 全量扫描 |
| 日线表规模 | 5236 只（sh 2327 + sz 2909，**无 bj**，含 3 只指数与 sh600001 退市票），末日 2026-10-29 | `paths.daily_files()` |
| hfq 缺口 | **10 只**：sh000300 / sh399001 / sh399006（指数）、sh600001（退市）、sz001246 / sz301569 / sz301660 / sz301686 / sz301716 / sz301718（2025–26 新上市） | 逐文件比对 |
| baostock 覆盖 | `query_stock_basic()` 5564 只，**仅 sh/sz**；北交所无源，但北交所本来就不在日线表里 | 一次调用（0.3s） |
| 回填可行 | sh600000 全历史 **6534 根 / 4.2 秒**；与在役 hfq 重叠 **1651 行**，`raw_close` 最大相对误差 **0.0** | 一次性探针 |
| 回填成本 | 5221 × 4.2s ≈ 6.1 小时单线程；**3 并发约 2 小时**（本轮选定） | 同上外推 |
| hfq 质量 | **11 只 / 12 次**因子下跳（8 次 >0.5%），其中 6 次落在 **2020-11-25** 同一天 | 全表逐日比值扫描 |
| qfq 质量 | **49 个**与 hfq 不一致的事件，**27 个**造成 ≥3% 假跌幅 | 同一扫描 + 裁判实验 |
| 影响面 | 12 次下跳 × 5 日窗口 ≈ 数百条 / daban 1.07M 样本（<0.1%） | 由事件数与窗口宽度估 |

### 1.1 裁判实验：hfq 对、qfq 错（688048）

同一窗口，同时取 baostock 的 raw（adjustflag=3）、qfq（2）、hfq（1）：

| 日期 | raw | qfq | hfq |
|---|---|---|---|
| 2022-04-01 | 79.60 | 79.60 | 79.60 |
| 2022-04-06 | 74.25（−6.7%） | 56.65（**−28.8%**） | 74.25（−6.7%） |
| 2023-06-07 | — | 回到连续 | **+31.08%（真实转增）** |

`data/adj_factor/688048.parquet` 的因子恰有两次跳变：2022-04-06 → 0.7629、2023-06-07 → 1.000000，**与 baostock qfq 逐值一致**。即：qfq 把 2023 年那笔真实转增的一部分错误地记到了 2022 年上市后第 3 个交易日，制造一次 −28.8% 的假跌、再在 2023 年制造一次 +31% 的假涨。hfq 全程只有 2023-06-07 一次跳变，其余日期与 raw 逐值相同。

结论：**这不是本地缓冲表的问题，是源本身的 qfq 构造问题**；换权威不是换一个近似量，而是换一个自洽量。27 个同类事件集中在 2021–22 上市的 688xxx。

### 1.2 为什么不修 hfq 那 12 个下跳

000002 的形态是「一日凹坑」：2020-11-19 −2.56%、2020-11-20 +2.62% 回补。任何「从跳变日起整段缩放」的修法都会把回补变成新的 +2.6% 假跳；「钳到前缀最大值」只是把假跳推迟一天。修复启发式本身会造假，而代价是数百条 / 1.07M。**决定：不修表，只记录 + 守卫**（新增下跳即红，见 §7）。

---

## 2. 数据面：回填 + 三道门禁

### 2.1 回填器

新增 `scripts/backfill_hfq_history.py`，复用 `tdxfeed/hfq_factor.py` 的既有件：`bs_code`（`tdxfeed/hfq_factor.py:48`）、`write_atomic`（`:138`）、`_query_retry`（`:163`）。每只：

```
raw  = baostock(code, adjustflag=3, [1990-01-01, today])   # 未复权
hfq  = baostock(code, adjustflag=1, [1990-01-01, today])   # 真后复权
new  = factor = hfq_close / raw_close，schema 与在役表一致
old  = data/hfq_factor/{code}.parquet
重叠区 = old.date ∩ new.date
对账（§2.2）通过 → 写 new（覆盖全段，视图等价于「追加 [上市日, 2020) 段」）
```

写入前 `cp -a` 该只到 `data/hfq_factor.bak-20261030/`（全表 143MB，逐只备份总开销与整目录相当）。

### 2.2 三道门禁（承重墙）

| # | 检查 | 不通过时 |
|---|---|---|
| 1 | 重叠区 `raw_close` 最大相对误差 ≤ **0.5%** | **整只拒绝，零写入**。raw 是交易所绝对价权威（`data/parquet/daily`），回填器无权用第二源改写它 |
| 2 | 重叠区 `hfq_close` 最大相对误差 ≤ **0.5%** | 采用新拉的整段并记 `rebased`（与 `update_one` 既有的源修订语义一致；hfq 是派生因子表，不是价格权威） |
| 3 | 写入后行数 ≥ 原行数 | 整只拒绝 |

门禁 1 与门禁 2 的不对称是刻意的：价格表与因子表的法律地位不同，前者错一个字就改写了涨停判定，后者可在同源内重算。

### 2.3 运维

- **单实例**：`flock` 锁 `data/.state/hfq_backfill.json.lock`（照 `history_backfill.json.lock` 先例）。
- **单权威进度**：`data/.state/hfq_backfill.json`，字段 `done / rejected / not_in_source / rebased`，不新建第二份进度文件。
- **熔断**：连续 `20` 只失败即停轮（`FAIL_ABORT` 口径与 `tdxfeed/hfq_factor.py:45` 一致）；进程退出码非 0。
- **并发**：默认 `--workers 3`（本轮定），每 worker 独立 login。
- **可续跑**：`done` 集命中即跳过；`--limit / --dry-run / --symbols / --force`。
- **时窗**：**18:00 之后**跑。16:30 的 `tdx-sync.service` 第三条 `ExecStartPost=-` 就是 `tdxfeed.hfq_factor` 增量，同一批文件不能有两个写者。
- **范围**：只扫股票前缀（60/68/00/30）；指数与 ETF 不扫（它们不在训练集里，且 baostock 无源）。`not_in_source` 的 symbol 不改表。

### 2.4 回填完成后的不变量

- 每只 `factor` 首行 ≤ 日线首行（除 §1 的 10 只缺口）；
- 2020+ 段的 `factor` 除门禁 2 记录的 `rebased` 外不再变化（append-only 语义由 `update_one` 继续维护）；
- `data/adj_factor` 一字不动。

---

## 3. 读侧咽喉 `tdxfeed/adjusted.py`

### 3.1 API

```python
def frame(symbol: str, *, kind: str = "raw", strict: bool = False) -> pd.DataFrame
```

`symbol` 接受 `sh600000` / `600000` 两种形状（内部走 `paths.daily_path` 与 `paths.hfq_factor_path`，后者见 `tdxfeed/paths.py:115`）。

### 3.2 语义

- `kind="raw"`：直接返回 `paths.daily_path(symbol)` 的读取结果，**列序与数值逐值同盘**。涨停价、封板、委托价、金额全部走这条。
- `kind="hfq"`：`daily` 左连 `hfq_factor`（按 `date`），因子按日 `ffill`，`open/high/low/close × factor`；`volume / amount / date` 不动。`attrs` 至少带 `kind / factor_cov / factor_first / factor_last / factor_missing`。**`factor_cov` 定义**：有因子值的行数 / 日线行数，因子按 `ffill` 从 `factor_first` 向后填；`factor_first` 之前的行记 `factor=1.0` 且不计入覆盖——这样「因子表尾部滞后 1 天」不扣覆盖（ffill 兜住），只有「因子比日线起得晚」才扣。
- **只读 `data/hfq_factor` 一张因子表**，任何情况下不回落到 `data/adj_factor` 或 `data/adj_factor_ext`（后者仍是实验模块、零消费者）。
- 不落盘任何派生价格表——派生价格是读侧现算，落盘就会再造一张会过期的权威。

### 3.3 门禁

| 场景 | strict=True（训练/建表） | strict=False（预测） |
|---|---|---|
| 因子表 `raw_close` 与日线 `close` 重叠区中位相对偏差 > **1%**（`FACTOR_RAW_TOL`） | **raise** | `factor=1.0`，`attrs["factor_mismatch"]=True`，打一行日志，**不乘因子** |
| 缺因子文件 / 缺当日行 | `factor=1.0`，但 `factor_cov < 0.999` → **raise** | `factor=1.0`，`attrs["factor_missing"]=True`，打一行日志 |
| 因子首行晚于日线首行（>5 个交易日缓冲） | **raise** | 日志 + `attrs` 留痕 |
| 因子末日 < 日线末日 − 1 个交易日 | **raise**（这正是 10-26 spec §7 那条新鲜度守卫，改为对在役表生效） | 日志 + `attrs` 留痕 |

`raw_close` 一致性门禁专治 **6 位码冲突**：`paths.hfq_factor_path` 只认裸 6 位，`sh000001`（上证指数）会读到 `000001`（平安银行）的因子文件。实测这类错配的重叠中位偏差 **99.65%**，正常个股为 **0.0**，阈值 1% 把两者分得很开。daily 里同码双文件共 5 组（`000001/000016/000688/000852/000905`），全部由这条门禁兜住，不额外维护指数名单。

预测侧不 raise 的理由：新股或缺因子的票不能让整轮预测打挂；但它必须可见（`attrs` + 日志），而不是静默变成 raw。

### 3.4 单一权威

守卫扫源码：`features_5d.py` / `factors_candidates.py` / `factors_polars.py` / `train_daban_model.py` / `find_d_signal_5d.py` / `feature_engineering.py` / `train_pnl_proxy.py` / `tdxfeed/stage1_wide_table.py` 八个模块里**不得出现** `adj_factor` / `hfq_factor` 字面量，价格派生只能经 `adjusted.frame`。`find_double_up_c_v2.py` / `predict.py` 的 meta 展示不在此列（它们不产出特征/标签）。

---

## 4. 特征分派

### 4.1 判据（沿用 10-26 spec §2.3，一字不改）

**要拿去和真实报价/成交价对表的量 → raw；量本身就是比率的 → hfq。**

### 4.2 29 维（`compute_window_features` / `compute_window_features_extended`，`tdxfeed/features_5d.py:94,172`）

| 来源 | 维度 | 理由 |
|---|---|---|
| **hfq（13）** | ret_5d、ret_3d、max_5d_pct、pct_t1、volatility_5d、close_pos_5d、max_drawdown_5d、acceleration、range_efficiency、vol_norm_ret、up_down_ratio、dist_to_5d_high、dist_to_5d_low | 同尺度相除的比率；除权日必须连续 |
| **raw（8）** | limit、prev_close、limit_up_5d、limit_up_t1、is_limit_up_today、consecutive_boards、board_position、amt_per_price | 涨停判定是交易所绝对价规则；`amt_per_price` 分子是金额，分母必须同表 |
| **量纲不变（4）** | avg_amount_5d、vol_ratio_t1、turnover_proxy、amount_surge_3d | 只用 amount / volume |
| **枚举（4）** | is_st、is_new、is_bj、is_gem | 与价格无关 |

`is_limit_up_today` / `consecutive_boards` / `board_position` 目前在 `compute_window_features_extended` 内部用 `df["close"].pct_change()` 算，切口径时必须显式改用 raw 帧——这是最容易漏的一处。

### 4.3 9 个候选因子（`tdxfeed/factors_candidates.py:35`）

`rsi_14 / macd_hist / ret_skew_20 / vol_term / gap_t1 / vp_corr_20 / amihud_20 / rsrs_20 / intraday_rev_t1` **整帧传 hfq**：全部是比率或对同尺度缩放的统计量（Pearson 相关、回归斜率对正比例缩放不变）。stage1 的 21 维契约里有 4 个来自这里（ret_skew_20 / vol_term / vp_corr_20 / amihud_20）。

### 4.4 接口与兼容

- 两个特征函数加**关键字参数** `frame_hfq: pd.DataFrame | None = None`；`None` = 现行为（其余调用方零改动）。
- `predict.py:424` 的调用点、`find_d_signal_5d.py`、`feature_engineering.py`、`train_daban_model.py`、`tdxfeed/stage1_wide_table.py` 由 `--price-basis raw|hfq`（或注册表字段）决定是否多建一帧。
- 两帧必须同 `date` 序列：`adjusted.frame` 保证行数与日线一致，特征函数里断言 `len` 相等、`date` 逐值相等，否则 raise。

---

## 5. 标签

### 5.1 `hypothetical_pnl` → hfq

`scripts/train_pnl_proxy.py:69` 的 `compute_hypothetical_pnl` 改为从 `adjusted.frame(code, kind=...)` 取 bar；`scripts/recompute_pnl_labels.py` 与 `train_pnl_proxy.py` 加 `--price-basis`。产物分文件：

- `data/double_up/d_signal_5d_features_vpnl.csv`（raw，对照组，保持现状）
- `data/double_up/d_signal_5d_features_vpnl_hfq.csv`（hfq，实验组）

成本模型 `CostModel.round_trip` 是相对量，价格缩放无关；内核 A 的 TP/SL/跳空口径一字不动（`tdxfeed/execution/` 不改）。

### 5.2 daban 标签 → 留 raw

`scripts/train_daban_model.py:79` 的次日收盘涨停判定继续用 raw close。这是交易所口径，不能复权。

### 5.3 样本宇宙 → 本轮不动（2026-10-30 后续已重筛，见文首补记）

翻倍筛选（`find_double_up_c_v2.py:51` 的 `screen_one_qfq`）仍用 qfq，产出 `double_up_2020_2026_detail.csv`，决定 stage1 的 9981 个样本。理由：它决定样本集，切了就不是同一样本集的对照，双跑会失去解释力。

已知残余：qfq 的 49 个伪事件会污染翻倍判定（688048 那类会造成假翻倍/假未翻倍）。写进报告，单列一条后续：用 hfq 重筛宇宙 → 样本集会变 → 需要新一轮对照。本轮**不改** `find_double_up_c_v2.py` / `clean_double_up_detail.py`。

---

## 6. 双跑与产物

### 6.1 daban（干净消融：样本集不随特征变）

`scripts/train_daban_model.py` 加 `--price-basis raw|hfq`，同 seed / 切分 2025-06-01 / 同样本参数：

| 组 | 产物 |
|---|---|
| 对照 | `data/double_up/d_signal_5d_model_daban_raw_20261030.txt` + `daban_train_report_raw_20261030.md` |
| 实验 | `data/double_up/d_signal_5d_model_daban_hfq_20261030.txt` + `daban_train_report_hfq_20261030.md` |

现役 `d_signal_5d_model_daban.txt` 不覆盖。报告报三个数：OOS AUC（对照基线 0.7478）、Top-5/Top-20 命中率、13 个 hfq 维的重要性漂移。

### 6.2 stage1（同 9981 样本、同 21 维契约）

`tdxfeed/stage1_wide_table.py:57` 加 `--price-basis`，双建：

- `data/double_up/stage1_wide_table.parquet`（raw，现状）
- `data/double_up/stage1_wide_table_hfq.parquet`（hfq 特征 + hfq 标签）

`scripts/stage1_retrain.py --wide-table ... --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1_hfq.txt`（该脚本的旧模型保护逻辑照旧）。对照基线 fold1 **0.3475 / 0.2045**。

### 6.3 predict 注册表

`scripts/predict.py:100` 的 `MODEL_REGISTRY` 由二元组 `(path, "cls"|"reg")` 改为三元组 `(path, "cls"|"reg", "raw"|"hfq")`，同步 5 处解包（`ACTIVE_MODEL_PATH` / `ACTIVE_IS_REG` / 两处回退 / 主流程）；**现有 10 条全部 `raw`**（行为零变化），新增两条 `daban_hfq` / `vpnl_huber_stage1_hfq`，**`DEFAULT_MODEL` / `FALLBACK_MODEL` / `POSITION_MODE_MODEL` 不动**。`compute_features` 只在当前模型 `price_basis=="hfq"` 时多建一帧。

### 6.4 报告口径

`docs/research/2026-10-30-hfq-read-authority-dual-run.md` 必须把两个差分开报：

- **非口径漂移** = raw 组 AUC − 0.7478（代码/样本/环境漂移，应当接近 0）；
- **口径效应** = hfq 组 AUC − raw 组 AUC。

同时报 12 个 hfq 异常日命中的样本数（证明影响面估算）。

---

## 7. 守卫清单（TDD，RED 先行）

| 文件 | 锁住什么 |
|---|---|
| `tests/test_hfq_backfill_gate.py` | 重叠 raw 超差 → 整只拒绝且零写入；hfq 超差 → 记 `rebased` 并采用新段；行数只增不减；done 集命中即跳过（幂等）；熔断计数 |
| `tests/test_hfq_factor_monotone.py` | 全表下跳事件恰好 11 只 / 12 次且命中白名单（含日期与幅度）；新增一个即红。断言只覆盖 `>= 2020-01-01` 段（回填范围内、与训练相关），2020 前若新出现只打印记录、不阻断。**这是源漂移的绊线，不是修复器** |
| `tests/test_adjusted_frame.py` | `raw` 与盘上逐值一致；`hfq = close × 当日因子`；缺因子 = 1.0 且 `attrs` 留痕；strict 四条门禁（raw_close 偏差 / 覆盖率 / 首行滞后 / 末日滞后）各自 raise；不读 `adj_factor` |
| `tests/test_features_5d_price_basis.py` | 合成除权（raw 跳 −30%、hfq 平）：13 维与「无除权序列」逐值相同，8 维随 raw 变；`is_limit_up_today` / `consecutive_boards` 在除权日仍按 raw 判定 |
| `tests/test_label_price_basis.py` | 同一除权窗口：hfq 标签收益连续、raw 标签出现假止损；`--price-basis` 产物分离 |
| `tests/test_predict_price_basis.py` | 现有 10 条注册项特征值零变化（raw 帧不多读）；hfq 条目在缺因子时走告警路径而非 raise |
| `tests/test_single_factor_authority.py` | §3.4 的源码扫描 |

---

## 8. 验收标准

1. **回填**：全量跑完，`not_in_source` 只允许 §1 的 10 只；每只 `factor_first ≤ daily_first + 5 个交易日`（与 §3.3 的缓冲一致）；门禁 1 零误伤（或逐只记录 rejected 原因）；行数只增不减。
2. **因子新鲜度**：在役 hfq 末日不早于 daily 末日 − 1 个交易日（`test_adjusted_frame.py` 门禁）。
3. **分派机械可检**：合成除权消融通过；13/8/4/4 四组归属与 §4.2 表逐项一致。
4. **双跑**：daban 与 stage1 各出两份产物 + 报告；现役模型文件 mtime/字节不变；`predict.py` 默认模型不变。
5. **门禁全绿**：`venv/bin/pytest tests/ -q`、`venv/bin/ruff check`、`make lint-all`。
6. **影响面**：报告给出 12 个异常日命中样本数与两个差（非口径漂移 / 口径效应）。

---

## 9. 非目标

- 不改 `tdxfeed/execution/`（成交/结算/涨跌停语义一字不动）；不改 `plan_e` / daban 实盘入口与定时器。
- 不改样本宇宙（§5.3）；不改 `quant/multifactor`、`trend_hold_eval`（它用 raw × adj_factor 的 qfq，属另一条线）。
- 不切线上模型、不改 `DEFAULT_MODEL` / `POSITION_MODE_MODEL` / `FALLBACK_MODEL`。
- 不建任何派生价格落盘表；不复活 `adj_factor_ext`；不改 `data/adj_factor`。
- 不重建 `derived`、不改分钟表、不改 finance/xdxr。

---

## 10. 执行顺序与时窗

| 步 | 内容 | 时窗 |
|---|---|---|
| 1 | 回填彩排 3 只 → 全量 3 并发（约 2 小时） | 18:00 后，避开 16:30 同步 |
| 2 | 咽喉 + 逐维分派 + 守卫（与 1 并行，先用 2020+ 数据） | 盘中只写代码与测试 |
| 3 | daban 双跑（约 40 分钟，后台） | 回填通过覆盖率门禁后 |
| 4 | stage1 wide table 双建 + 双跑 | 同上 |
| 5 | 报告 + 文档收口（AGENTS.md 数据口径段补一行为 hfq 权威） | 末尾 |

---

## 11. 失败模式

| 失败 | 行为 |
|---|---|
| baostock 挂死 / 黑名单 | 30s 级超时 + 连续 10 只熔断停轮；`done` 集保证续跑不重来。10-30 实测接口可用（全历史 4.2s） |
| 回填中途与 16:30 增量撞写 | 由时窗规则避免；`write_atomic` 保证最坏是旧文件完好 |
| 门禁 1 误判（源 raw 与本地 raw 有尾差） | 整只进 `rejected`，人工复核；绝不部分写入 |
| 某只无因子（新股/退市） | `factor=1.0` + `factor_missing`；训练侧覆盖率门禁拦住，预测侧日志可见 |
| 因子表滞后 1 天且当日除权 | 门禁只容忍 1 个交易日；连续两天不前进才阻断（与 canary 的 `allowed_trading_days=1` 一致） |
| `features_5d` 漏改一处裸 close | 合成除权守卫直接红 |
| 双跑结果劣于基线 | 只报数、不切流；是否采用是后续独立决定 |
