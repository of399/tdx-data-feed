# 方案 E 实盘启动指南

> 生成：2026-09-27 · 启动日期：2026-09-28（周一，A股开盘）
> 配套脚本：`scripts/plan_e_journal.py`
> 数据源：`scripts/plan_e_backtest.py` + 历史翻倍样本

---

## 1. 系统配置

| 参数 | 值 | 说明 |
|---|---|---|
| **初始资金** | 100,000 RMB | 小资金验证 |
| **单股仓位** | 10% | 10 只满仓 |
| **最大持仓** | 10 只 | 同时持仓上限 |
| **止盈 TP** | +30% | 期内最大涨幅 |
| **止损 SL** | -10% | 期内最大跌幅 |
| **时间止损** | 10 个交易日 | 持有期满 |
| **入场价** | T+2 开盘价 | 避开 T+1 涨停 |
| **触发条件** | 翻倍 + T+1 涨停 + 量比 ≥ 1.5 | v2 增强版 |

---

## 2. 信号触发时间线

```
[T 日 09:25~09:30]   集合竞价：观察翻倍股开盘价
[T 日 09:30~15:00]   交易时段：翻倍股全天可能继续涨停
[T 日 15:00]         收盘：确认 T 日累计涨幅首次 ≥ 100%
[T+1 日 09:30~15:00] 关键观察日：
                     - T+1 单日涨幅 ≥ 9.5%？
                     - T+1 量比 ≥ 1.5？
[T+1 日 15:00]       信号确认：方案 E 触发条件成立
[T+2 日 09:30]       入场日：开盘价买入（避开 T+1 涨停风险）
[T+2 ~ T+12]         持有期：监控 TP / SL / TIME
```

---

## 3. 每日工作流程

### 3.1 信号生成（每日 18:30 自动化）

```bash
# 已由 predict-daily.timer 配套：每日 18:05 跑方案 D 预测
# 方案 E 信号需要单独触发（建议加 timer 18:10）

# 手动触发今日信号
venv/bin/python scripts/plan_e_journal.py --mode daily
```

### 3.2 开盘前检查（每个工作日 09:00）

```bash
# 1. 查看今日方案 D 预测 Top 50
cat data/predictions/predict_latest.md | head -40

# 2. 查看今日方案 E 触发信号
venv/bin/python scripts/plan_e_journal.py --mode daily

# 3. 查看当前持仓
venv/bin/python scripts/plan_e_journal.py --mode status
```

### 3.3 开盘买入（09:30 ~ 09:35）

```bash
# 对每个触发信号：
# 1. 开盘价 ≤ 信号价 1.5% 以内 → 买入
# 2. 开盘价 > 信号价 1.5%        → 放弃（避免追高）

# 买入示例：开盘价 10.50，买 1 万 RMB（1000 股）
venv/bin/python scripts/plan_e_journal.py --mode buy \
    --code 600519 --price 10.50 --qty 1000
```

### 3.4 盘中监控（09:30 ~ 15:00）

每个交易日监控持仓：
- 期内涨幅 ≥ +30% → 触发 TP，**立即卖出**
- 期内跌幅 ≤ -10% → 触发 SL，**立即卖出**
- 持有满 10 日 → 时间止损，**当日卖出**

### 3.5 收盘卖出（任意时间）

```bash
# 卖出示例：触发 TP
venv/bin/python scripts/plan_e_journal.py --mode sell \
    --code 600519 --price 13.65 --reason TP

# 卖出示例：触发 SL
venv/bin/python scripts/plan_e_journal.py --mode sell \
    --code 600519 --price 9.45 --reason SL

# 卖出示例：时间止损
venv/bin/python scripts/plan_e_journal.py --mode sell \
    --code 600519 --price 11.20 --reason TIME
```

### 3.6 周报（每周五 20:00）

```bash
# 生成周报
venv/bin/python scripts/plan_e_journal.py --mode weekly
```

---

## 4. systemd timer 集成（可选）

方案 E 实盘跟踪也可以加 systemd timer（每日 18:10 自动跑 daily 模式）：

```bash
# /home/jiuben/.config/systemd/user/plan-e-journal.timer
[Unit]
Description=方案 E 每日信号归档

[Timer]
OnCalendar=Mon..Fri 18:10:00
Persistent=true
AccuracySec=60s
Unit=plan-e-journal.service

[Install]
WantedBy=timers.target
```

```bash
# /home/jiuben/.config/systemd/user/plan-e-journal.service
[Unit]
Description=方案 E 每日信号生成
After=predict-daily.service

[Service]
Type=oneshot
WorkingDirectory=/home/jiuben/tdx-data-feed
ExecStart=/home/jiuben/tdx-data-feed/venv/bin/python scripts/plan_e_journal.py --mode daily
StandardOutput=append:/home/jiuben/tdx-data-feed/logs/plan_e_journal.log
StandardError=append:/home/jiuben/tdx-data-feed/logs/plan_e_journal.err

[Install]
WantedBy=timers.target
```

启用：

```bash
cp systemd/plan-e-journal.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable plan-e-journal.timer
systemctl --user start plan-e-journal.timer
```

---

## 5. 风险控制清单

### 5.1 开仓前必查

- [ ] 翻倍首次触发日已确认（peak_date）
- [ ] T+1 单日涨幅 ≥ 9.5%
- [ ] T+1 量比 ≥ 1.5
- [ ] 当前持仓 < 10 只
- [ ] 现金足够买入 1 单（≥ 1 万）
- [ ] 非 ST 股、非停牌股
- [ ] 大盘环境：上证/创业板 MA20 上方

### 5.2 持仓中监控

- [ ] 每日收盘后查看持仓价 vs 买入价
- [ ] 期涨幅 ≥ +28%（接近 TP）→ 准备卖单
- [ ] 期跌幅 ≤ -8%（接近 SL）→ 准备卖单
- [ ] 持有 ≥ 7 日（接近 TIME）→ 评估是否继续

### 5.3 紧急止损

| 场景 | 动作 |
|---|---|
| 个股停牌 | 立即评估，复牌后退出 |
| 大盘崩盘（>5%） | 全部止损清仓 |
| 个股黑天鹅（业绩暴雷/财务造假） | 立即清仓 |
| 政策风险（行业整顿） | 立即清仓 |

---

## 6. 验证记录

### 6.1 历史回测（方案 E v2）

| 指标 | 值 |
|---|---|
| 时段 | 2020-01 ~ 2026-09 |
| 样本 | 1942 首次翻倍股 → **27 笔可入场** |
| **胜率** | **55.6%** |
| **平均收益** | **+12.22%** |
| 盈亏比 | 3.00 |
| 最大回撤 | -44.04% |

### 6.2 1 个月模拟验证（2026-08-27 ~ 2026-09-25）

| 指标 | 值 |
|---|---|
| 初始资金 | 100,000 |
| 期末资金 | **100,451** |
| **总盈亏** | **+451 (+0.45%)** |
| 总交易 | 3 笔 |
| 胜率 | 33.3% (1 胜 2 负) |
| 平均收益 | +3.33% |

**模拟交易明细**：

| 日期 | 股票 | 操作 | 价格 | 数量 | 盈亏 | 原因 | 持有 |
|:---:|:---:|:---:|---:|---:|---:|:---:|---:|
| 09-02 | 920087 | BUY | 19.71 | 500 | - | ENTRY | 0 |
| 09-03 | 920895 | BUY | 26.96 | 300 | - | ENTRY | 0 |
| 09-10 | 920087 | SELL | 17.74 | 500 | -985.5 (-10%) | **SL** | 8 |
| 09-10 | 920895 | SELL | 35.05 | 300 | +2426.4 (+30%) | **TP** | 7 |
| 09-10 | 920895 | BUY | 32.99 | 300 | - | ENTRY | 0 |
| 09-11 | 920895 | SELL | 29.69 | 300 | -989.7 (-10%) | **SL** | 1 |

---

## 7. 实盘 1 个月计划（2026-09-28 ~ 2026-10-28）

### 7.1 阶段目标

| 周次 | 时间 | 目标 |
|---|---|---|
| 第 1 周 | 09-28 ~ 10-02 | 熟悉流程 + 验证信号 + 积累 5+ 笔交易 |
| 第 2 周 | 10-09 ~ 10-15 | 优化入场节奏，胜率 > 50% |
| 第 3 周 | 10-19 ~ 10-23 | 持仓规模从 10 万扩到 30 万（如胜率稳定）|
| 第 4 周 | 10-26 ~ 10-28 | 周报汇总 + 评估是否继续 |

### 7.2 评估标准

**继续**（满足任一）：
- 月胜率 ≥ 50%
- 月盈亏 ≥ +5%
- 平均盈亏比 ≥ 2.0

**暂停**（满足任一）：
- 月胜率 < 35%
- 月亏损 ≥ -10%
- 单股最大亏损 > -20%

### 7.3 关键指标

| 指标 | 目标 | 计算 |
|---|---|---|
| 信号命中率 | > 1%/日 | 触发数 / 全市场翻倍触发数 |
| 胜率 | ≥ 50% | 止盈笔数 / 总笔数 |
| 平均收益 | ≥ +8% | 总盈亏 / 总笔数 |
| 盈亏比 | ≥ 2.0 | 平均盈利 / 平均亏损 |
| 最大回撤 | ≤ 20% | 累计净值最大跌幅 |

---

## 8. 常用命令速查

```bash
# === 信号 ===
venv/bin/python scripts/plan_e_backtest.py --today    # 当日信号
venv/bin/python scripts/plan_e_backtest.py            # 完整回测
venv/bin/python scripts/predict.py                    # 方案 D 预测

# === 实盘操作 ===
venv/bin/python scripts/plan_e_journal.py --mode daily    # 每日归档
venv/bin/python scripts/plan_e_journal.py --mode status   # 当前状态
venv/bin/python scripts/plan_e_journal.py --mode buy --code XXX --price Y --qty Z
venv/bin/python scripts/plan_e_journal.py --mode sell --code XXX --price Y --reason TP|SL|TIME
venv/bin/python scripts/plan_e_journal.py --mode weekly   # 周报

# === 模拟验证 ===
venv/bin/python scripts/plan_e_journal.py --mode simulate \
    --start 2026-08-27 --end 2026-09-25 --capital 100000

# === 查看历史 ===
cat data/plan_e/live/journal.csv
ls data/plan_e/live/weekly_reports/
```

---

## 9. 文件位置

```
data/plan_e/live/
├── journal.csv                 # 所有交易流水
├── positions.json              # 当前持仓
├── daily_signals/              # 每日信号归档
└── weekly_reports/             # 周报

logs/
├── predict.log                 # 方案 D 预测日志
└── plan_e_journal.log          # 方案 E 信号日志
```

---

## 10. 一句话总结

**方案 E 实盘 = 翻倍 + 涨停接力 + 跟随 10 日**
- 启动日期：2026-09-28（周一）
- 初始资金：10 万
- 评估期：1 个月
- 关键动作：每日 09:00 检查信号 → 09:30 开盘买入 → 盘中监控 TP/SL → 周日生成周报

> 下次评估：2026-10-28 周一，根据 4 周实盘数据决定是否继续。

---

## 11. 信号驱动回测（方案 B · 2026-10-16 落地）

> 本章配套 `docs/PROPOSAL_B_INTERFACES_2026-10-16.md` 与 `docs/ROADMAP_2026-10-16.md` §方案 B。
> 用途：把方案 D（predict.py 每日预测）与方案 E（plan_e_backtest + plan_e_journal）**端到端打通**，
> 不再需要手动复制代码列表。

### 11.1 三组件数据流

```
predict.py --emit-top  →  data/predictions/predict_*.csv
                                ↓ (mtime 最新)
plan_e_journal.py --from-predict-list  →  journal.csv / journal_sim.csv
                                ↓ (Top 50 / 阈值 0.7 过滤)
plan_e_backtest.py --codes-file codes.csv  →  data/plan_e/plan_e_*.md
```

### 11.2 完整使用流程

```bash
# 步骤 1: 每日预测 + 额外写一份 Top 50 到 data/signals/（与默认输出叠加）
venv/bin/python scripts/predict.py --emit-top

# 步骤 2: 模拟端消费最新 predict（写入 journal_sim.csv）
venv/bin/python scripts/plan_e_journal.py --mode simulate --from-predict-list

# 步骤 3: 实盘端消费最新 predict（写入 journal.csv, paper-trading, 走 positions 管理）
venv/bin/python scripts/plan_e_journal.py --mode real --from-predict-list

# 步骤 4: 用 predict 输出 codes 写 codes_file.csv → 子集回测
head -1 data/predictions/predict_*.csv  # 取最新文件的 code/score 列
awk -F, 'NR>1 {print $1","$2}' data/predictions/predict_<latest>.csv > /tmp/codes.csv
# (加 header: code,score)
venv/bin/python scripts/plan_e_backtest.py --codes-file /tmp/codes.csv --today
```

### 11.3 codes-file 格式契约

| 字段 | 必填 | 说明 |
|---|---|---|
| 第 1 行 header | ✅ | 首列名必须是 `code` |
| code 列 | ✅ | 6 位代码（自动去 sh/sz/bj 前缀） |
| comment 行 | 可选 | 以 `#` 开头（可自定义前缀） |
| 空行 | 可选 | 自动跳过 |
| 重复 code | ❌ | **立即 sys.exit(1)**（不容错） |
| 格式错 | ❌ | 立即 sys.exit(1) + 详细错误信息 |

**文件示例**：

```csv
code,name,score
600519,贵州茅台,0.92
000001,平安银行,0.88
300750,宁德时代,0.85
# 这是注释行:仅 Top 3
```

### 11.4 emit-top 输出格式

`--emit-top` 在默认 `data/predictions/predict_*.csv` 输出基础上**额外**写一份到 `data/signals/`：

```
data/signals/predict_YYYYMMDD_HHMMSS_top{50}_thr{0.7}.csv
```

默认字段：`code,score,ts,top_k`（可自定义 `--emit-fields`）。

### 11.5 from-predict-list 默认行为

| 维度 | 默认值 | 来源 |
|---|---|---|
| Top N | 50 | 与 `predict.py --top` 默认放大（journal 想更多信号）|
| 阈值 | 0.7 | 与 `predict.py --threshold` 默认一致 |
| 文件发现 | `data/predictions/predict_*.csv` 按 mtime 倒序 | 自动 |
| 资金分配 | 等额 `INITIAL_CAPITAL / N`（100 股一手取整）| 简化 |
| 实盘/模拟分流 | `--mode real` → `journal.csv` / `--mode simulate` → `journal_sim.csv` | 自动 |

### 11.6 端到端验证记录（2026-10-16）

- **测试 1（--from-predict-list）**：临时 predict 文件（5 只候选，阈值 0.7）→ journal_sim.csv 5 行 ✓
  - code 6 位 zfill 正确（`000001` 保留前导 0）✓
  - source=sim / reason=PREDICT_LIST / score 列就位 ✓
- **测试 2（--codes-file）**：用 5 只真实翻倍样本代码（bj92xxxx）→ 22972 → 105 行过滤 ✓
  - 错误不容错：测试用伪 codes 过滤后 0 行 → `sys.exit(1)` ✓
- **测试 3（ruff + AST）**：4 Python 文件 ruff 0 错误 + AST 校验通过 ✓
- **测试 4（watchdog sanity 集成）**：`bash -n` 语法通过 + run_v4_eval 5 指标输出就位 ✓

### 11.7 注意事项

- ⚠️ **`pd.read_csv` 默认推断 `000001` 为 int**（丢前导 0）——读取 journal.csv / journal_sim.csv 时需显式 `dtype={'code': str}`（`append_journal`/`append_sim_journal` 写入时已强制 str，但旧数据可能已丢前导 0）
- ⚠️ **`--mode real` 是 paper-trading**（模拟真金白银流），不是真交易
- ⚠️ **方案 B sanity 集成仅对 `checkpoint-200`** 触发一次（首次落地的 sanity）；后续 checkpoint 不重复
- ⚠️ **`--codes-file` + 现有批量回测** 是叠加（限定范围）而非互斥
