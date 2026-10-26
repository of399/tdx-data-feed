# daban 执行研究线总览（Stage 0 → Stage 1 → Stage 2）

（2026-10-26 整理入库 · 对应提交 `e5d60f0` / `c0b37dd` / `c9e2e01` / `b3f1274`）

> **定位**：把「模型给出高概率」进一步拆成「**能不能成交 → 成交后会不会封住 → 封住后赚不赚**」三层，逐层用数据证伪。
> **边界**：**纯研究 / 诊断**。不下单、不连券商、不写实盘账本、不改生产模型、不覆盖生产数据。
> **当前结论**：**Stage 1 门禁未通过、Stage 2 `overall_go=false`** ⇒ 本线**不支持前向纸盘与任何资金投放**，只保留为研究与回归基线。

---

## 📊 核心指标（20261022 口径，数据落 `data/daban/`）

| 层 | 指标 | 值 |
|---|---|---|
| Stage 0 事件表 | 触板事件 | **21,963** / 256 交易日 / 3,742 只（2025-09-05 ~ 2026-09-24）|
| Stage 0 事件表 | 过滤口径 | 触板 & 非开盘涨停 & 非 ST & 非新股（总体封住率 0.704）|
| Stage 1 封板分类 | 验证集 AUC | logistic **0.5431** / gbm 0.5405（基线 0.6946）|
| Stage 1 封板分类 | **测试集 AUC** | logistic **0.5216** / gbm **0.5279**（**接近随机**）|
| Stage 1 封板分类 | 阈值 0.8659 | precision 0.791（CI 0.738~0.836）、recall 仅 **0.069**、6.92 events/day |
| Stage 1 门禁 | `gate.passed` | ❌ **false**（目标 precision 0.80 / ≥1 event/day / ≥100 events）|
| Stage 2 成交模型 | AUC / Brier | 0.592 / 0.251；**基础成交率 46.6%** |
| Stage 2 baseline | 4,133 信号 / 52 日 | 成交率 46.6%，**平均净收益 -1.30%/笔**，胜率 32.4%，毛收益 -1.15% |
| Stage 2 分化 | 封住收盘 vs 未封住 | 封住 **+1.24%** / 未封住 **-2.89%** ⇒ 分化真实存在 |
| Stage 2 top1 | 52 信号 | 成交率 34.6%，平均净收益 **+0.22%**，胜率 44.4%（样本极小，不作结论）|
| Stage 2 终局 | `overall_go` | ❌ **false**（Stage 1 未过 ⇒ Stage 2 仅诊断）|

---

## 1. 三阶段流程

```
Stage 0  分钟级触板事件表          Stage 1  封板概率分类器         Stage 2  成交感知回测
───────────────            ───────────────────────         ─────────────────────
daban_min60_features  →    daban_seal_research        →    daban_exec_research
daban_event_table          seal_classifier.py               execution_backtest.py
（特征与标签严格分离）      （38 维 + isotonic 校准）        （成交感知 + 闸门判定）

                     旁路：daban_orderbook_logger（盘中盘口快照）
                           daban_paper（cb≥3 接力纸盘账本）
                           risk_gateway（风险闸门 · 休眠）
```

### Stage 0 · 分钟级触板事件表
- **做什么**：把日线换成分钟级，找「首次触板」的 K 线作为信号，**特征只用触板当刻及之前的数据**（标签在触板之后）——避免未来函数。
- **产物**：`data/daban/min60_features_20261022.parquet`（123 MB）、`min60_market_20261022.parquet`、`event_table_min60_20261022.parquet`（7.2 MB）+ `.meta.json`。
- **纪律**：`daban_event_table.py:11-16` 明确写死「特征/标签分离」，这是本线最有价值的部分。

### Stage 1 · 封板概率分类器
- **做什么**：给定「已触板」，预测「能否封住收盘」。38 维特征（`SEAL_FEATURES`）+ isotonic 校准 + Wilson 区间 + 阈值选择（目标 precision ≥ 0.80、≥1 event/day、≥100 events）。
- **切分**：按**交易日顺序** train 9,956 / calib 3,179 / val 4,326 / test 4,502（不随机打散，避免同日泄漏）。
- **产物**：`data/daban/seal_classifier_20261022.json`、`seal_model_20261022.joblib`、`seal_scores_20261022.parquet`。
- **结论**：验证集 AUC 0.543 → 测试集 0.522，**判别力在时间外推中衰减到接近随机**；`gate.passed=false`。

### Stage 2 · 成交感知回测
- **做什么**：把「信号日买不进」显式建模——信号 → 次日开盘成交（跳空按开盘成交）→ 持仓到卖出（SL 优先）。
- **产物**：`data/daban/execution_backtest_20261022.json`（含 `diagnostic_only=true`、`stage1_gate_passed=false`、`overall_go=false`）。
- **结论**：baseline 平均净收益 **-1.30%/笔**（毛收益 -1.15%）⇒ **负期望来自入场时点，不是交易成本**；成交率仅 46.6%，一半信号根本买不进；封住收盘的子样本 +1.24% vs 未封住 -2.89%。

---

## 2. 模块与脚本清单

### 2.1 核心模块（`tdxfeed/daban/`，**不进入 `__init__.__all__`** → 生产导入面零变更）

| 模块 | 职责 | 主要产物 |
|---|---|---|
| `paper.py` | cb≥3 接力纸盘账本（38 列 journal，原子写）| `data/daban/paper/{config.json,journal.csv,signals/,reports/}` |
| `orderbook_log.py` | 盘中盘口快照记录器（57 列，当日追加）| `data/daban/orderbook/orderbook_YYYYMMDD.csv` |
| `seal_classifier.py` | 封板概率分类器 + 阈值选择 + 可靠性表 | 由脚本落 json / joblib |
| `risk_gateway.py` | 交易前风险闸门（15 项限额）| JSON 状态（调用方给路径）|
| `fill_model.py` | 成交流概率模型（brier / log_loss / auc + 每日 TopK 成交率）| 由脚本落 json |
| `execution_backtest.py` | 成交感知回测（`ExecutionConfig`）| 由脚本落 json |
| `topk_pnl.py` | Top-K 逐笔 PnL（T+1 开盘买 → T+2 开盘卖）| 由脚本落 csv |

### 2.2 研究脚本（`scripts/`，全部只读生产数据）

| 主题 | 脚本 |
|---|---|
| 事件表 | `daban_min60_features.py`、`daban_event_table.py` |
| 封板分类 | `daban_seal_research.py` |
| 成交回测 | `daban_exec_research.py`、`backtest_daban_topk_pnl.py`、`report_daban_topk.py`、`daban_limit_topk_eval.py` |
| 稳健性 / 对账 | `daban_relay3_robustness.py`、`daban_relturn_validation.py`、`daban_filter_oos.py`、`daban_formula1_screen.py`、`daban_formula1_validate.py`、`daban_band_check.py` |
| 其他专题 | `daban_intraday_ic.py`、`daban_min60_exec.py`、`trend_hold_eval.py`、`hengpan_t1_eval.py`、`eval_daban_pattern.py` |
| 盘口 / 纸盘 | `daban_orderbook_logger.py`、`daban_paper.py` |

### 2.3 证据报告（`docs/DABAN_*.md`）

`DABAN_EXEC_RESEARCH_20261022` · `DABAN_TOPK_PNL_20261022` · `DABAN_TOPK_REPORT_20261022` · `DABAN_MIN60_EXEC_20261022` · `DABAN_INTRADAY_IC_20261022` · `DABAN_RELAY3_ROBUSTNESS_20261022` · `DABAN_FILTER_OOS_20261022` · `DABAN_SALVAGE_20261022` · `DABAN_LIMIT_TOPK_20261022` · `DABAN_PAPER_TRADE_RULES_20261022`

---

## 3. 与生产链路的边界（必须保持）

| 关系 | 结论 | 证据 |
|---|---|---|
| 新代码 → 既有模块 | 单向依赖，只读复用 | `topk_pnl.py:10` 复用 `tdxfeed.features_5d`；`mcp/servers/backtest.py:56-60` 与 `predict.py` 同源算 29 维 |
| 写生产模型 | ❌ 无 | 对 `data/double_up/d_signal_5d_model_daban.txt` 只有 `lgb.Booster(model_file=...)` 读 |
| 改生产日线 | ❌ 无（研究脚本全只读 `data/parquet/daily`）| 唯一写生产日线的是 `tdxfeed/amount_fix.py`，且已由 `writers`/`incremental` 在**写入时**补缺口，不是研究脚本 |
| 写 `data/double_up/` | ❌ 已整改 | `eval_daban_pattern.py` 产物改到 `data/daban/`（原先会往生产目录写 188 MB 快照）|
| 下单 / 连券商 | ❌ 无 | 全量 grep 无券商 API 命中；`daban-ticket.service` 自述「本单元不下单」|
| `tdxfeed/daban/__init__.__all__` | 未新增导出 | 生产 import 面**零变更**，新模块只能显式 `from tdxfeed.daban.paper import ...` |

---

## 4. 口径纪律（复用即必须遵守）

1. **诚实成交口径**：信号日 T 收盘触发 → **T+1 开盘入场**（跳空按开盘价成交，不假设能按信号价买到）→ 持仓至卖出，**SL 优先于 TP**。
2. **成交率必须单独报**：本线 baseline 成交率仅 **46.6%** ⇒ 「模型排序好」和「能买到」是两件事，合并统计会失真。
3. **负期望先归因入场时点，再谈成本**：baseline 毛收益 -1.15% / 净收益 -1.30% ⇒ 成本只占小头，主要亏损来自**入场即跌**。
4. **子样本分化要单列**：封住收盘 +1.24% vs 未封住 -2.89% ⇒ 「触板后能否封住」是有效区分维度，但 Stage 1 分类器本身判别力不足（test AUC 0.52）。
5. **不使用未来信息**：Stage 0 的特征只能取触板当刻及之前；Stage 1 按交易日顺序切分，不随机打散。
6. **小样本不下结论**：Stage 2 top1 仅 52 信号 / 18 笔成交，统计意义不足，报告中只作观察项。

---

## 5. 安全约定（2026-10-26 加固，已入库）

| 风险 | 处置 | 位置 |
|---|---|---|
| 成交额回填原地覆盖生产日线 + 重写 `.day` | **默认 dry-run**，落盘须显式 `--apply`；落盘前逐文件备份到 `data/parquet/daily_amount_bak/` | `scripts/backfill_daily_amount.py`、`tdxfeed/amount_fix.py` |
| 研究产物污染生产目录 | 产物统一落 `data/daban/`，不再写 `data/double_up/` | `scripts/eval_daban_pattern.py` |
| skill 被误当买入信号 | skill 描述 + MCP `warnings` 均标注「只作排序研究，不是买入指令（-0.78%/笔，overall_go=false）」| `skills/daban.yaml`、`mcp/servers/backtest.py` |
| 纸盘账本被误当实盘 | 账本落 `data/daban/paper/`，`init --force` 才覆盖；实盘建仓仍只走人工 `--mode buy` | `tdxfeed/daban/paper.py`、`scripts/daban_paper.py` |
| 高频轮询新浪被限流 | `--interval` 默认 3s，脚本头自警「与 `daban_scan.py --watch` 二选一」| `scripts/daban_orderbook_logger.py:25-27` |
| 空文件入库 | pre-commit 新增 `no-zero-byte-files` 钩子 | `.pre-commit-config.yaml` |

---

## 6. 已知缺陷与待办（按 ROI）

| 优先级 | 项 | 说明 |
|---|---|---|
| P0 | **持仓回流**：`daban_paper.py` 与委托单成交未闭环 | 「下单建议 → 实际成交 → 归因」目前不可审计 |
| P0 | **`daban-ticket` 运行边界** | 午休 11:30-13:00 是否产单、尾盘 14:00 后是否覆盖，待确认 |
| P1 | **重复代码抽取** | `net_return`（两套口径）、`_auc`、`load_universe`、markdown 表格助手各有多份；应收到**拟新增**的 `tdxfeed/daban/_util.py`（当前不存在） |
| P1 | **硬编码日期** | 多数脚本顶部写死 `20261022` 常量，跨月重跑需改代码 → 参数化为 `--date/--tag` |
| P1 | **缺 logging** | `tdxfeed/daban/` 全包 0 处 `import logging`，盘中长跑只有 stdout；应改 `logging` + 落盘轮转 |
| P1 | **吞错** | 研究脚本共 84 处 `except Exception`，样本缺失会静默少样本 → 至少统计级错误要计数上报 |
| P2 | **报告结论硬编码** | `daban_min60_exec.py` 的 `build_report` 把结论正文写死，换数据重跑正文不更新 → 结论由代码填充 |
| P2 | **超大函数** | `build_report` 265 行、`backtest_daban_topk_pnl.main` 264 行、`daban_event_table.build` 213 行 → 拆分 |
| P2 | **北交所口径不一致** | `daban_min60_features._code_prefix` 只处理 sh/sz，未处理 `bj` 前缀，与 `scanner.daily_path` 口径不一致 |

---

## 7. 复现命令

```bash
# Stage 0 事件表（分钟级特征 → 事件表）
venv/bin/python scripts/daban_min60_features.py
venv/bin/python scripts/daban_event_table.py

# Stage 1 封板分类（落 json + joblib + scores）
venv/bin/python scripts/daban_seal_research.py

# Stage 2 成交感知回测（落 execution_backtest json，含 overall_go 闸门）
venv/bin/python scripts/daban_exec_research.py

# TopK PnL 与报告
venv/bin/python scripts/backtest_daban_topk_pnl.py
venv/bin/python scripts/report_daban_topk.py

# 盘中盘口采集 / 纸盘（研究用，不下单）
venv/bin/python scripts/daban_orderbook_logger.py --interval 3
venv/bin/python scripts/daban_paper.py --help

# 收盘后只想要 Top-N 候选（不落盘完整预测）
# → 用 daban skill：mcp:tdx-backtest.daban_top_picks
```

> 复现前请确认数据日期：多数脚本带 `20261022` 硬编码常量，跨期复跑需先改常量或参数化。

---

## 8. 一句话总结

**执行研究线把「daban 模型次日涨停概率」拆成可证伪的三层（能不能买 / 买后能不能封 / 封了赚不赚），结论是：模型排序在**买不进的一半信号**上无法兑现（成交率 46.6%），封板分类器时间外推接近随机（test AUC 0.52），诚实成交口径下平均净收益 -1.30%/笔 ⇒ `overall_go=false`，本线只作研究与回归基线，不支持任何资金投放。**

---

生成时间：2026-10-26 18:20:00
生成者：CodeBuddy（执行研究线整理入库）
数据日期：20261022（研究口径）/ 2026-10-26（整理入库）
