# hfq 样本宇宙重筛与 stage1 对照（2026-10-30）

> 状态：已完成。`scripts/find_double_up_hfq.py` 产出新宇宙，stage1 样本链与宽表按 hfq 重建并重训；`factor_engine` 已切新 CSV，v3 两个死常量已删。**线上模型不切**（第 4 项仍待拍板）。
>
> 前置：`docs/superpowers/specs/2026-10-30-hfq-read-authority-design.md` §5.3「样本宇宙仍用 qfq」的残余，本轮处理。
> 对照报告：`docs/research/2026-10-30-hfq-read-authority-dual-run.md`（同一样本集上的特征/标签消融）。

## 0. 一句话结论

翻倍筛选的读侧从 qfq 换成 hfq 后得到 **17,794 笔 / 1,621 只**的新宇宙：相对 2026-09-25 的 v2 qfq 快照，非 bj 侧 **435 笔旧事件在今日 qfq 与 hfq 下都不成立**（旧 adj 表因子跳变制造的伪事件，603125 为例），**143 笔新事件在原始价下全部 ≥2×**（旧 qfq 表压制的真事件）。stage1 在新宇宙重训的纯 OOS 指标与旧宇宙同量级（Spearman **0.2527** vs 0.2533），跨宇宙互评的差在自助法 95% 区间内全部跨 0 ⇒ **不足以支持切线上模型**。

## 1. 口径与实现

`scripts/find_double_up_hfq.py` 逐项复刻 `find_double_up_c_v2.py` 的筛选参数，唯一差别是价格取 hfq：

| 项 | 值（与 v2 相同，除注明外） |
|---|---|
| 起点 / 窗口 / 阈值 | `2020-01-01` / 20 个自然行 / `max(close[i+1:i+21]) / close[i] >= 2.0` |
| 峰值 | `np.argmax` 取首个峰值；窗口含 NaN 时 `np.max` 传播 NaN，事件不成立 |
| 短序列 | `len < WINDOW + 5` 整只跳过 |
| 涨停计数 | 用 **raw** 日收益、阈值 `limit - 0.001`；`limit` 来自 `get_limit` |
| `is_new` | 沿用 v2 语义：2020+ 切片首日起不足 250 个交易日 |
| `year` / `window_amount` | 峰值年份 / raw amount 窗口和 |
| **close** | `tdxfeed.adjusted.frame(code, kind="hfq", strict=False)`；`factor_cov < 0.999` 整只跳过 |
| 异常剔除 | 只剔除窗口 `(起点, 起点+20]` 内跨 `econ > 0` 的 suspect 上跳事件；起点当日跳变不影响 `close[j]/close[i]`，不剔除；下跳只记录不剔除 |

- 守卫 `tests/test_find_double_up_hfq.py`（9 条）：唯一 2× 窗口、跨 suspect 上跳剔除、起点当日不剔除、`_suspect_up_dates` 只留 `econ>0`、短序列/2020 前/跨年起点、涨停计数用 raw。
- `suspect` 定义在 `tdxfeed/adjusted.py::factor_jumps`：`econ = (1+raw_ret) × (1+factor_chg) − 1`，`suspect = |econ| > limit + JUMP_RET_TOL(2%)`。

## 2. 数据前提

- 遍历 `daily_files()` 共 5,236 只：`ok=5,221`、`no_factor=15`、`error=0`，全量 **74 秒**。
- 15 只无因子（不产生事件、也不进样本）：5 只指数同码错配（`sh000001/000016/000688/000852/000905`，因子表 `raw_close` 与日线中位偏差 98.5%–99.9%）、`sh000300` / `sh600001` 缺 hfq 因子文件、`sz001246/301569/301660/301686/301716/301718/399001/399006` 缺因子文件。
- 剔除 20 笔 / 1 只：`sz302132` 中航成飞，窗口跨 2025-02-18 的 suspect 上跳（`econ +462.5%`，pct_change 444.7–500.6%）。清单：`data/double_up/double_up_hfq_excluded_2020_2026.csv`。

## 3. 与旧 v2 快照的差异（决定性证据）

| 集合 | 行数 | 代码数 | bj 行 |
|---|---|---|---|
| 旧 v2（2026-09-25 生成） | 22,305 | — | 4,219 |
| └ 非 bj | 18,086 | 1,672 | 0 |
| 新 hfq | 17,794 | 1,621 | 0 |

按 `(code, start_date)` 对齐（非 bj 侧）：`only_old=435`、`only_new=143`、`overlap=17,651`（17,651 + 435 = 18,086；17,651 + 143 = 17,794）。

- **bj 4,219 条不可复现**：`data/parquet/daily` 没有任何 bj 日线（`daily_files()` 只有 sh/sz），v2 生成时用的是当时的 bj 数据通道。上一轮 /tmp 探针用 `daily_files()` 扫描，才得到「bj 消失」的错觉——**不是 adj 修订**。这些 bj 事件既不可复现也不可标注。
- **only_old 435 笔**：435/435 在原始价下 20 日最大涨幅 **< 2×**，且 435/435 的筛选窗口内 hfq 因子无变化（无未知、无缺因子）。即这些事件在今日任何一条复权线上都不成立 ⇒ 旧快照的 ≥2× 来自**旧 adj 表的因子跳变**，不是 hfq 口径丢事件。
- 例证 `sh603125`：v2 记 2023-05-24 起点、2023-05-25 峰值、`pct_change=116.1%`；原始收盘 32.88 → 33.17（+0.9%），今日 hfq 与今日 qfq 因子在该窗口都是常数（hfq 1.0 / qfq 0.466815），两条线都算不出翻倍。旧 adj 表在该日有跳变。
- **only_new 143 笔**：143/143 在原始价下 ≥2×，是旧 qfq 表压制到 <2× 的真事件。
- 旧 v2 是 2026-09-25 的快照，此后 adj 表在 10-29/10-30 被修订，所以「重跑今日 qfq」也不会复现旧快照；本次直接以 hfq 为准，不与旧 qfq 数值纠缠。

## 4. 样本链与宽表

```
detail 17,794
  → find_d_signal_5d.py（7 日间隔去重后正样本 4,964；特征可算 4,813）
  → 负采样 4,964（seed 42；特征可算 4,962）
  → 18 维特征表 9,775
  → recompute_pnl_labels.py --init --price-basis hfq（9,775 全部非空）
  → stage1_wide_table.py --price-basis hfq（9,775 行）
  → stage1_retrain.py（21 维契约 + 旧 hfq 超参；118 棵树）
```

- `data/double_up/d_signal_5d_features_universe_hfq.csv`：9,775 行（正 4,813 / 负 4,962），18 维。
- `data/double_up/d_signal_5d_features_vpnl_universe_hfq.csv`：9,775 行，`hypothetical_pnl` 非空 **9,775**（闸门 `min_valid = int(rows_with_daily × 0.95)` 通过，`RECOMPUTE_DONE rows=9775 valid=9775 basis=hfq`）。
- `data/double_up/stage1_wide_table_universe_hfq.parquet`：9,775 行 × 39 列；缺失文件 0、缺失日期 0、计算失败 0、缺因子跳过 0、非有限单元 0、特征漂移 0。`price_basis != raw` 时 CSV-parity 强制关闭，打印的 `parity_max=0` **不是校验证据**（与上一轮 hfq 宽表同一约定）。
- 与旧链的结构差异：新宇宙全部来自 `daily_files()`，没有 bj/ETF 行，**不存在旧标签表 13,064 行里 3,083 行 null 的问题**；非空分母就是 9,775。

## 5. 指标

### 5.1 同宇宙纯 OOS（仅 <2025-01-01 训练，≥2025-01-01 测试）

| 宇宙 | 宽表行 | OOS 行 | Spearman | Top10 | Top20 | Top50 | 树数 |
|---|---|---|---|---|---|---|---|
| 旧宇宙 hfq 臂（上一轮） | 9,979 | 2,695 | 0.2533 | 0.3285 | 0.2339 | 0.2347 | 113 |
| 旧宇宙 raw 臂（上下文） | 9,981 | 2,695 | 0.2485 | 0.2886 | 0.2571 | 0.2154 | 123 |
| **新宇宙 hfq** | 9,775 | 2,748 | **0.2527** | 0.2926 | **0.2423** | 0.2172 | 118 |

新模型：`data/double_up/d_signal_5d_model_vpnl_huber_uhfq_20261030.txt`（476,713 B）；
新契约：`data/double_up/stage1_selected_features_uhfq_20261030.json`（21 维，与旧 hfq 契约逐项相同）；
报告：`logs/stage1_retrain_20261030_212944.{md,json,log}`。

> Top10/Top20/Top50 是**预测分最高的 10/20/50 行**的均值 PnL（`_eval_fold` 的绝对行数口径），不是百分比分位。两个测试集上旧模型的 Top10 逐位相同（0.32853017601927537）是因为这 10 行在两个宇宙都入选且重叠行标签一致，已逐行核验。

### 5.2 跨宇宙互评（两模型 × 两 OOS 集）

同一协议：每个宇宙各训一个「仅 <2025」的模型（早停在 ≥2025 选轮，超参 = 旧 hfq 契约），再分别到两个宇宙的 OOS 行上打分。指标复用 `stage1_baseline._eval_fold`。

| 模型 \ 测试集 | 旧宇宙 OOS（n=2,695） | 新宇宙 OOS（n=2,748） |
|---|---|---|
| 旧宇宙模型 | rho 0.2533 / T10 0.3285 / T20 0.2339 / T50 0.2347 | rho 0.2539 / T10 0.3285 / T20 0.2660 / T50 0.2377 |
| 新宇宙模型 | rho 0.2433 / T10 0.2898 / T20 0.2592 / T50 0.2393 | rho 0.2527 / T10 0.2926 / T20 0.2423 / T50 0.2172 |

配对自助法（1,000 次，行重采样）下「新 − 旧」的 95% 区间：

| 测试集 | Δrho | ΔTop10 | ΔTop20 | ΔTop50 |
|---|---|---|---|---|
| 旧宇宙 OOS | −0.0100 [−0.0268, +0.0076] | −0.0388 [−0.1396, +0.0492] | +0.0252 [−0.0662, +0.0846] | +0.0046 [−0.0441, +0.0508] |
| 新宇宙 OOS | −0.0012 [−0.0174, +0.0142] | −0.0359 [−0.1257, +0.0486] | −0.0237 [−0.0973, +0.0641] | −0.0205 [−0.0548, +0.0437] |

- 所有差均跨 0；新宇宙没有带来可判定的改善或退化。
- 两个宇宙的正样本 `(code, signal_date)` 重叠 4,700 对（旧 5,025 唯一对 / 新 4,813 行），负样本只有 5 对偶然重合——负采样以 seed 42 但正样本数变了，整体重抽。因此「跨宇宙差」里混着负样本换批，不能当成纯宇宙效应。
- 结论与上一轮 stage1 双跑一致：单折 2,7xx 样本不足以判定优劣，**线上模型保持不动**。新宇宙模型与契约只作对照留档。

## 6. 影响面与文件

新增（脚本/守卫）：

- `scripts/find_double_up_hfq.py`、`tests/test_find_double_up_hfq.py`
- `tdxfeed/adjusted.py::factor_jumps`（`JUMP_RET_TOL` / `JUMP_COLS`）、`tests/test_adjusted_factor_jumps.py`

新增（产物，均在 gitignore 的 `data/` / `logs/` 下）：

- `data/double_up/double_up_hfq_2020_2026_detail.csv`（17,794 行）
- `data/double_up/double_up_hfq_excluded_2020_2026.csv`（20 行）
- `data/double_up/d_signal_5d_features_universe_hfq.csv` / `..._vpnl_universe_hfq.csv`（各 9,775 行）
- `data/double_up/stage1_wide_table_universe_hfq.parquet`（9,775 × 39）
- `data/double_up/d_signal_5d_model_vpnl_huber_uhfq_20261030.txt`、`stage1_selected_features_uhfq_20261030.json`
- `data/double_up/stage1_cross_eval_uhfq_20261030.json`（互评矩阵 + 重叠计数）
- `logs/stage1_retrain_20261030_212944.{md,json,log}`

修改（消费者/入口）：

- `scripts/find_d_signal_5d.py`：`--universe` / `--out-features` / `--price-basis {raw,hfq}` / `--skip-train`；训练与报告仍用默认路径，只有显式传参才走新宇宙。
- `scripts/recompute_pnl_labels.py`：新增 `--init`（无 `hypothetical_pnl` 列时初始化，写前闸门用 `rows_with_daily × 0.95`）。
- `scripts/factor_engine.py`：`DOUBLE_UP` 切到新 hfq CSV；文档同步。
- `scripts/plan_e_v3_live_track.py` / `scripts/plan_e_v3_early_backtest.py`：删除未使用的 `DOUBLE_UP_FP` 死常量（全仓库无消费者）。
- `AGENTS.md`：数据口径更新（宇宙筛选已可选 hfq；bj 不可复现的原因）。

## 7. 复现命令

```bash
# 1) 新宇宙（74s / 5,236 只）
venv/bin/python scripts/find_double_up_hfq.py

# 2) 18 维样本链（约 103s）
venv/bin/python scripts/find_d_signal_5d.py \
  --universe data/double_up/double_up_hfq_2020_2026_detail.csv \
  --out-features data/double_up/d_signal_5d_features_universe_hfq.csv \
  --price-basis hfq --skip-train

# 3) hfq 标签（9,775 行全部非空）
venv/bin/python scripts/recompute_pnl_labels.py --init --price-basis hfq \
  --src data/double_up/d_signal_5d_features_universe_hfq.csv \
  --out data/double_up/d_signal_5d_features_vpnl_universe_hfq.csv

# 4) 宽表（9,775 行）
venv/bin/python -m tdxfeed.stage1_wide_table \
  --src data/double_up/d_signal_5d_features_vpnl_universe_hfq.csv \
  --out data/double_up/stage1_wide_table_universe_hfq.parquet \
  --sample 0 --no-parity --price-basis hfq

# 5) 重训（21 维契约 + 旧 hfq 超参）
venv/bin/python scripts/stage1_retrain.py \
  --wide-table data/double_up/stage1_wide_table_universe_hfq.parquet \
  --out-model data/double_up/d_signal_5d_model_vpnl_huber_uhfq_20261030.txt \
  --frozen data/double_up/stage1_selected_features_uhfq_20261030.json \
  --config data/double_up/stage1_selected_features_hfq_20261030.json \
  --no-backup-old --no-protect-old
```

## 8. 残余与后续

- **bj 不可覆盖**：`data/parquet/daily` 无 bj 日线，宇宙无法含北交所；旧 v2 的 4,219 条 bj 事件不可复现也不可标注。要恢复需先补 bj 日线，再重跑筛选与样本链。
- **hfq 源侧上跳坏点 3 起**（`factor_jumps` 已能识别）：`sz302132` 2025-02-18（`econ +462.5%`，已在宇宙筛选中剔除 20 笔）、`sz003038` 2022-03-30（`+32.6%`）、`sz000981` 2022-02-28（`+58.3%`；另有 2011-08-26 一次，早于宇宙起点）。现有守卫 `tests/test_hfq_factor_monotone.py` 只白名单 12 次**下跳**，上跳没有白名单、也没有修表（本地忠实复刻源侧数据）。
- **规格 49/27 未能复现**：按「日收益差」扫 2020+，>0.5% 有 97 起、>1% 77、>2% 52、≥3% 42（其中 37 起负向）。阈值与翻倍宇宙过滤都未能对齐 49/27，pre-2020 无事件。只能标注「口径未复现」，不能据此声称规格数字错误。
- **ST 带未建模**：筛选沿用 v2 的名称/`limit` ST 判断，未接入 `get_limit` 的按交易日 ST 分段。
- **v9/proxy 中间特征链未重建**：18/26 维中间表只是链路留档；宽表只消费 `code/signal_date/is_positive/hypothetical_pnl` 四个元数据列，口径落点在 `recompute_pnl_labels --price-basis hfq` 与 `stage1_wide_table --price-basis hfq`，两者已按新宇宙重建。
- **factor_engine 下游待重跑**：`event_factors*.parquet` 仍是旧宇宙产物；`factor_engine` 全市场约 25–35 分钟，本轮未跑。v3 回测/跟踪读的是已存在的因子产物，数字暂不变。
- **新宇宙 CSV 无定时生产者**：`double_up_hfq_2020_2026_detail.csv` 与旧 v2 快照一样是一次性产物，没有 timer 或 `ExecStartPost` 刷新；`factor_engine` 切它之后，下游拿到的是 2026-10-30 的快照。要日更需另立调度，并与每日 18:30 的 v1 raw 宇宙（`find_double_up_c.py`，喂 plan_e）划分职责。
- **负样本未对齐**：跨宇宙指标差混着负样本换批（仅 5 对重合），不是纯口径效应。
- **线上模型不切**：`DEFAULT_MODEL` / 注册表不动；新宇宙模型仅作对照。
