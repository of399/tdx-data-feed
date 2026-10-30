# hfq 读侧权威双跑（2026-10-30）

## 0. 结论一句话

口径效应在 daban 上小于可观测精度（两组 OOS AUC 在 4 位小数下同为 0.7606，但模型 md5 与 gain 分布不同），在 stage1 上方向不一致（纯 OOS Spearman +0.0048、Top-10 +0.0399、Top-50 +0.0193，但 Top-20 −0.0232），**不足以支持切流——本轮线上模型不切**，两组模型仅作对照留档。

## 1. 数据前提

- 回填覆盖（`data/hfq_backfill_coverage_20261030.json`，2026-10-30 04:27）：`total=5236`，`by_state = {ok: 5221, mismatch: 5, no-factor: 10}`，`head-lag=0`，`tail-lag=0`。
  - `mismatch`（因子表 `raw_close` 与日线 `close` 中位偏差 >1%，同码双文件）：`sh000001`（99.65%）、`sh000016`（99.83%，另有 tail_lag 5）、`sh000688`（98.47%）、`sh000852`（99.90%）、`sh000905`（99.87%）——全部是**指数**代码撞上同码个股因子表。
  - `no-factor`：`sh000300`、`sz399001`、`sz399006`、`sh600001`、`sz001246`、`sz301569`、`sz301660`、`sz301686`、`sz301716`、`sz301718`。
- 回填执行：`ok=5217 not_in_source=0 fail=0`，状态机 `done=5221 / rejected=0 / rebased=0`；抽样 12 只 `factor_first` 均在 1999 年、`head_lag=0`、`tail_lag=0`、`cov=1.0000`。
- 下跳白名单：**11 只 / 12 次**（2020 年起），守卫 `tests/test_hfq_factor_monotone.py` 绿（1 passed）。2020 年以前另有 30 次下跳，仅记录在覆盖 JSON 的 `pre2020_jumps`，不进白名单。

## 2. daban（干净消融）

`--price-basis` 在本脚本里只切换**特征**：目标仍是「次日是否涨停」，判定始终用 raw 收盘（`scripts/train_daban_model.py:125`、`:151`），因此这一组是纯特征口径消融，符合「涨跌停留 raw」的拍板。

| 组 | OOS AUC | best_iter | 样本（正/负） | train / test |
|---|---:|---:|---:|---:|
| raw（对照） | 0.7606 | 500 | 216361 / 1082735 | 1171124 / 127972 |
| hfq（实验） | 0.7606 | 500 | 216361 / 1082735 | 1171124 / 127972 |

模板里的「Top-5 / Top-20 命中」本脚本不产出（`train_daban_model.py` 只输出 OOS AUC 与 gain 重要性），表列改为 `best_iter` 与样本数，不另算新指标。

- 非口径漂移 = raw AUC − 0.7478 = **+0.0128**。这个差不是噪声：现役模型训练于 2026-10-19，样本 1071227 / 113892，本轮两组共 1299096 条（+113977），漂移里混着宇宙扩张与日线修订。
- 口径效应 = hfq AUC − raw AUC = **0.0000**（日志只保留 4 位小数）。两组模型并非同一个：md5 `2ff3825ebdcd764c04537861bdf15111`（hfq）对 `ca4d9fb4c9cd777d80a7f8c4775793b5`（raw），gain 数值与排名均可区分。
- 13 个 hfq 维的 gain 排名变化（全 29 维导出后计算，delta = raw 排名 − hfq 排名，正数表示 hfq 组更高）：
  - 升：`ret_5d` **+3**（17→14）、`close_pos_5d` **+2**（19→17）、`ret_3d` +1、`max_5d_pct` +1、`range_efficiency` +1；
  - 降：`pct_t1` −1（7→6）、`acceleration` −1（16→15）；
  - 不变：`volatility_5d`、`dist_to_5d_low`、`dist_to_5d_high`、`max_drawdown_5d`、`vol_norm_ret`、`up_down_ratio`。
  - 前 4 名两组一致（`volatility_5d` / `dist_to_5d_low` / `is_limit_up_today` / `dist_to_5d_high`）。最大位移只有 3 位，没有任何一维跨过前 5 门槛。
- 样本集对齐证据：两组正/负样本数**逐字相同**（216361 / 1082735）。hfq 组把 7 只覆盖不足的标的写进 `data/double_up/daban_hfq_skipped_20261030.json`，raw 组用同一 JSON 作 `--exclude-symbols-file`；跳过发生在 RNG 抽样之前，两组负样本抽样流因此同源。7 只为 `sh000001`、`sh000016`、`sh000300`、`sh000688`、`sh000852`、`sh000905`、`sh600001`（5 只同码指数 + 2 只缺因子）。hfq 日志里这 5 只指数的告警形态是「拒绝乘因子，按 raw 返回」，随后被训练脚本整只跳过——两道闸门都生效了。

## 3. stage1（同 9981 样本、同 21 维契约）

脚本的实际指标是**纯 OOS（≥2025-01-01）Spearman 与 Top-N 组合收益**，不产出 fold1 AUC / MSE（模板列名沿用计划时的假设，此处按实测改名）。样本 9,981 / 9,979，纯 OOS 测试集两臂均为 2,695 条。

| 组 | 纯 OOS Spearman | 纯 OOS Top20 PnL | 树数 | 样本 |
|---|---:|---:|---:|---:|
| 10-28 干净重训（基线） | 0.248268 | 0.278062 | 117 | 9981 |
| raw（对照，2026-10-30） | 0.248499 | 0.257139 | 123 | 9981 |
| hfq（实验，2026-10-30） | 0.253298 | 0.233921 | 113 | 9979 |

- 非口径漂移 = raw − 基线 = **+0.000232**（Spearman）/ **−0.020923**（Top20）。
  - 来源已拆开：raw 宽表是用新代码从同一份 CSV 重建的，与 10-28 原版逐单元比对（按 (code, signal_date) 排序后逐行对齐，**不用 merge**——两表各有 3 个重复键（一个键占 32 行，合计多出 93 行），merge 会叉积出 12957 行，正是 AGENTS.md 记过的坑），仅 **15 行 / 41 个单元**差 >1e-9；集中在 `turnover_proxy`、`avg_amount_5d`、`amt_per_price`（成交额估算修订后的派生量，`data/.state/amount_estimated.json` 里 sh600000 / sz300339 / sz300401 等均有条目）与 `sz300401 @ 2022-08-12` 的整组价格类派生量（该票日线 mtime 2026-10-30 01:21，晚于 10-28 宽表）。树数 117→123 是这些修订后的早停变化。
- 口径效应 = hfq − raw = **+0.004798**（Spearman）/ **−0.023218**（Top20）/ **+0.039896**（Top10）/ **+0.019272**（Top50）。
  - 注意这不是纯特征消融：stage1 的目标 `hypothetical_pnl` 本身按 hfq 重算（9979 行有效），hfq 臂同时换了价格类特征与标签，21 维契约不变。
  - hfq 臂少 2 行（`sh600001` 的 2006-11-02、2005-11-29，缺因子被整只跳过），两行都在 2025 年前，纯 OOS 测试集不受影响，两臂 n=2695 相同。

## 4. 影响面

- 12 次异常日（因子下跳）命中的训练样本数：**全部为 0**。窗口口径：特征窗口按 `anchor = sig_idx-1` 的 5 日 `[sig-5, sig-1]`，持有窗口按入场 `sig_idx+1` 起 10 根，均按该票自己的交易日索引数、停牌日不计。

| 代码 | 下跳日 | 幅度 | 特征窗口命中 | 持有窗口命中 |
|---|---|---:|---:|---:|
| 000001 | 2020-12-31 | −16.816% | 0 | 0 |
| 000002 | 2020-11-19 | −2.556% | 0 | 0 |
| 000002 | 2025-01-09 | −1.179% | 0 | 0 |
| 000517 | 2020-11-17 | −1.036% | 0 | 0 |
| 600372 | 2020-11-19 | −0.388% | 0 | 0 |
| 600654 | 2020-11-25 | −13.839% | 0 | 0 |
| 600655 | 2020-11-25 | −9.115% | 0 | 0 |
| 600681 | 2020-11-25 | −3.256% | 0 | 0 |
| 600727 | 2020-11-25 | −0.081% | 0 | 0 |
| 600771 | 2020-11-25 | −0.101% | 0 | 0 |
| 600817 | 2020-11-25 | −0.272% | 0 | 0 |
| 601607 | 2020-11-27 | −0.778% | 0 | 0 |

- 缺因子被排除的代码清单：
  - daban：7 只（见 §2，`daban_hfq_skipped_20261030.json`），两组样本数未因此分叉。
  - stage1：`n_hfq_skip=2`（`sh600001` 两行），宽表 `rows=9979`、缺失文件 3083、计算失败 0、非有限单元 0。
- raw 宽表 `parity_max=3.626e+09` 的单点来源已定位：`sz300339 @ 2024-12-06` 的 `avg_amount_5d` 在 CSV 里是 `0.0`（同行的 `turnover_proxy`、`amt_per_price` 也是 0），重算值约 3.6e9。这是 CSV 脏行，非本轮引入，也不影响 hfq 侧。

## 5. 已知残余

- **样本宇宙仍用 qfq 翻倍筛选**（规格记为约 49 个伪事件，本轮未复核），本轮不动；改用 hfq 重筛宇宙需要新一轮对照，不能与本次结果直接横比。
- **12 次因子下跳不修**，白名单守卫 `tests/test_hfq_factor_monotone.py` 钉住；2020 年以前的 30 次只在覆盖 JSON 里记录。
- **hfq 宽表的 CSV-parity 检查是关闭的**：`tdxfeed/stage1_wide_table.py` 在 `price_basis != "raw"` 时强制 `verify_parity=False`（CSV 存的是 raw 特征），所以 hfq 宽表打印的 `parity_max=0.000e+00` **不是**校验证据，只是没比。hfq 侧的正确性依赖 §4 的窗口统计与 `adjusted.frame` 的 attrs 门禁。
- 5 个同码指数在 hfq 侧无可用因子（`mismatch`），daban 一律跳过。指数本不该进入个股模型，这是口径边界而非缺陷。
- stage1 的 hfq 臂同时动了特征与标签，Top-20 与 Spearman 方向相反，单折 2,695 样本，不足以判定优劣；只换一侧的两组已于同日补做，归因见 §7——Top20 的总负效应主要来自 hfq 标签，hfq 特征对 Top10 有稳定正贡献。
- 线上模型未切换：`scripts/predict.py` 的注册表只新增两条 hfq 条目，默认模型不变。

## 6. 产物清单

| 产物 | 路径 | mtime | 备注 |
|---|---|---|---|
| hfq 因子表 | `data/hfq_factor/*.parquet` | 回填完成 2026-10-30 04:25 | 5221 只、日线全段覆盖 |
| 覆盖审计 | `data/hfq_backfill_coverage_20261030.json` | 10-30 04:27 | 5.2 KB |
| hfq 标签 CSV | `data/double_up/d_signal_5d_features_vpnl_hfq.csv` | 10-30 04:41 | 13064 行 / 9979 有效 |
| raw 标签 CSV 备份 | `data/double_up/_pre_hfq_20261030/d_signal_5d_features_vpnl_raw_20261030.csv` | 10-28 21:22 | 重算前快照 |
| raw 宽表 | `data/double_up/stage1_wide_table.parquet` | 10-30 02:55 | 9981 行 |
| raw 宽表原版备份 | `data/double_up/stage1_wide_table.bak-20261030.parquet` | 10-28 21:24 | 10-28 产物 |
| hfq 宽表 | `data/double_up/stage1_wide_table_hfq.parquet` | 10-30 04:42 | 9979 行 |
| stage1 raw 模型 | `data/double_up/d_signal_5d_model_vpnl_huber_stage1_raw_20261030.txt` | 10-30 02:59 | md5 `257df281695bbace927e62fd2977eb13` |
| stage1 hfq 模型 | `data/double_up/d_signal_5d_model_vpnl_huber_stage1_hfq_20261030.txt` | 10-30 04:42 | md5 `bd6fe71cad723c3bbddabf45c3918d67` |
| stage1 raw 契约 | `data/double_up/stage1_selected_features_raw_20261030.json` | 10-30 02:59 | 21 维 |
| stage1 hfq 契约 | `data/double_up/stage1_selected_features_hfq_20261030.json` | 10-30 04:42 | 21 维 |
| daban hfq 模型 | `data/double_up/d_signal_5d_model_daban_hfq_20261030.txt` | 10-30 05:16 | md5 `2ff3825ebdcd764c04537861bdf15111` |
| daban raw 模型 | `data/double_up/d_signal_5d_model_daban_raw_20261030.txt` | 10-30 05:46 | md5 `ca4d9fb4c9cd777d80a7f8c4775793b5` |
| daban 跳过清单 | `data/double_up/daban_hfq_skipped_20261030.json` | 10-30 05:15 | 7 只 |
| daban 报告 | `data/double_up/daban_train_report_{hfq,raw}_20261030.md` | 05:16 / 05:46 | AUC 0.7606 |
| 宽表日志 | `logs/stage1_wide_{raw,hfq}_20261030.log` | 02:55 / 04:42 | parity 与跳过计数 |
| stage1 报告 | `logs/stage1_retrain_20261030_{025905,044251}.{md,json}` | 02:59 / 04:42 | 纯 OOS 全精度 |
| daban 日志 | `logs/daban_train_{hfq,raw}_20261030.log` | 05:16 / 05:46 | 样本数与 AUC |

现役产物未被覆盖：`d_signal_5d_model_daban.txt`（10-19 17:30）、`d_signal_5d_model_vpnl_huber_stage1.txt`（10-28 21:29）、`stage1_selected_features.json`（10-10 23:45）、`d_signal_5d_features_vpnl.csv`（10-28 21:22）。

复现命令：

```bash
# 标签
venv/bin/python scripts/recompute_pnl_labels.py --price-basis hfq --expect-drop 2
# 宽表
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 \
  --src data/double_up/d_signal_5d_features_vpnl_hfq.csv \
  --out data/double_up/stage1_wide_table_hfq.parquet --price-basis hfq
# stage1（两组）
venv/bin/python scripts/stage1_retrain.py --wide-table data/double_up/stage1_wide_table.parquet \
  --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1_raw_20261030.txt \
  --frozen data/double_up/stage1_selected_features_raw_20261030.json \
  --config data/double_up/stage1_selected_features.json --no-backup-old --no-protect-old
venv/bin/python scripts/stage1_retrain.py --wide-table data/double_up/stage1_wide_table_hfq.parquet \
  --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1_hfq_20261030.txt \
  --frozen data/double_up/stage1_selected_features_hfq_20261030.json \
  --config data/double_up/stage1_selected_features.json --no-backup-old --no-protect-old
# daban（hfq 先写跳过清单，raw 再按同一清单排除）
venv/bin/python scripts/train_daban_model.py --price-basis hfq --tag hfq_20261030 \
  --hfq-skipped-out data/double_up/daban_hfq_skipped_20261030.json
venv/bin/python scripts/train_daban_model.py --price-basis raw --tag raw_20261030 \
  --exclude-symbols-file data/double_up/daban_hfq_skipped_20261030.json
```

## 7. 特征效应与标签效应分离（2×2，同日追加）

§5 遗留项要求「只换一侧」的第三组。实际补做了两组（C/D），与原 A/B 凑成完整 2×2：

| 组 | 价格特征 | 标签 | 宽表 |
|---|---|---|---|
| A | raw | raw | `stage1_wide_table.parquet` |
| B | hfq | hfq | `stage1_wide_table_hfq.parquet` |
| C | hfq | raw | `stage1_wide_table_hfqfeat_rawlabel.parquet` |
| D | raw | hfq | `stage1_wide_table_rawfeat_hfqlabel.parquet` |

设计成立的前提是 CLI 边界正交：`--src` 只决定标签（`base`），`--price-basis` 只决定特征（`frame_hfq`）。逐值核对结果：

- A vs D：37 个共有列里**只有 `hypothetical_pnl` 不同**（5,514 行），36 个特征列逐值相同；
- B vs C：同样只有标签列不同（5,512 行）；
- 样本数：A 9,981，其余三组 9,979。差的是 `sh600001` 两行（hfq 侧缺因子整只跳过；D 的 hfq 标签在这两行也是 NaN，训练期同样剔除到 9,979）。所以 **B−D、B−C 是严格同样本单差**，C−A、D−A 差 2 行（0.02%），可横比但不能称「完全同样本」。

| 组 | 树数 | 样本 | 纯 OOS Spearman | Top10 | Top20 | Top50 | Top20 胜率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| A raw/raw | 123 | 9,981 | 0.248499 | 0.2886 | 0.2571 | 0.2154 | 0.85 |
| B hfq/hfq | 113 | 9,979 | 0.253298 | 0.3285 | 0.2339 | 0.2347 | 0.80 |
| C hfq/raw | 118 | 9,979 | 0.254885 | 0.3326 | 0.2761 | 0.2387 | 0.90 |
| D raw/hfq | 123 | 9,979 | 0.253744 | 0.2853 | 0.2516 | 0.2143 | 0.85 |

2×2 的四点正好用完自由度，加性分解的交互项恒为 0，所以不单列交互；两个方向上单差是否同号就是交互的直观读法。

| 指标 | 特征效应（raw 标签）C−A | 特征效应（hfq 标签）B−D | 标签效应（raw 特征）D−A | 标签效应（hfq 特征）B−C | 总 B−A |
|---|---:|---:|---:|---:|---:|
| Spearman | +0.006385 | −0.000446 | +0.005244 | −0.001587 | +0.004799 |
| Top10 | +0.043946 | +0.043221 | −0.003325 | −0.004050 | +0.039896 |
| Top20 | +0.018929 | −0.017709 | −0.005509 | −0.042147 | −0.023218 |
| Top50 | +0.023326 | +0.020345 | −0.001073 | −0.004054 | +0.019272 |

读法：

- **Top10 的特征效应是唯一在两种标签下几乎同值的项**（+0.0439 / +0.0432）：hfq 价格特征让头部 10 只的组合收益提升约 4.4 个百分点，这是本轮口径改造最扎实的收益。
- **Top20 的总负效应主要来自标签**：同样本、只换标签的 B−C = −0.0421；特征效应在 Top20 上两种标签下方向相反（+0.0189 / −0.0177），说明这个切片对具体配对敏感，A→B 的单差不能归因给特征。§5 的「Top-20 与 Spearman 方向相反」由此拆开：特征是正贡献（Top10 / Top50 稳健），标签是负贡献（Top20 最明显）。
- **标签效应在排序质量上方向不一致**（Spearman：raw 特征下 +0.0052、hfq 特征下 −0.0016），在 Top-N PnL 上偏负。hfq 标签没有表现出收益改善，也没有可信证据表明它更差。
- 统计力：纯 OOS 单折 2,695 条，Top10 只有 10 个点、Top20 20 个点，PnL 均值噪声大；上述差异是方向性证据，不是显著性结论。四个模型 md5 两两不同（`257df281…` / `bd6fe71c…` / `70915f63…` / `7524ee33…`），树数 113–123，不存在「训练没生效」。

产物：

| 产物 | 路径 |
|---|---|
| C 宽表 | `data/double_up/stage1_wide_table_hfqfeat_rawlabel.parquet`（9,979 行） |
| D 宽表 | `data/double_up/stage1_wide_table_rawfeat_hfqlabel.parquet`（9,981 行，训练期剔除 2 行） |
| C 模型 / 契约 | `d_signal_5d_model_vpnl_huber_stage1_hfqfeat_rawlabel_20261030.txt`（md5 `70915f63…`）/ `stage1_selected_features_hfqfeat_rawlabel_20261030.json` |
| D 模型 / 契约 | `d_signal_5d_model_vpnl_huber_stage1_rawfeat_hfqlabel_20261030.txt`（md5 `7524ee33…`）/ `stage1_selected_features_rawfeat_hfqlabel_20261030.json` |
| 训练日志与报告 | `logs/stage1_retrain_20261030_hfqfeat_rawlabel.log` + `logs/stage1_retrain_20261030_150720.{md,json}`（C）；`logs/stage1_retrain_20261030_rawfeat_hfqlabel.log` + `logs/stage1_retrain_20261030_150642.{md,json}`（D） |
| 宽表构建日志 | `logs/stage1_wide_hfqfeat_rawlabel_20261030.log`、`logs/stage1_wide_rawfeat_hfqlabel_20261030.log` |

复现命令（宽表 + 训练；训练模板见 §6，换宽表与输出名）：

```bash
# C：hfq 特征 + raw 标签
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 \
  --src data/double_up/d_signal_5d_features_vpnl.csv \
  --out data/double_up/stage1_wide_table_hfqfeat_rawlabel.parquet --price-basis hfq
# D：raw 特征 + hfq 标签
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 \
  --src data/double_up/d_signal_5d_features_vpnl_hfq.csv \
  --out data/double_up/stage1_wide_table_rawfeat_hfqlabel.parquet --price-basis raw
```

两组宽表可并行构建，但训练不要并行：`stage1_retrain.py` 的报告/JSON 文件名按 `%Y%m%d_%H%M%S` 取，同秒启动会互相覆盖报告（本轮踩过一次，C 组串行重跑补回，模型 md5 不变，训练确定性已验证）。
