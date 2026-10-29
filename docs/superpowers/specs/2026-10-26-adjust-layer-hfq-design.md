# 复权层设计与日K历史补全（2026-10-26）

状态：**设计已确认，2026-10-26 中午按 HEAD 现状重写**（另一路会话已把补历史通道与因子生产落进 HEAD，本设计据此收窄）。三项决定由用户拍板 —— ① daily 继续存 raw、另加复权层；② 价格类特征一起切口径并重训；③ 标签（翻倍正样本宇宙）也切 hfq。

> **2026-10-29 收口（先读这条）**：本文「因子权威 = `data/adj_factor_ext/`」的定位**已撤回**。该目录**至今不存在**（0 文件、零消费者、无回填驱动、不在每日链路内），`tdxfeed/adj_factor_history.py` 只是**实验模块**，不构成现行权威。现行两套在役因子表是 `data/adj_factor/`（qfq 锚，`tdxfeed/adj_factor.py` 每日增量）与 `data/hfq_factor/`（真后复权，`tdxfeed/hfq_factor.py` 每日增量），两者都随 `tdx-sync.service` 的 `ExecStartPost` 维护；「三表互不可换算、禁回落」这条约束**仍然有效**。§2.2 / §2.3 / §8 描述的是一条**未实施**的路线：只有出现真实下游（收益率类特征或标签）时才按 §2.1 重新评估，落地前不得引用本文声称复权层「已就绪」。

目标：在不破坏「涨停价 = 交易所绝对价」这一不可让渡语义的前提下，让收益率类消费者拿到无除权跳空的连续价格序列，并把确实缺历史的票补到上市日（判据与只数见 §3：**候选 10 只**，不是先前按行数猜的 98 只）。


## 1. 实测前提（2026-10-25/26 量出来的，不是引用旧文档）

| 事实 | 数值 | 出处 |
|---|---|---|
| 日K 落盘口径 = 不复权 raw | `daily close / adj_factor.raw_close` 三只票均 min=median=max=**1.000000** | `data/parquet/daily/*.parquet` × `data/adj_factor/*.parquet` 直接对比 |
| 旧审计的 raw 判定成立 | raw 5543 / no_ref 1659 / mixed 27（7229 行，含已下线板块） | `data/parquet/daily/_audit_adjust_convention.csv`（2026-10-10） |
| `download_hfq_factor.py:6` 那句「raw_close 实为后复权」 | **错陈述** | 上一条实测 1.000000 即反证 |
| 因子表覆盖只有 6 年 | 抽样 400 只，起始年 **100% = 2020**，末日 **100% = 2026-09-24** | `data/adj_factor/*.parquet`；成因 `scripts/download_adj_factor_b.py:24` 硬编码 `START_DATE='2020-01-01'` |
| 因子表已停止生产 21 个交易日 | 文件 mtime 全部 **2026-09-27**，且 `data-adj` 无 timer | `ls -lt data/adj_factor/` |
| 因子是「末日锚定」的前复权系数 | `raw_close × adj_factor = qfq_close`，且末行因子恰为 1.000000 | 000001/600519/600036 三只实测 |
| ~~落在 Sina 封顶带（1000~1060 行）的只数 = 98 只待补~~ | **作废**：按行数带判截断是错的——2022 年后上市的票本来就正好约 1000 根 | 改判据见下两行；`scripts/audit_daily_history_depth.py:48-60` |
| 增量源的窗口上限 | Sina `datalen=1023`；腾讯深取未复权满页 1024、**复权序列硬顶 640** | `tdxfeed/sources/sina_source.py:19,22-24`、`tdxfeed/history_paging.py:23-27` |
| 补历史不会引入口径污染 | baostock `adjustflag="3"` 拉 sh600000 得 6530 根（1999-11-10→2026-10-23），与本地重叠 1033 日 close **最大相对误差 0.00e+00** | 一次性探针 |
| **谁需要补，本地就能算，不必逐只敲源** | `bs.query_stock_basic()` 一次调用回 9022 行含 `ipoDate`（股票 5564 / 在市 5224），实测 **0.3s** | 一次性探针（2026-10-26 11:2x） |
| 按 ipoDate 判出的真实缺口 | universe 5221 只里首日晚于上市日 **>30 天者仅 10 只、>300 天者 8 只** | 同上，逐只对比 `daily` 首行 |
| 截断的指纹 | 5 只票 `local_min` 恰为 **2022-07-01**、行数恰 **1045**（sz000900/sz300339/sz300401/sh689009/sh688511）——同一窗口批量重写，不是源尽头 | 同扫描 |
| 只读审计已跑出的范围（target=2000-01-01） | extend **4** / listed_after_target 4471 / ok 746，`request_failure_ratio=0.0`，缺 19700 交易日 | `data/audit/history_depth_20261026.json` |
| 回填已在一只票上跑通 | sh600000 现 6413 行、首行 1999-11-10 = ipoDate；`done=["sh600000"]` | `data/parquet/daily/sh600000.parquet`、`data/.state/history_backfill.json` |
| 三套因子口径（两套在盘、一套未落地），**互不可换算** | `adj_factor`（qfq 锚、2020 起）/ `hfq_factor`（2020 起）/ `adj_factor_ext`（设计为 hfq 锚、可追到 2000 前，**未落地**）。2026-10-29 重建前实测：新/旧 hfq 重叠 1633 行比值 中位 12.221060、**std 0.437535** ⇒ 不是同一量的两种刻度 | `tdxfeed/adj_factor_history.py` 文档字符串 |
| 现役价格模型的重训成本 | 1,071,227 训练 / 113,892 测试，29 维，**17–19 分钟，CPU ~3GB** | `docs/V9R_CKPT1000_ASSESSMENT_2026-10-19.md:100-108` |
| 可复现基线 | daban OOS AUC **0.7478**（raw 口径）；对照 0.7602 / 0.7459 在盘 | `docs/RERUN_PLAN_2026-10-19.md:103`、`data/double_up/daban_train_report*.md` |

### 1.1 交付批次（每批独立可停，后一批不依赖前一批已落盘）

> **本节 2026-10-26 中午重写**：写这份 spec 的同时，另一路会话已把「补历史通道 + 因子生产」落到 HEAD（`8db2d82`→`301cc6b`，共 6 笔）。原 P1/P2 与它重复，照抄等于造第二套权威。下表按「谁已在盘上」重切。

| 批 | 内容 | 状态 | 停止点判据 |
|---|---|---|---|
| **P0 已在 HEAD** | 腾讯分页深取（`tdxfeed/history_paging.py`）、幂等回填 runner（`backfill_history.py` + `scripts/backfill_daily_history.py`）、深度只读审计（`scripts/audit_daily_history_depth.py`）、hfq 因子反推（`tdxfeed/adj_factor_history.py` → `data/adj_factor_ext/`） | **已落地并有测试**（4 份守卫文件） | 本设计不复述、不重写，只作为 P1/P2 的依赖 |
| P1 因子接线 | `adj_factor_ext` 补跑到全市场 + append-only 不变量守卫 + **读侧咽喉 `tdxfeed/adjusted.py`** | **2026-10-29 定夺：不落地**（目录不存在、零消费者、无回填驱动；`adj_factor_ext` 降级为实验模块，现行因子以 `data/adj_factor` + `data/hfq_factor` 两条每日链为准）。咽喉同样**不存在**，`features_5d` 仍按旧契约用 raw close | 验收 §8 第 1、2 条过；此时线上行为一字未变，可安全搁下 |
| P2 补历史 | 按 ipoDate 判据补 **8~10 只**（不是 98）+ 重叠区对账门禁 | 已在 sh600000 单只跑通，其余未跑 | 验收 §8 第 3 条过；每只补前留单日快照 |
| P3 口径切换 | `features_5d` 逐维分派 + 无裸 close 不变量守卫 + daban 双跑对照 | 待做，且**是这份设计唯一还独占的部分** | 验收 §8 第 4、5 条过；**线上模型不切** |

P1 做完就能停下来评估价值——如果咽喉现算的收益率已经够用，P2/P3 可以无限期推迟；P3 依赖 P1，不依赖 P2（补历史只影响可用样本长度）。

## 2. 架构：两层权威 + 一个读侧咽喉

```
腾讯 hfq + 未复权两条序列 ──► data/adj_factor_ext/{code}.parquet  ← 实验模块（未落地，不是权威；见 §2.1）
data/parquet/daily/*.parquet                                     ← 价格权威（raw，不改写历史）
                    └──────────► tdxfeed/adjusted.py: frame(symbol, kind)  ← 唯一咽喉（本设计新增）
                                            ├─► tdxfeed/features_5d.py（训练与预测共用）
                                            └─► quant/multifactor / plan_e_backtest（后续单独决定）
```

**两次修正，都写在这儿以免再漂**：

1. 原计划「后复权落盘成 `daily_hfq/`」作废，改为**派生价格不落盘**——要治的病恰恰是「`adj_factor` 固化后过期 21 天」，再落一张派生价格表就是再造一个会过期的权威。
2. 原计划自建 `tdxfeed/adjfactor.py` + `data/adj_factor_events.parquet`（baostock 逐事件因子表）**同样作废**：HEAD 上的 `tdxfeed/adj_factor_history.py` 已经用「两条序列逐日相除」给出同一种东西，且锚在上市日、历史段不动，满足 append-only。再加一张事件表就是第四套因子数字，与本设计要解决的问题正面冲突。

落一张**因子**表不算破例：因子是比率，不是价格；`adj_factor_ext` 存 `date/raw_close/hfq_close/factor` 四列，下游要的连续价格仍由咽喉现算。

### 2.1 因子表 `data/adj_factor_ext/`（实验模块；2026-10-29 定夺：不落地、不构成权威）

**本节写的是「若要落地该怎么做」，不是现状。** 2026-10-29 的收口结论：该目录从未生成，全仓库只有 `tdxfeed/adj_factor_history.py` 与 `tests/test_adj_factor_history.py` 提到它，无消费者、不在每日链路内；日常两条因子链已在役（`data/adj_factor/` qfq 锚、`data/hfq_factor/` 真后复权，均为 `tdx-sync.service` 的 `ExecStartPost` 增量）。因此本模块降级为**实验模块**，本设计撤回「因子权威 = `data/adj_factor_ext/`」的定位，回填与否等真实下游出现再定。下面两条约束**不随撤回失效**，将来落地时照此执行：

模块本体已有 `derive_factor(raw_rows, hfq_rows)`（逐日 `factor = hfq_close / raw_close`，只出两条序列交集，交集内部有洞直接抛）与 `write_ext`（`.parquet.tmp` + `os.replace`，半写不可见），`tests/test_adj_factor_history.py` 锁着写入形状；缺的是数据与消费者。

- **选表规则写进代码，不留在文档里**。三套因子表互不可换算（`adj_factor_ext` / `data/hfq_factor/` 重叠 1633 行比值 std 0.437535），`tdxfeed/adj_factor_history.py` 的文档字符串已经点明「不要做读不到就回落到另一套」。这条约束目前只活在文档字符串里，咽喉实现必须把它变成机械检查：`adjusted.py` 里出现的因子目录常量**只允许一个**，守卫扫源码禁止任何第二处 `hfq_factor` / `adj_factor` 读取路径进入价格派生。
- **append-only 不变量要有绊线**：`factor` 在除权日之前不得随新事件改变。现有测试只锁了写入形状，没锁这条。

`data/adj_factor/*.parquet` **不是退役表**：它由 `tdxfeed/adj_factor.py` 每日增量维护（`tdx-sync.service` 的 `ExecStartPost`），并继续服务已有的两个研究脚本（`scripts/clean_double_up_detail.py:60-69`、`scripts/find_double_up_c_v2.py:157`）；本设计不改这两个脚本。它锚定 qfq、只覆盖 2020 年起，所以不能当连续价格权威用。


### 2.2 `tdxfeed/adjusted.py`（待落地；当前不存在，唯一咽喉）

```python
frame(symbol: str, *, kind: str = "raw", root: Path) -> pd.DataFrame   # 未实现
```

- `kind="raw"` 直接返回 `data/parquet/daily/{symbol}.parquet`，**一字不改**（涨停价、封板判定走这条）。
- `kind="hfq"` 返回同形状，但 `open/high/low/close` 各乘**当日** `adj_factor_ext.factor`；`volume/amount/date` 不动。因子按日查表（`searchsorted` 取「不晚于当日的最后一行」），不再自己拼事件阶梯。
- `kind="qfq"` 同上但用 `factor / factor(表内末日)`，且**只在读侧存在**，函数文档必须写明「qfq 随新除权整体平移，任何把它固化的做法都是错的」——`adj_factor` 那张 2020 年起的 qfq 锚表就是这个错误的实物证据。
- 无因子行 → 因子恒 1.0，`hfq` 逐值等于 `raw`（这条由测试锁住，是「无除权股派生无代价」的依据，也是深取段没跑到时不会静默变形的保证）。
- 缺 `adj_factor_ext/{code}.parquet` → 返回 `raw` 并在 `df.attrs["factor_missing"] = True` 上留痕，日志打一行；**不静默降级，也不回落到 `hfq_factor`/`adj_factor`**。


任何消费者不许自己拼因子。这是本设计唯一的接口约束，也是防止重演「训练 raw / 预测 qfq」的那个约束。

### 2.3 `features_5d.py` 的逐维分派

判据一条：**要拿去和真实报价/成交价对表的量 → raw；量本身就是比率的 → hfq。**

- 切 `hfq`：MA 比值、收益率、波动率、动量、gap（open/prev_close）、RSI/MACD 类。
- 留 `raw`：涨停价与封板（`tdxfeed/daban/limit_price.py:144-157` 四舍五入到分）、9.5% 阈值类判定（`scripts/plan_e_backtest.py:233`）、一切与委托价同表的字段。
- 新增不变量守卫：`features_5d` 内**不得出现裸 `close`/`prev_close` 引用**，每个价格型输入必须显式来自 `frame(kind="raw")` 或 `frame(kind="hfq")`。这条是设计落没落地唯一可机械检查的形式。

训练侧与预测侧本来就共用 `compute_window_features_extended`（`scripts/train_daban_model.py:31-35` 与 `scripts/predict.py:166` 同一函数），所以口径只改一处即同源，不存在分叉——这是第三档能做的结构性原因，也是 `features_5d.py:3-5` 那条教训的正面实现。

## 3. 补全历史

范围不再靠行数猜。`bs.query_stock_basic()` 一次调用（9022 行、含 `ipoDate`、实测 0.3s）就给齐全市场上市日，于是「谁缺历史」变成本地可算：

```
gap = local.date.min() − ipoDate(sym)        # 只读，零逐只探测
候选 = gap > 30 天                            # 留缓冲：上市即停牌会造成合法的小 gap
```

实测 universe 5221 只里 **gap>30 天仅 10 只、>300 天 8 只**（2026-10-26 扫描）。其中 5 只的 `local_min` 恰为 **2022-07-01**、行数恰 **1045**——同一窗口批量重写的指纹，不是源尽头。另有 69/42 天两例（sh600653/sh600605）落在缓冲带内，属停牌类正常情形，**不补**。

对照已有的只读审计（`data/audit/history_depth_20261026.json`：extend 4 / listed_after_target 4471 / ok 746）：它按 target=2000-01-01 回答的是「够不够到 2000 年」，`plan_gaps` 里 `local_min <= target` 直接短路成 `ok` 且**从不问源**，所以那 746 只从未被证明到上市日。ipoDate 判据补的正是这个洞，两者结论对 sh601607/sz000900/sz001872 一致，且多找出 sz300339/sz300401/sh689009/sh688511。

执行复用 HEAD 上的 `scripts/backfill_daily_history.py`（幂等、done 集单权威、连续 10 只失败熔断、零成功退 1），不再自建 baostock 通道。**sh600000 已于 09:41 补成 6413 行、首行 1999-11-10 = ipoDate**，其余候选未跑。

**回填缺的一道门禁（本设计要补）**：`Writers.merge_write` 是 `concat([old, new])` + 稳定排序 + `keep='last'`，语义是**同日新值覆盖旧值**（`tdxfeed/writers.py:152,160`，注释在 154 行写明了这是有意的）。而 `backfill_symbol` 深取的是 `[target, 今天]` **全段**，重叠区＝整个本地文件——等于把每只票的全部历史 close 静默换成腾讯的值。价格权威恰恰是 raw 交易所价，一旦两源有出入（尾差、除权日处理），本地就会在无人报警的情况下改口，而涨停价与封板判定直接和委托价对表。

实测这一风险在本次彩排里没有发生：sh600000 重叠 1633 日（2020-01-02..2026-09-24）对 `adj_factor.raw_close` 这条**独立生产者**的最大相对误差 **0.000e+00**，sz000001/sz000900 同样 0。**但这是「一只票没出事的证据」，不是「合并路径安全」的证明**，所以门禁照建：

```
for sym in candidates:
    new = fetch_history(sym, ...)                       # adjust="" ⇒ 未复权，与本地同口径
    overlap = new ∩ local
    maxrel = max(|new.close − local.close| / local.close)
    if maxrel >= 0.005: 拒绝整只，记入 rejected 清单，不写一个字节
    else: 并入（可继续走 merge_write 的覆盖语义——已对账，覆盖无害）
```

0.5% 这道门禁是承重墙，原因之一是仓库现成的长历史通道有写死前复权的：`tdxfeed/sources/akshare_source.py:12-14`、`tdxfeed/sources/tencent_source.py:17,28` 都固定 `adjust="qfq"`。顺手用它们就是把 qfq 灌进 raw 文件，让同一只票内部口径分裂——比现在更糟且不可逆（`data/` 在 gitignore 里）。`history_paging.fetch_history` 把 `adjust` 作为必填语义的参数（未知类型走 `KeyError`，不静默回落，见 `tdxfeed/history_paging.py:28-30`），这正是本设计要的形态。

深取段的 `amount` 恒为 0（腾讯日K 不吐成交额，`history_paging.py:113`）。并发会话正在把 `fill_missing_amount` 接进 `merge_write`（**此刻是工作树里的未提交改动，不归本设计**）；接线与否不改变上面的对账门禁——它只管价格。


## 4. 落盘纪律

1. **原子写仍未做**：`tdxfeed/writers.py` 的 `_write_parquet_date32` 末尾是就地 `pq.write_table(table, p, ...)`（HEAD 与工作树都是），中断即坏文件。改成写同目录临时文件再 `os.replace`，照仓库内既有先例 `tdxfeed/amount_fix.py:55-57` 与 `adj_factor_history.write_ext`（`tdxfeed/adj_factor_history.py:59-61`）。
   **协作约束**：`tdxfeed/writers.py` 与 `tdxfeed/incremental.py` 此刻是别人的未提交改动（接 `fill_missing_amount`），本条要等那笔落地后再动，或由其顺带完成——同文件并发编辑是共享工作树里唯一真正会互相踩的东西。
2. **备份**：`data/` 在 gitignore 里，git 救不回来。实测本机**当前没有任何 `*bak*` 快照**，而 sh600000 已经在全段覆盖语义下被改写过一次、事前没有备份。规则定为：补一只就 `cp -a` 一只的 parquet 到 `data/parquet/daily.bak-YYYYMMDD/`，不整目录复制（5236 只没必要）。
3. **可续传已有单权威**：`data/.state/history_backfill.json` 的 done 集（`backfill_history.load_done/mark_done`，主线程原子换文件）。本设计**不再新增** `adj_events.json`——回填要回答的只有「这只补过没有」，第二份进度文件只会漂移。


## 5. 重训与对照

1. 先建因子与派生层并跑通守卫，**再**动 `features_5d`。
2. daban 双跑，同 seed、同切分（2025-06-01）、同样本参数：
   - 对照组：口径全 raw（现状），用于确认这次改的只有口径。
   - 实验组：特征 hfq + 标签 hfq。
   两组都写独立 `--tag` 文件（`train_daban_model.py:243-244` 已支持），**不覆盖现役** `data/double_up/d_signal_5d_model_daban.txt`。
3. 因为标签也切了 hfq（用户拍板），0.7478 这个历史基线**不可比**，上面这个同日子对照是唯一有效的比较对象。约 17 分钟 × 2。
4. 旧模型不续训：`tdx_v7_resume.yaml` 那种 `adapter_name_or_path` 复用旧口径 checkpoint 的路径，对价格模型同样禁止。
5. 切换线上模型是**另一次决定**，不在本设计交付范围。

## 6. 时序与运维

| 窗口 | 约束 |
|---|---|
| 09:00 起 `daban-ticket.timer` 每 5 分钟触发（今日首次自动跑） | 盘中 09:30~15:00 **不改 `features_5d`、不改 writers**；盘中只写文档与测试 |
| 18:00 `tdxfeed-sync.timer` 写 `data/parquet/daily/` | 范围缩到 8~10 只后，整轮只要几分钟：**跑在 18:00 前即可，不必停 timer**。原「补 98 只需授权停生产 timer」这条待决事项**作废**（同一只票两次 `merge_write` 交叠才是风险，隔开时段就够） |
| baostock 可用性 | **只有 `query_history_k_data_plus` 挂死**；`login` 与 `query_stock_basic`（2026-10-26 11:2x 实测一次调用 9022 行 / 0.3s）正常。回填已改走腾讯分页，不依赖 baostock 的历史接口，只借它的上市日 |

## 7. 失败模式

| 失败 | 行为 |
|---|---|
| `query_history_k_data_plus` 挂死（**已实测**：08:44 同参数成功 6530 根，09:05 起含「近4年」在内的所有调用挂死 >100~540s，`login` 仍成功） | 回填不走它，故不构成阻塞项；若仍要用，每次调用带 30s 硬超时。**通道级熔断已有**：runner 连续 `TRIP_FAIL_STREAK=10` 只失败即判通道故障停轮（`scripts/backfill_daily_history.py:31`），本设计不另定 N |
| 腾讯 WAF 按 IP 封 501（**已实测**：4 并发跑到 ~3400 请求 ≈6.4 req/s 后 ifzq 与 web.ifzq 双双被拦） | `history_paging` 已内建：`WAF_CODES=(401,403,429,501,503)` 退避 30s 换入口，健康域排第一（`history_paging.py:32-49`）。审计默认 `--workers 2` 压阈值以下。8~10 只的量根本撞不到上限 |
| 「请求被拒」被当成「历史到此为止」（静默截断的成因本身） | 已由 `PageRequestError` / `EmptyPageError` 分开判（`history_paging.py:11-12,52-58`）；深取返回 0 行一律抛 `RuntimeError`，不静默少补（`backfill_history.py:58-59`） |
| 重叠区对账不过 | 该只**整只拒绝**，写 rejected 清单，绝不部分写入 |
| 票无除权 ⇒ `adj_factor_ext` 无行 | 因子恒 1.0，派生逐值等于 raw（守卫锁死） |
| 因子表缺该 code | 派生退回 raw 并在 `attrs` 留痕，日志打一行；**不静默、不回落到另两套因子表** |
| **因子表末端滞后（新除权发生在因子表之后）** | 这是换掉事件表之后**新引入**的失败模式，且正是 `data/adj_factor/` 停更 21 个交易日那个病：逐日因子表若不刷新，末端会沿用上一行的因子，把除权当成真实涨跌幅。**必配两道**：① 新鲜度守卫——`adj_factor_ext` 末日不得比 `daily` 末日旧超过 1 个交易日，红就阻断训练/预测；② 生产者——`adj_factor_ext` 需要挂进夜间链路（或至少显式 `make` 目标），否则它和旧表一样会烂。事件表原本对这条免疫（事件稀疏），接受这个代价的前提是守卫先落地 |
| 大批量写中断 | 已改原子写 ⇒ 最坏情况是旧文件完好，不会出现半写 parquet |
| `features_5d` 漏改一处裸 close | 不变量守卫直接红，挡在 CI |


## 8. 验收标准

> 以下全部是**落地后**的验收标准；2026-10-29 收口时一条都还没有执行（第 3 条除外——补历史已单独在推进，见 §3）。

1. **派生不反噬 raw**：`frame(sym, kind="raw")` 与磁盘逐字节一致；且 `adj_factor_ext.raw_close` 对 `daily.close` 逐日误差 0（因子表由这两条序列相除而来，误差非 0 就说明它在覆盖之后又变了）。sh600000 用独立生产者 `adj_factor.raw_close` 实测已为 **0.000e+00 / 1633 日**，其余候选照此复核。
2. **复权真的起作用，且因子表不陈旧**：取 `factor` 发生跳变的相邻各 5 日（600519 最后一次实测 2026-06-26），`hfq` 收益率连续、`raw` 出现台阶；同时 `adj_factor_ext` 末日不得比 `daily` 末日旧超过 1 个交易日——这条不通就禁止开训、禁止出预测。
3. **补历史**：§3 的 10 只候选逐只落到「已补到 ipoDate（gap ≤ 缓冲带）」或「进 rejected / 停牌清单」两者之一；重叠区一字不变（按第 1 条的对账方式复测）；每只补前留有单日快照；行数只增不减。
4. 全量 `venv/bin/pytest tests/ -q` 绿 + `venv/bin/ruff check` 干净 + 不变量守卫（无裸 `close`、单因子目录）通过。
5. 双跑对照报告：同日子 raw 组 AUC 与历史 0.7478 的差 = 「非口径因素」的漂移量；hfq 组与它的差 = 口径效应。两个数都要报出来。

## 9. 非目标

- 不改写 `data/parquet/daily/` 的价格语义（raw 仍是落盘权威）。
- 不做指数全历史（universe 只有股票；审计与回填都不碰指数）。
- **不重写 P0 已有的四个模块**：`history_paging` / `backfill_history` / `adj_factor_history` / `audit_daily_history_depth` 及其 4 份守卫留在 HEAD，本设计只在其上加咽喉与门禁。
- 不动 stage1/vpnl 链：它是四段缓存（`d_signal_5d_features.csv` → `_v9.csv` → `_vpnl.csv` → `stage1_wide_table.parquet`），`_v9.csv` 已不在盘，且 stage1 无可复现基线——没有对照就不重训。
- 不为 LLM 侧重蒸：`scripts/build_distill_skeleton.py:43-50` 确实把 OHLCV 原值写进 prompt，但 LoRA 未接入线上（predict/plan_e 不加载）。设计里只标注「将来接入需重蒸」。
- 不改 `clean_double_up_detail.py` / `find_double_up_c_v2.py`（旧因子表的仅存消费者）。
- 不切换线上模型。

## 10. 守卫清单（TDD，RED 先行）

> 下表 6 份测试**全部尚未创建**；唯一已存在的复权守卫是 `tests/test_adj_factor_history.py`（只锁写入形状，不在下表）。不要把下表当成已存在的门禁。

| 文件 | 锁住什么 |
|---|---|
| `tests/test_adj_factor_ext_freshness.py` | 因子表末日与 `daily` 末日相差不超过 1 个交易日；缺表时报错而非静默；**append-only**：给同一只票追加新事件后，旧日期的 `factor` 一字不变 |
| `tests/test_adjusted_frame.py` | `raw` 与磁盘逐字节一致；`hfq` 逐值 = `close × 当日 factor`；无因子行恒等于 raw；缺因子退回并留痕；qfq 不落盘 |
| `tests/test_single_factor_authority.py` | 扫源码：价格派生路径里只允许出现 `adj_factor_ext` 一个因子目录常量，读到 `hfq_factor` / `adj_factor` 即红（把「不可换算就别回落」从文档字符串变成绊线） |
| `tests/test_daily_backfill_gate.py` | 重叠区误差 ≥0.5% 时整只拒绝且零写入；候选清单来自 ipoDate 判据而非行数带；行数只增不减 |
| `tests/test_writers_atomic.py` | 写盘中途抛异常时原文件完好（tmp + `os.replace`）**——等并发会话那笔 `fill_missing_amount` 落地后再动同一个文件** |
| `tests/test_features_5d_no_bare_close.py` | 扫源码，`features_5d.py` 内不得出现裸 `close`/`prev_close` 引用 |
