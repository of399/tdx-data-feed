# stage1 特征集合双跑：把 Alpha158 的 47 条净新增并进模型，值不值

> 一次性研究快照，记录 2026-11-04 这一轮的口径变化、数字与裁定；不是现状规格。
>
> **结论：现役 21 维契约不切。** 47 条净新增在模型层带来的是 +0.02 量级的池化 Rank IC
> 改进，两个宇宙方向一致、幅度接近，但在各自的比较集（8 个 Δ）里 `q_boot` 分别是
> 0.276 / 0.084，都过不了 BH；钱上的 Top-20 差（+0.0715 / +0.0586）置信区间全部跨 0。
> 论证顺序见 §5，代价与没做的事见 §6。

## 1. 这一轮回答的问题，与因子层那轮的分界

`docs/research/2026-11-04-alpha158-vs-feat26-ic.md` 在**全市场面板**上量到的是：158 维在
ρ=0.8、h=5 下去重后有 56 条独立，扣掉现役当 seed 后**净新增 47 条**，其中 33 条过 BH。
那句话的主语是"因子"，不是"模型"——它说的是这 47 条在 1,199 只票、日均 1,173 只（中位）的横截面上排得动远期收益。

本轮把同一批 47 条真装进 stage1 的训练表并重训，问的是另一个问题：**在一个只有 6 笔/天的
事件排序任务里，加这 47 列能不能让模型挑得更好**。两轮共用一份名单工件
（`config/alpha158_union.json`），所以"IC 判过的那一维"与"模型吃到的那一列"不必靠措辞对齐。

## 2. 三份契约：只让特征集合这一条轴动

名单一律由 `scripts/stage1_union_configs.py` 从两份权威工件派生（基线 21 维取自
`data/double_up/stage1_selected_features_hfq_20261030.json`，47 条净新增与 15 条现役独立
取自并集工件），生成器里没有任何列名字面量，产物不含时间戳因此 `--check` 可当幂等判据：

| 契约 | 维数 | 组成 | 角色 |
|---|---:|---|---|
| `b0` | 21 | 冻结契约原样 | 基线 |
| `b1` | 68 | 21 + 净新增 47 | **主候选**（纯加法） |
| `b2` | 62 | 现役独立 15 + 净新增 47 | 诊断对照（并集原样） |

`b2` 不能当候选，是集合运算的结果而不是意见：并集那 62 条会剔掉基线里的 12 维
（`amihud_20`、`amount_surge_3d`、`is_bj`、`is_st`、`limit`、`max_drawdown_5d`、
`range_efficiency`、`ret_skew_20`、`up_down_ratio`、`vol_norm_ret`、`vol_term`、
`vp_corr_20`），换来 6 条基线里没有的现役独立维（`acceleration`、`avg_amount_5d`、
`dist_to_5d_low`、`is_gem`、`ret_5d`、`vol_ratio_t1`）。被剔的 `is_st` / `is_bj` 在面板里
**根本测不出 IC**——不是"IC 小"，是分母不存在：实测面板 769,828 行里两列恒为 0
（`is_st=1` 的行 0 条、`is_bj=1` 的行 0 条，656 天没有一天有变动），常数列在
`cv_utils.rank_ic` 里直接给 NaN。"测不出"不等于"冗余"——这正是 `AGENTS.md` 里
「集合内独立 ≠ 净新增」那条的镜像情形。契约 JSON 把这两个清单写进 `provenance`，
所以这个理由是可见的，不用回来读这份文档才知道。

三份共用同一组超参与协议（huber α=0.5、`random_state=42`、早停 50），行集由
`load_and_validate(表, ALL35)` 固定、与契约无关。

## 3. 宽表装配：一次全量跑撞到一条会整只丢票的缺陷

| 表 | rows | cols | 缺失文件 | 缺因子跳过 | Alpha158 失败 | rc | 耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `stage1_wide_table_union47_hfq.parquet` | 9,979 | 86 | 3,083 | 2 | **0** | 0 | 771 s |
| `stage1_wide_table_union47_universe_hfq.parquet` | 9,775 | 86 | 0 | 0 | **0** | 0 | 未单独计时 |

86 列 = 既有 39 列 + 47 条净新增。两张表与 2026-10-30 的同口径表**逐键同集**（9,979 / 9,775
个 `(code, signal_date)` 一个不差），37 个共有列里 36 列逐值相等，只有 `turnover_proxy`
有 11 个单元在第 4 位小数上不同（1,158.9999 → 1,159.0 这类），是 10-30 之后日线被源修订
带出来的，不是本轮改动。

**第一次全量装配把 10 只票整只丢了。** 症状不是"这一维缺值"：停牌日 `volume=0`，我用
`replace(0, pd.NA)` 把除零挡成 NA，而 `pd.NA` 让列变成 object 可空类型，下游任何一次
`float()` 都对 `NAType` 抛 TypeError，失败记账于是把**整只票**的行丢掉——行数少几条、
列一个不少、其余值全都正常。改成面板同款的 `np.nan`（`_alpha158_frame` 与
`scripts/alpha158_ic_panel.py::build_code_panel` 本来就声明要逐字同形，这一处是那句声明
第一次被真数据检验）：停牌日的 `$vwap` 给 NaN 而不是 0，行留在表里。修后两张表都是
`Alpha158 失败=0`，47 列共 469,013 个单元里非有限 7 个（就是那几天的停牌 vwap）。
守卫 `tests/test_stage1_wide_table_alpha158.py::test_suspended_day_yields_nan_not_a_dropped_code`
把两半分开钉：远离锚点的停牌日不污染取值，锚点当天停牌则该行仍在、`VWAP0` 是 NaN、
不用 `$vwap` 的 `ROC5` 不受影响。

## 4. 判定为什么不是"逐日配对 Δ IC"

原计划沿用面板那轮的判据：逐日横截面 Rank IC，同日相减得 Δ 序列，再做 Newey-West
（`maxlags=5` 跟着标签的前瞻天数）。第一次真跑交出来的是一张 `n_days=0`、均值 NaN 的空表。

原因是样本形状，不是代码：

| | 全市场面板 | stage1 事件样本（本轮） |
|---|---:|---:|
| 每日横截面中位 | 1,173 只 | **6 笔** |
| 达 `min_codes=30` 的天 | 656 / 656 | hfq 0 / 415、universe 0 / 430 |
| 截面 p10 | 1,155 | 2（hfq）/ 3（universe） |

6 笔的 Spearman 抽样噪声比效应本身还大，把它平均出来只会得到一个看起来像结论的数——
这正是 `IcConfig.min_codes=30` 存在的理由。所以本轮的判定换成**按交易日整块重抽的配对
自助法**：天是抽样的最小单位（同日共享市场水位，逐行重抽会把相关性当独立性、CI 窄到骗人），
指标仍是池化的。评测器把这条判据的可用性也显式写进产物（能算 IC 的天数、达标的天数、
为什么不用它），不许再交出一张 NaN 表——空表会被读成"验出来是 0"。

协议对账：池化那一栏与 6 次 `stage1_retrain` 报告的 `honest_oos` **逐字相同**
（0.2533 / 0.2754 / 0.2643 与 0.2532 / 0.2787 / 0.2703），所以自助法用的模型与重训报告
的模型是同一套协议，不是两套实现。

## 5. 数字

OOS = `signal_date ≥ 2025-01-01`；`n_boot=2000`、`seed=42`；q 是 BH 校正，
**比较集 = 8**（每份报告里"非基线契约 × 4 个指标"全体——换比较集 q 就会变）。

### 池化 OOS

| 宇宙 | 契约 | 维数 | best_iter | Spearman | Top10 | Top20 | Top50 |
|---|---|---:|---:|---:|---:|---:|---:|
| hfq (n=2,695) | `b0` | 21 | 113 | 0.2533 | 0.3285 | 0.2339 | 0.2347 |
| | `b1` | 68 | 124 | **0.2754** | 0.3436 | **0.3054** | 0.2361 |
| | `b2` | 62 | 105 | 0.2643 | 0.2914 | 0.2810 | 0.2438 |
| universe (n=2,748) | `b0` | 21 | 118 | 0.2532 | 0.2926 | 0.2423 | 0.2172 |
| | `b1` | 68 | 146 | **0.2787** | 0.3369 | **0.3009** | 0.2252 |
| | `b2` | 62 | 135 | 0.2703 | 0.3431 | 0.2762 | 0.2287 |

### 配对差（自助均值 [95% CI]，p / q）

| 宇宙 | Δ | 池化 Rank IC | Top20 实际收益 |
|---|---|---|---|
| hfq | `b1−b0` | +0.0223 [+0.0015, +0.0425] p=0.034 **q=0.276** | +0.0476 [−0.0222, +0.1292] p=0.206 q=0.809 |
| | `b2−b0` | +0.0112 [−0.0122, +0.0347] p=0.351 q=0.809 | +0.0340 [−0.0426, +0.1158] p=0.404 q=0.809 |
| universe | `b1−b0` | +0.0252 [+0.0067, +0.0449] p=0.010 **q=0.084** | +0.0525 [−0.0214, +0.1287] p=0.156 q=0.417 |
| | `b2−b0` | +0.0168 [−0.0041, +0.0380] p=0.096 q=0.386 | +0.0283 [−0.0511, +0.1113] p=0.501 q=0.829 |

Top10 / Top50 两档全部 CI 跨 0（hfq `b1−b0` 的 Top10 点差甚至只有 +0.0151，而并集版是
−0.0371），完整 8 行见 `logs/stage1_union_compare_hfq.md` 与
`logs/stage1_union_compare_universe.md`。

怎么读这一张表：

1. **方向是稳的，量级不够。** 两宇宙的 `b1−b0` 池化 IC 都是 +0.022 / +0.025，单看 p 分别
   0.034 / 0.010；但 8 个 Δ 一起校正后 q 是 0.276 / 0.084。按本仓库的判据（q 依赖比较集、
   且必须连着比较集引用），这不足以判"有增益"。
2. **排序变好 ≠ 赚钱变好。** Top-20 的实际收益点差最大（+0.0715 / +0.0586），但 CI 跨 0，
   而且这一档的样本就是 20 笔——事件样本的 Top-N 天然是薄样本上的厚尾，噪声远大于 IC。
3. **"跨宇宙一致"的分量要打折。** 两张表来自同一批 5 日信号事件、同一段 OOS，只是宇宙
   过滤不同（hfq 重筛 vs 未筛），**不是两个独立样本的重复验证**。真要拿独立证据，得换
   窗口（例如只用 2025H2 训练、2026 测）或换标签口径，本轮没做。
4. **`b2` 输给 `b1`，与 §2 的集合差对得上。** 并集版剔掉的 12 维里有 `limit`、`is_st`、
   `is_bj` 这类面板测不出但事件任务用得上的维；它在 universe 的 Top10 上反而最好
   （0.3431，全场最高），但同一行 `b2−b0` 的 Top10 差 CI 是 [−0.0393, +0.1365]——
   这是薄档位常见的"点值漂亮、区间没内容"。

## 6. 与因子层那轮的关系：为什么 33 条过 BH，到模型层看不见

这是解释，不是结论：**任务不同**。面板量的是"在一天 1,173 只里谁比谁好"，事件样本量的是
"已经被 `d_signal_5d` 筛出来的这 6 笔里怎么排"。筛选本身吃掉了一部分价格形态信息——
`ROC`/`MA`/`STD` 家族在同一天里同向筛出的候选之间差异不大，所以横截面上成立的自由度
到了事件内不一定还有区分力。要证伪或证实这条，得做一个"事件样本 × 面板同维"的直接
对照（同一批 47 列，在事件日 vs 全市场日的 `\|IC\|` 分布），本轮未做。

## 7. 裁定、代价与产物

**裁定（本轮定）**：现役 21 维契约与线上模型**不切**；47 条净新增记为"模型层证据不足"，
不是"已被证伪"。名单工件继续有效，因为它同时是因子层结论的落盘证据。

- 台账：本轮属口径变更（新增可选装配维度 + 6 个新模型产物），按约定应登记
  `config/known_issues.yaml`，并把两条新约定写进 `AGENTS.md`（① 宽表价格列走 hfq 且
  `$vwap` 与面板逐字同形、停牌日 `np.nan` 不是 `pd.NA`；② 事件样本日均 6 笔，逐日配对
  IC 判据不适用，对照判定用按交易日整块重抽的自助法）。这两个文件此刻都被并发会话
  （KI-OPS-004 的同步链改造）持有未提交改动，本仓库是共享工作树，`git add` 会把别人的
  改动一起提交，所以**登记延后**到文件干净时补，裁定先由本文档承载。
- 没做的事：不动 `predict.py` 注册表、不动任何在役产物。6 个新模型
  （`d_signal_5d_model_stage1union_{b0,b1,b2}.txt`、`..._stage1univ_*.txt`）与
  6 份冻结契约（`stage1_frozen_run_*.json`、`stage1_frozen_univ_run_*.json`）都在
  `data/double_up/`（gitignore），在役 `d_signal_5d_model_vpnl_huber.txt` 与默认冻结契约
  实测未被改写（mtime 仍是 10-06 / 10-10）。两次重训都带 `--no-backup-old --no-protect-old`，
  不生成 `.bak`、不改权限。
- 成本实测：hfq 全量装配 771 s（内存上限 8 G 的 scope 内），universe 同量级；单次重训
  < 1 分钟；`n_boot=2000` 的对照一轮约 1 分钟。
- 产物：研究文档（本文件）、`logs/stage1_union_compare_{hfq,universe}.{json,md}`、
  `logs/stage1_retrain_*.json`（6 份）、`logs/wt_union47_*.log`、
  代码与守卫 `scripts/stage1_union_configs.py`、`scripts/stage1_union_compare.py`、
  `tdxfeed/stage1_wide_table.py`、`tests/test_stage1_union_*.py`、
  `tests/test_stage1_wide_table_alpha158.py`。

复现命令（都在仓库根）：

```bash
# ① 名单工件（需面板 run_meta 在场）
venv/bin/python scripts/alpha158_union_list.py --run-meta \
  data/alpha158_ic_vwapfix/run_meta_2024-01-01_2026-09-15_h5_n1200_s20261104.json \
  --rho 0.8 --horizon 5 --out config/alpha158_union.json
# ② 宽表（47 条净新增，走 hfq 读侧权威）
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 --no-parity \
  --src data/double_up/d_signal_5d_features_vpnl_hfq.csv \
  --out data/double_up/stage1_wide_table_union47_hfq.parquet \
  --price-basis hfq --alpha158 config/alpha158_union.json
# ③ 三份契约（可用真宽表验列名）+ ④ 三遍重训
venv/bin/python scripts/stage1_union_configs.py --out-dir data/double_up \
  --wide-table data/double_up/stage1_wide_table_union47_hfq.parquet
for c in b0 b1 b2; do venv/bin/python scripts/stage1_retrain.py \
  --config data/double_up/stage1_contract_$c.json \
  --wide-table data/double_up/stage1_wide_table_union47_hfq.parquet \
  --out-model data/double_up/d_signal_5d_model_stage1union_$c.txt \
  --frozen data/double_up/stage1_frozen_run_$c.json \
  --no-backup-old --no-protect-old; done
# ⑤ 对照评测（判定层）
venv/bin/python scripts/stage1_union_compare.py \
  --wide-table data/double_up/stage1_wide_table_union47_hfq.parquet \
  --contracts data/double_up/stage1_contract_b{0,1,2}.json \
  --baseline b0 --tag hfq --n-boot 2000
```
