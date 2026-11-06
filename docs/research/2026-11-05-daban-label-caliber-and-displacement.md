# daban 标签口径审计与位移数（2026-11-05）

> 一次性研究快照，记录这一轮的判据与数字；不是现状规格。
> 结论：**2026-10-28 的标签口径变更不触及 daban，「重训到新口径」这一前提不成立；现役模型不切。**
> 现役 29 维分类模型与 10-30 那只同契约重训版，在 1,729,388 行（及重扫后 1,781,464 行）同一 OOS
> 行集上的 Top-5 平均位移 **1.14–1.24/5 只**，配对收益差 **95% 置信区间全部跨 0**。

## 1. 为什么不需要重训（裁定证据）

`AGENTS.md`「训练标签口径」那条的主语是 `hypothetical_pnl`（stage1 系）。四项口径变化——入场次根起判定、
同根双触发结算亏损侧、跳空按开盘、成本走 `CostModel.round_trip` 乘法——**没有一项存在于 daban 的标签里**：

| 判据 | 实测 |
|---|---|
| daban 标签定义 | `scripts/train_daban_model.py:6` 与判定 `:162-163`：`ret_next = close[sig_idx+1]/cur_close - 1`，`is_limit_up_ret(ret_next, limit_val)` → 次日收盘涨停布尔值，无出入场阶梯、无成本 |
| 是否引用内核 A / 成本 | `grep -E 'CostModel\|round_trip\|execution\|settle\|compute_hypothetical_pnl\|train_pnl_proxy' scripts/train_daban_model.py` → **退出码 1（零命中）** |
| 10-28 是否改过该文件 | 唯一一笔是 `ec68d1f`（2026-10-28 23:47）`refactor(paths)`，diff 只有 2 行：`PARQUET = ROOT/"data"/"parquet"/"daily"` → `daily_dir()`，不含任何判定语句 |
| 标签行是否漂移过 | `git log -S 'is_limit_up_ret(ret_next' -- scripts/train_daban_model.py` 只有 `3f9c799`（2026-10-19「提取公共涨停函数」）一笔，此后未变 |
| 旧口径快照的归属 | `data/double_up/_pre_settle_kernel_20261028/` 里 4 个产物全是 vpnl/stage1（`d_signal_5d_features_vpnl.csv`、`..._vpnl_huber.txt`、`..._vpnl_huber_stage1.txt`、`stage1_wide_table.parquet`），**一个 daban 文件都没有** |
| 10-28 那轮的覆盖范围 | `docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md:1` 标题即「stage1 重训结论」；全文 daban 只出现在 `:303`（讲注册表回退），不是重训对象 |

旁证：`docs/archive/RERUN_REPORT_2026-10-19.md:69` 当时已把「L3 daban 全量重训」判为 🔴
「会覆盖生产模型……基线 AUC 0.7478 已验证，重复重训边际收益低」。

## 2. 10-30 那两只是什么（同契约、但不是「新口径」）

三只模型的 `feature_names=` 行**逐字节相同**（`diff` 退出码 0 × 2），29 维、`objective=binary sigmoid:1`：

| 产物 | mtime | md5 | 训练报告 |
|---|---|---|---|
| `d_signal_5d_model_daban.txt`（现役，18:05 在用） | 10-19 17:30:31 | `cc0696cabc6580cddc83d5039af909aa` | `daban_train_report.md` 同秒落盘：train 1,071,227 / test 113,892，`best_iter=492`，OOS AUC 0.7478 |
| `..._daban_hfq_20261030.txt` | 10-30 05:16:10 | `2ff3825ebdcd764c04537861bdf15111` | `daban_train_report_hfq_20261030.md`：train 1,171,124 / test 127,972，`best_iter=500`，AUC 0.7606 |
| `..._daban_raw_20261030.txt` | 10-30 05:46:15 | `ca4d9fb4c9cd777d80a7f8c4775793b5` | `daban_train_report_raw_20261030.md`：同上，AUC 0.7606 |

两只 10-30 的 md5 与 `docs/research/2026-10-30-hfq-read-authority-dual-run.md:99-100` 记录的逐字吻合（本轮复核）。

**那一轮量的是 hfq vs raw 特征口径，不是新旧口径。** 文档自己写明（`:17`）「`--price-basis` 在本脚本里只切换
**特征**：目标仍是「次日是否涨停」，判定始终用 raw 收盘」——读码成立：`build_dataset` 的 `df`（`:115`）始终是
raw 帧，`frame_hfq`（`:123-134`）只喂给 `compute_window_features_extended`（`:173-174`），
`ret_next`（`:162`）取的是 raw `close`。这与 stage1 不同——同一文档 `:48` 指出 stage1 的 hfq 臂
「同时换了价格类特征与标签」。

现役与 10-30 raw 之间的真实差别只有两条：数据更新（train 样本 +113,977，含宇宙扩张与日线修订，
`docs/research/2026-10-30-...:26` 记该漂移为 AUC +0.0128），以及 §5 的撞码排除。

## 3. 位移数实测

### 3.1 冻结评测集，两只模型可共用

`data/double_up/daban_pattern_eval_20261022.parquet`：1,729,388 行 / 45 列 / 336 交易日 /
`2025-06-03..2026-10-20` / 基准率 1.2492%，29 维**零缺失**（`missing_in_snapshot []`）。
`scripts/eval_daban_pattern.py` 头部即写明「明细落盘含全部 29 维模型特征 → 之后换模型可 `--eval-only` 直接重打分」。
快照全段 ≥ `SPLIT_DATE`（2025-06-01），而训练只取 `date < SPLIT_DATE`，所以对两只模型**都是** OOS，无泄漏。

### 3.2 数字（A = 现役 10-19，B = `..._raw_20261030`）

破平口径复刻 `scripts/report_daban_topk.py::_topk_stats`（`sort_values(["date","score"], ascending=[True,False], kind="mergesort")`
+ `groupby.cumcount() < K`）；配对差的自助法按**交易日整块重抽**（`n_boot=2000`、`seed=42`），
沿用 `docs/research/2026-11-04-stage1-union47-dual-run.md:83-85` 那条判据。

| 行集 | 位移（Top-5 平均改变） | 完全一致 | AUC A / B | Δ(A−B) Top5 命中 | 95% CI | p |
|---|---|---|---|---|---|---|
| 冻结快照 1,729,388 行 / 336 日 | **1.24/5**（位移率 24.9%） | 57/336 日 | 0.7743 / 0.7691 | −0.89pp（22.92% vs 23.81%） | [−2.26, +0.42]pp | 0.215 |
| 重扫至 11-03，1,781,464 行 / 346 日 | **1.14/5**（位移率 22.8%） | 66/346 日 | 0.7751 / 0.7713 | −0.35pp（22.89% vs 23.24%） | [−1.50, +0.92]pp | 0.649 |
| 同上，去掉 5 个撞码（§5） | 1.14/5 | 66/346 日 | 0.7750 / 0.7712 | −0.40pp | [−1.62, +0.87]pp | — |
| 仅新增段 2026-10-21..11-03（52,009 行 / 10 日） | 0.80/5 | 3/10 日 | 0.7555 / 0.7506 | **+2.00pp** | [+0.00, +6.00]pp | — |

重合分布（冻结快照 336 日，按重合只数）：`0 只:1 日 / 1 只:4 / 2 只:20 / 3 只:83 / 4 只:171 / 5 只:57`。
逐日命中优势：A 更多 46 日 / B 更多 55 日 / 平 235 日（10-22 快照）→ 47/51/248（重扫）。

`report_daban_topk.py` 的补充口径（主标签 `label`）：Top-5 命中 A 22.92%（lift 18.35x）/ B 23.81%（19.06x），
Top-10 20.03% / 20.21%；分年 Top-5 2025 25.34% vs 25.48%、2026 21.05% vs 22.53%。
对照组标签 `label_buyable`（次日涨停且开盘可买）：A 0.7687 / 16.13%，B 0.7633 / 15.42%——**方向随标签翻转**。

### 3.3 判定

排序上 A 略优（AUC +0.004~+0.005），钱上 B 略优（Top-5 +0.35~+0.89pp），**两个方向都给不出显著性**，
且方向随标签口径、随时间段翻转（新增 10 天里换成 A 占优）。样本只有 10 天的那一段 CI 下界恰好压在 0.00，
是薄样本的退化形状，**不得**读成「最新数据上现役显著更好」。
→ **现役 `d_signal_5d_model_daban.txt` 不切**；这与 union47 那轮的判法同形（证据不足，不是证伪）。

## 4. 判据更正：两条不该从 stage1 搬过来的 caveat

- **「事件样本日均仅 6 笔 ⇒ 逐日 IC 不适用」不适用于 daban。** 本快照日均 1,729,388/336 ≈ **5,147 行**，
  逐日横截面对 daban 可用。真正薄的是 **Top-5 本身（日均 5 笔）**，所以钱上的判据仍须按日整块自助。
- **9,981 vs 13,064 那个非空分母坑与 daban 无关**，那是 stage1 标签 CSV 的形状。daban 直接扫日线，
  基数是 train 1,171,124 / test 127,972、正样本 216,361（10-30 两只逐字相同）。

## 5. 顺带查出的三条缺陷（本轮只披露，未处置）

1. **三个 daban 研究入口在 HEAD 上直接跑不起来。** `b7ae04f`（2026-10-28）把 `tdxfeed/features_5d.py`
   改成相对导入（`from .execution import limits`，HEAD 第 37 行），于是任何以 bare 名导入该模块的脚本都抛
   `ImportError: attempted relative import with no known parent package`。受影响站点：
   `scripts/eval_daban_pattern.py:43`、`scripts/compare_daban_vs_stage1.py:43`、`scripts/diagnose_sample_design.py:33`。
   实测 `venv/bin/python scripts/eval_daban_pattern.py --limit 30 --workers 4 --out /tmp/...` → **RC=1，1 秒即退**；
   独立复现 bare 导入 → RC=1。守卫缺口：`grep -rln 'eval_daban_pattern|compare_daban_vs_stage1|diagnose_sample_design'
   tests/ v5/tests/` → **退出码 1**，三个脚本一条测试都没被收。注意这三笔都在 10-28（与口径重构同日）。
   本轮靠 `/tmp` 侧的导入垫片（预置 `sys.modules['features_5d']` + `set_start_method('fork')`，
   因 Python 3.14 默认 forkserver 会让子进程重导主模块）完成重扫，**未改仓库任何代码**。
2. **`iter_stock_files` 按裸 6 位码记账，同码指数与个股撞车。** `scripts/train_daban_model.py:76-90`
   剥掉 `sh/sz/bj` 前缀后只按 `is_stock_code` 过滤，于是 `sh000001`（上证指数）等 5 只指数被当成
   000001 号个股进入样本。实测：10-22 快照 **1,643 个重复 `(date, code)` 键 / 3,286 行（占 0.1900%）**；
   11-05 重扫全段 **1,683 键 / 3,366 行（0.1889%）**，而同一批键限在 ≤2026-10-20 窗口时与旧快照一致（1,643/3,286）。
   撞码 code 为 `000001/000016/000688/000852/000905`。现役 10-19 那只训练时**未排除**它们，
   10-30 那对用 `--exclude-symbols-file` 排除了 7 只（`daban_hfq_skipped_20261030.json`）。
   敏感性见 §3.2 第三行：去掉撞码后 Δ 从 −0.35pp 变 −0.40pp，**不改变判定**。
3. **`is_st` 是硬编码 0 的死维。** `tdxfeed/features_5d.py:179` `"is_st": 0`（注释「默认 0，由调用方按需通过
   name 覆盖」），而 daban 的训练侧（`:173-174`）与推理侧（`scripts/predict.py:466`）都不覆盖 →
   该维在训练与推理里恒为 0。两侧同死，所以**没有 train/serve skew**，只是白占一维。
   同处 `:148` `limit = get_limit(code)` 不传 `name`/`on_date`，`features_5d.py:67-69` 自己警告过
   「做 2020-2026 的历史区间一定要把交易日传进来，否则并轨前的主板 ST 会被误判成 10% 档」。

## 6. 已拍板：ST 日期分档**不**并入 daban 标签

代价边界实测（`/tmp` 重扫快照 + `tdxfeed.names.load_universe` 当日名单）：名单 5,221 只里 **ST/*ST 201 只**，
在 OOS 快照中占 **69,099 / 1,781,464 = 3.879%** 的行，其 `limit` 只取 0.095（49,560 行）或 0.195（19,539 行），
**没有 0.045 一档**；这些行的 `is_st` 全为 0。误差方向是「ST 票真 +5% 涨停被记成非涨停」，即标签偏保守。

这个 3.879% 是**上界**：拿今天的 ST 名单套 2025-2026 的历史行本身就是后视映射，
真正按交易日分档需要 `on_date`（这正是被决定不并入的那件事）。由于两只候选模型与现役模型**同病**，
它不影响 §3 的位移判定，只影响绝对命中率。若将来要并，那是换口径、需要重训 + 重扫 + 重做双跑。

## 7. 复现命令

```bash
# 两只模型同一行集打分（只读，--no-write 不落盘）—— 实测 25 秒
venv/bin/python scripts/report_daban_topk.py --no-write \
  --model live=data/double_up/d_signal_5d_model_daban.txt \
  --model r1030=data/double_up/d_signal_5d_model_daban_raw_20261030.txt

# 位移数 + 按交易日整块自助 —— 实测 7 秒（脚本在 /tmp，见 §8）
systemd-run --user --quiet --scope -p MemoryMax=8G -p MemorySwapMax=2G \
  venv/bin/python /tmp/daban_displace.py data/double_up/daban_pattern_eval_20261022.parquet

# 刷新评测集到最新交易日 —— 实测 3 分 08 秒（5,235 只 / workers 16）
# ⚠ 直接跑 scripts/eval_daban_pattern.py 现在是 ImportError（§5.1）；且默认 --out 写 data/daban/
venv/bin/python /tmp/run_eval_daban.py --workers 16 --out /tmp/daban_pattern_eval_20261105.parquet
```

标签行自 `3f9c799` 后未变、`--price-basis` 不切标签，因此**没有**「重训到新口径」这一步要跑。

## 8. 产物与归属

| 产物 | 位置 | 状态 |
|---|---|---|
| 冻结评测集（现役在用） | `data/double_up/daban_pattern_eval_20261022.parquet` | 未改动，197 MB |
| 重扫至 11-03 的评测集 | `/tmp/daban_pattern_eval_20261105.parquet` | **204,502,679 字节，未入库**：本轮是只读调研，`data/` 写入需另行授权；`/tmp` 会被清，复扫成本 3 分 08 秒（§7） |
| 位移/对照探针 | `/tmp/daban_displace.py`、`/tmp/daban_rescan_control.py`、`/tmp/run_eval_daban.py` | 一次性脚本，未入库 |
| 现役模型 | `data/double_up/d_signal_5d_model_daban.txt` | **未替换**，md5 `cc0696ca…` |
| `AGENTS.md` | 「训练标签口径」新增一条负向登记 | 已改（限主语为 `hypothetical_pnl`，禁止再以「向新口径收敛」重训 daban） |

## 9. 未处置 / 需要拍板

1. **§5.1 的三处 bare import** 是 1 行×3 的修复（改成 `from tdxfeed.features_5d import ...`），
   外加一条「研究入口可导入」的守卫；本轮受只读约束未动代码。**这条最该先修**——它已经在悄悄削减
   daban 这条线的可复现性（`report_daban_topk.py` 能跑，是因为它不导入 `features_5d`）。
2. **§5.2 撞码是否收编**（`iter_stock_files` 改用 `paths.daily_files()` 的规范名 + `exchange_prefix`），
   属数据宇宙口径变更，会让训练样本与现役模型不再同集，需要单独一轮。
3. **重扫产物是否落 `data/`**：本轮判定不依赖它（冻结快照已给出同向结论），但后续要做「以最新数据判定」
   的常规复核就得有个稳定路径；`--out` 默认写 `data/daban/`（`:57`、`:249`）而非 `data/double_up/`，
   与现役快照不在同一目录，落盘前先定归属。
4. **`report_daban_topk.py` 不产出位移数**：它只并排给每只模型的 AUC/Top-K，重合与配对自助差是本轮在 `/tmp`
   脚本里算的。若要把位移做成常备判据，应把这段并进该脚本（含破平口径一致性用例），否则下一次又会各算各的。
5. **三个 AUC 数不可互相引用**：0.7478（10-19 训练报告，自身 test 113,892 行）／0.8090
   （切流时 `compare_daban_vs_stage1.py` 抽样对照，334 日 199,283 行——而该脚本现在跑不起来）／0.7743（本轮全量快照）。
