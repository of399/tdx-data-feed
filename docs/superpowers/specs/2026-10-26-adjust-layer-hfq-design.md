# 复权层设计与日K历史补全（2026-10-26）

状态：**设计已确认，待复核**。三项决定由用户拍板 —— ① daily 继续存 raw、另加复权层；② 价格类特征一起切口径并重训；③ 标签（翻倍正样本宇宙）也切 hfq。

目标：在不破坏「涨停价 = 交易所绝对价」这一不可让渡语义的前提下，让收益率类消费者拿到无除权跳空的连续价格序列，并把卡在增量源窗口里的日K补到上市日（候选 98 只，实补只数由作业报告，见 §3）。

## 1. 实测前提（2026-10-25/26 量出来的，不是引用旧文档）

| 事实 | 数值 | 出处 |
|---|---|---|
| 日K 落盘口径 = 不复权 raw | `daily close / adj_factor.raw_close` 三只票均 min=median=max=**1.000000** | `data/parquet/daily/*.parquet` × `data/adj_factor/*.parquet` 直接对比 |
| 旧审计的 raw 判定成立 | raw 5543 / no_ref 1659 / mixed 27（7229 行，含已下线板块） | `data/parquet/daily/_audit_adjust_convention.csv`（2026-10-10） |
| `download_hfq_factor.py:6` 那句「raw_close 实为后复权」 | **错陈述** | 上一条实测 1.000000 即反证 |
| 因子表覆盖只有 6 年 | 抽样 400 只，起始年 **100% = 2020**，末日 **100% = 2026-09-24** | `data/adj_factor/*.parquet`；成因 `scripts/download_adj_factor_b.py:24` 硬编码 `START_DATE='2020-01-01'` |
| 因子表已停止生产 21 个交易日 | 文件 mtime 全部 **2026-09-27**，且 `data-adj` 无 timer | `ls -lt data/adj_factor/` |
| 因子是「末日锚定」的前复权系数 | `raw_close × adj_factor = qfq_close`，且末行因子恰为 1.000000 | 000001/600519/600036 三只实测 |
| 落在 Sina 封顶带（1000~1060 行）的只数 | **107** = 8 指数 + 1 只末日停在 2009-12-15 + **98 只待判** | 全量扫 5236 个 parquet 的 `date` 列并分类 |
| 增量源的窗口上限 | Sina `datalen=1023` | `tdxfeed/sources/sina_source.py:19,22-24` |
| 补历史不会引入口径污染 | baostock `adjustflag="3"` 拉 sh600000 得 6530 根（1999-11-10→2026-10-23），与本地重叠 1033 日 close **最大相对误差 0.00e+00** | 一次性探针 |
| 因子事件表成本 | 20 只 → 6.1s（0.31s/只），零失败；外推 5221 只串行 **≈27 分钟** | `bs.query_adjust_factor` pilot |
| 现役价格模型的重训成本 | 1,071,227 训练 / 113,892 测试，29 维，**17–19 分钟，CPU ~3GB** | `docs/V9R_CKPT1000_ASSESSMENT_2026-10-19.md:100-108` |
| 可复现基线 | daban OOS AUC **0.7478**（raw 口径）；对照 0.7602 / 0.7459 在盘 | `docs/RERUN_PLAN_2026-10-19.md:103`、`data/double_up/daban_train_report*.md` |

### 1.1 交付批次（每批独立可停，后一批不依赖前一批已落盘）

| 批 | 内容 | 触什么 | 停止点判据 |
|---|---|---|---|
| P1 | 因子事件表 + `tdxfeed/adjusted.py` 派生层 + 守卫 | 只**新增**文件与 `data/adj_factor_events.parquet`，不碰 raw、不碰消费者 | 验收 §8 第 1、2 条过；此时线上行为一字未变，可安全搁下 |
| P2 | writers 原子写 + 98 只候选的 raw 补历史 | 写 `data/parquet/daily/`（需先备份，且与 18:00 timer 抢窗口） | 验收 §8 第 3 条过；补坏了可用备份整目录还原 |
| P3 | `features_5d` 逐维分派 + daban 双跑对照 | 改特征实现、产新模型文件 | 验收 §8 第 4、5 条过；**线上模型不切**，切与不切由对照结果另决 |

P1 做完就能停下来评估价值——如果派生层现算的收益率已经够用，P2/P3 可以无限期推迟；反过来 P3 依赖 P1，但不依赖 P2（补历史只影响可用样本长度）。

## 2. 架构：两层权威 + 一个读侧咽喉

```
baostock query_adjust_factor ──► data/adj_factor_events.parquet   ← 因子权威（append-only）
data/parquet/daily/*.parquet                                    ← 价格权威（raw，不改写历史）
                    └──────────► tdxfeed/adjusted.py: frame(symbol, kind)  ← 唯一咽喉
                                            ├─► tdxfeed/features_5d.py（训练与预测共用）
                                            └─► quant/multifactor / plan_e_backtest（后续单独决定）
```

**修正一处我先前说过的话**：原计划「后复权落盘成 `daily_hfq/`」作废，改为**派生价格不落盘**。理由是本次要治的病恰恰是「`adj_factor` 固化后过期 21 天」——再落一张派生表就是再造一个会过期的权威。后复权虽可 append，但事件表本身已经是 append-only 的权威，派生值现算只多一次 `searchsorted`，成本可忽略。

### 2.1 `tdxfeed/adjfactor.py`（新，因子层）

纯逻辑 + 一次网络。对外只有四个名字：

- `events_path(root) -> Path` —— `data/adj_factor_events.parquet`
- `fetch_events(code, *, timeout) -> list[Event]` —— `Event(code, ex_date, fore_factor, back_factor)`；`code` 用 baostock 的 `sh.600000` 形
- `load_events(root) -> dict[code6, list[Event]]` —— 一次读全表，按 code 分组，供 `frame()` 用
- `factor_asof(events, date, kind) -> float` —— 阶梯取值：`kind="hfq"` 取该日已生效的 `back_factor`，`kind="qfq"` 取 `fore_factor / fore_factor(anchor)`

`data/adj_factor/*.parquet`（旧日频表）**退役为只读**，不删、不再刷新，仅继续服务它现有的两个消费者（`scripts/clean_double_up_detail.py:60-69`、`scripts/find_double_up_c_v2.py:157`），本设计不改这两个脚本。

### 2.2 `tdxfeed/adjusted.py`（新，派生层，唯一咽喉）

```python
frame(symbol: str, *, kind: str = "raw", root: Path) -> pd.DataFrame
```

- `kind="raw"` 直接返回 `data/parquet/daily/{symbol}.parquet`，**一字不改**（涨停价、封板判定走这条）。
- `kind="hfq"` 返回同形状，但 `open/high/low/close` 各乘当日 `back_factor` 阶梯；`volume/amount/date` 不动。
- `kind="qfq"` 同上但用 `fore_factor / fore_factor(表内末日)`，且**只在读侧存在**，函数文档必须写明「qfq 随新除权整体平移，任何把它固化的做法都是错的」。
- 无事件 → 因子恒 1.0，`hfq` 逐值等于 `raw`（这条由测试锁住，是「无除权股派生无代价」的依据）。
- 缺因子文件 → 返回 `raw` 并在返回的 `df.attrs["factor_missing"] = True` 上留痕；**不静默降级**。

任何消费者不许自己拼因子。这是本设计唯一的接口约束，也是防止重演「训练 raw / 预测 qfq」的那个约束。

### 2.3 `features_5d.py` 的逐维分派

判据一条：**要拿去和真实报价/成交价对表的量 → raw；量本身就是比率的 → hfq。**

- 切 `hfq`：MA 比值、收益率、波动率、动量、gap（open/prev_close）、RSI/MACD 类。
- 留 `raw`：涨停价与封板（`tdxfeed/daban/limit_price.py:144-157` 四舍五入到分）、9.5% 阈值类判定（`scripts/plan_e_backtest.py:233`）、一切与委托价同表的字段。
- 新增不变量守卫：`features_5d` 内**不得出现裸 `close`/`prev_close` 引用**，每个价格型输入必须显式来自 `frame(kind="raw")` 或 `frame(kind="hfq")`。这条是设计落没落地唯一可机械检查的形式。

训练侧与预测侧本来就共用 `compute_window_features_extended`（`scripts/train_daban_model.py:31-35` 与 `scripts/predict.py:166` 同一函数），所以口径只改一处即同源，不存在分叉——这是第三档能做的结构性原因，也是 `features_5d.py:3-5` 那条教训的正面实现。

## 3. 补全历史

范围：**98 只候选**（107 减去 8 只指数与 1 只末日停在 2009 的历史遗留）。指数在 baostock 无覆盖、本环境 mootdx 探测不可用（今晚同步日志实测「mootdx 探测不可用，全程走 Sina 兜底」），所以指数全历史是**已知非目标**，见 §9。

封顶带不能只凭行数断定「被截断」——2022 年下半年上市的票本来就正好有约 1000 根。判据由作业自己给出，不另找上市日源：**baostock 拉到的最早日不早于本地最早日 ⇒ 无增量，跳过且不写一个字节**。实补只数因此是作业的报告项，不是事前断言。

流程：只取**早于本地最早日**的行；合并前先做重叠区对账。

```
for sym in truncated:
    new = baostock_raw(sym, 1990-01-01)
    overlap = new ∩ local
    maxrel = max(|new.close - local.close| / local.close)   # 实测 0.00e+00
    if maxrel >= 0.005: 拒绝整只，记入 rejected 清单，不写一个字节
    else: 只把早于 local.date.min() 的行并入，走 merge_write
```

0.5% 这道门禁是整个设计的承重墙：本仓库现成的两个长历史通道都写死前复权（`tdxfeed/sources/akshare_source.py:12-14`、`tdxfeed/sources/tencent_source.py:17,28`），顺手用它们就是把 qfq 灌进 raw 文件，让同一只票内部口径分裂——比现在更糟且不可逆（`data/` 在 gitignore 里）。

## 4. 落盘纪律

1. **原子写**：`tdxfeed/writers.py:119` 现在是就地 `pq.write_table(table, p)`，本次要一次触碰 5000+ 文件，中断即坏文件。改成写同目录临时文件再 `os.replace`，照仓库内既有先例 `tdxfeed/amount_fix.py:55-57`。
2. **备份**：动 `data/adj_factor/` 与批量改 `data/parquet/daily/` 前各留一份带日期快照（`data/adj_factor.bak-20261026/` 等）。git 救不回来，这是唯一退路。
3. **可续传**：补历史与因子下载都按「已存在即跳过」推进，进度写 `data/.state/adj_events.json`，中断后重跑不重复劳动。

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
| 18:00 `tdxfeed-sync.timer` 写 `data/parquet/daily/` | 补历史要么 18:00 前收工，要么在此期间 `systemctl --user stop tdxfeed-sync.timer`，跑完立即 `start`。**停生产 timer 需用户授权** |
| baostock 可用性 | 见 §7：会整段挂死，不能假设可用 |

## 7. 失败模式

| 失败 | 行为 |
|---|---|
| baostock 挂死（**已实测**：08:44 同参数成功 6530 根，09:05 起含「近4年」在内的所有 `query_history_k_data_plus` 挂死 >100~540s，`login` 仍成功） | 每次调用带 30s 硬超时（沿用本仓库已有的 30s 读超时预算）；**连续 5 只**超时即整轮中止并保留进度，退出码非 0，不当作「数据到此为止」。因子表刷新与补历史共用这条 |
| 重叠区对账不过 | 该只**整只拒绝**，写 rejected 清单，绝不部分写入 |
| 股票无除权事件 | 因子恒 1.0，派生逐值等于 raw（守卫锁死） |
| 事件表缺该 code | 派生退回 raw 并在 `attrs` 留痕，日志打一行；不静默 |
| 大批量写中断 | 已改原子写 ⇒ 最坏情况是旧文件完好，不会出现半写 parquet |
| 新除权发生在因子表之后 | 因子表按事件存储，`factor_asof` 只取「已生效」的阶梯 ⇒ 后复权旧值永不变，无前视；前复权按当日锚定现算，天然不过期 |
| `features_5d` 漏改一处裸 close | 不变量守卫直接红，CI 挡下 |

## 8. 验收标准

1. `daily close / 事件表回算的 raw` 对任意一只**有除权事件**的票，逐日误差 0（即派生层没有反向污染 raw）。
2. 后复权序列在每个除权日的相邻比值 ≠ 跳空：对 600519 最后一次事件（实测 2026-06-26）前后各取 5 日，`hfq` 收益率连续、`raw` 出现台阶 —— 这条是「复权真的起作用」的正向证据。
3. 补历史后：每只候选要么行数增加、要么被 §3 的「无增量」判据正确跳过；`date.min()` 前移到 baostock 的最早日；重叠区一字不变；拒绝清单与「无增量」清单分开可列。
4. 全量 pytest 绿 + `venv/bin/ruff check` 干净 + 不变量守卫（无裸 close）通过。
5. 双跑对照报告：同日子 raw 组 AUC 与历史 0.7478 的差 = 「非口径因素」的漂移量；hfq 组与它的差 = 口径效应。两个数都要报出来。

## 9. 非目标

- 不改写 `data/parquet/daily/` 的价格语义（raw 仍是落盘权威）。
- 不做指数全历史（源不具备）。
- 不动 stage1/vpnl 链：它是四段缓存（`d_signal_5d_features.csv` → `_v9.csv` → `_vpnl.csv` → `stage1_wide_table.parquet`），`_v9.csv` 已不在盘，且 stage1 无可复现基线——没有对照就不重训。
- 不为 LLM 侧重蒸：`scripts/build_distill_skeleton.py:43-50` 确实把 OHLCV 原值写进 prompt，但 LoRA 未接入线上（predict/plan_e 不加载）。设计里只标注「将来接入需重蒸」。
- 不改 `clean_double_up_detail.py` / `find_double_up_c_v2.py`（旧因子表的仅存消费者）。
- 不切换线上模型。

## 10. 守卫清单（TDD，RED 先行）

| 文件 | 锁住什么 |
|---|---|
| `tests/test_adjfactor_events.py` | 事件表阶梯单调；`factor_asof` 在除权日前后各取到正确的段；qfq 锚定 = 末日时因子为 1.0；无事件恒 1.0 |
| `tests/test_adjusted_frame.py` | `raw` 与磁盘逐字节一致；`hfq` 旧值不随新事件变化（append-only 不变量）；缺因子退回并留痕；qfq 不落盘 |
| `tests/test_daily_backfill_gate.py` | 重叠区误差 ≥0.5% 时整只拒绝且零写入；只并入早于本地最早日的行；行数只增不减 |
| `tests/test_writers_atomic.py` | 写盘中途抛异常时原文件完好（tmp + `os.replace`） |
| `tests/test_features_5d_no_bare_close.py` | 扫源码，`features_5d.py` 内不得出现裸 `close`/`prev_close` 引用 |
