# 方案 E v3 提前版实盘跟踪操作手册

> **版本**: v1.0  
> **生成时间**: 2026-09-27  
> **配套脚本**: `scripts/plan_e_v3_live_track.py`  
> **策略来源**: `data/QUANT_RESEARCH_REPORT.md`（v3 提前版 + 情绪因子）  
> **回测基准**: 21 月滚动模拟 +31.67% 累计 / 80% 月度胜率

---

## 一、实盘跟踪总流程图

```
每日 18:00 盘后
        ↓
[1] 拉取当日全市场日 K（Tencent/Sina）
        ↓
[2] 重算全市场因子（factor_engine.py --live）
        ↓
[3] 复合打分（Top 10 因子等权 Rank 标准化）
        ↓
[4] 生成 Top 5% 候选池
        ↓
[5] 过滤持仓（已持仓跳过 / 满 10 只跳过）
        ↓
[6] 输出买入建议（含代码、价格、数量、score）
        ↓
[7] 人工确认 → 实际下单（券商 App）
        ↓
[8] 更新持仓 + journal
        ↓
[9] 异常检测（胜率 / 回撤 / IC 衰减）
        ↓
[10] 生成日报 / 周报 / 月报
        ↓
每月最后一日
        ↓
[11] IC 衰减监控（factor_significance.py 重跑）
        ↓
[12] 月度报告（含 P&L / 因子稳定性 / 策略调整建议）
```

---

## 二、每日流程（盘后 18:30 跑）

### 2.1 一键执行

```bash
# 实盘模式（默认）
venv/bin/python scripts/plan_e_v3_live_track.py --mode daily

# 模拟模式（paper trading）
venv/bin/python scripts/plan_e_v3_live_track.py --mode simulate

# 指定日期（回填历史）
venv/bin/python scripts/plan_e_v3_live_track.py --mode daily --date 2026-09-25
```

### 2.2 每日动作清单

| 步骤 | 命令 | 产出 |
|:---:|---|---|
| 1 | 拉取日 K | `data/parquet/daily/*.parquet` |
| 2 | 重算因子 | `data/factors/event_factors_with_sentiment.parquet` |
| 3 | 跑 daily 模式 | `data/plan_e/live_v3/daily_reports/daily_YYYYMMDD.md` |
| 4 | 看 status | `--mode status` |
| 5 | 检查异常 | `--mode anomaly` |

### 2.3 systemd 定时（推荐）

```ini
# /etc/systemd/system/plan_e_v3_daily.service
[Unit]
Description=Plan E v3 Daily Tracking
After=network-online.target

[Service]
Type=oneshot
User=jiuben
WorkingDirectory=/home/jiuben/tdx-data-feed
ExecStart=/home/jiuben/tdx-data-feed/venv/bin/python /home/jiuben/tdx-data-feed/scripts/plan_e_v3_live_track.py --mode daily
StandardOutput=journal
StandardError=journal
```

```ini
# /etc/systemd/system/plan_e_v3_daily.timer
[Unit]
Description=Plan E v3 Daily Timer

[Timer]
OnCalendar=Mon-Fri 18:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now plan_e_v3_daily.timer
systemctl list-timers plan_e_v3*
```

---

## 三、监控指标（5 大类）

### 3.1 收益类

| 指标 | 计算 | 健康范围 | 报警阈值 |
|---|---|---|---|
| **累计收益率** | (总权益 - 起始资金) / 起始资金 | ≥ +5%（30 日）| < 0 |
| **月度收益率** | 本月收益 / 上月末权益 | ≥ +1%（参考回测）| < -2% |
| **年度化收益** | 月化 × 12 | ≥ +12% | < 0 |
| **月度胜率** | 本月盈利笔数 / 本月总笔数 | ≥ 60% | < 50% |
| **盈亏比** | 平均盈利 / 平均亏损 | ≥ 2.0 | < 1.5 |

### 3.2 风险类

| 指标 | 计算 | 健康范围 | 报警阈值 |
|---|---|---|---|
| **最大回撤** | 当前相对历史峰值的跌幅 | ≤ -8% | > -15% |
| **单笔最大亏损** | 单笔最大亏损百分比 | ≤ -10%（强制止损）| > -12% |
| **持仓集中度** | 第一大持仓 / 总权益 | ≤ 5% | > 8% |
| **板块集中度** | 第一大板块 / 总权益 | ≤ 25% | > 40% |
| **波动率** | 日收益标准差 × √252 | ≤ 25% | > 40% |

### 3.3 因子稳定性

| 指标 | 计算 | 健康范围 | 报警阈值 |
|---|---|---|---|
| **RankIC（月度）** | 因子值与 20 日涨幅的 Spearman 相关 | ≥ 0.05 | < 0.03 |
| **IC 衰减** | 本月 IC - 上月 IC | ≥ -0.02（无显著衰减）| < -0.05 |
| **因子单调性** | Q5 - Q1 正样本率差 | ≥ 20pp | < 10pp |
| **Top 因子集中度** | Top 3 因子 IC 占比 | 30-60% | > 80%（过拟合）|

### 3.4 执行质量

| 指标 | 计算 | 健康范围 | 报警阈值 |
|---|---|---|---|
| **滑点** | 实际成交价 - 信号价 / 信号价 | ≤ 1% | > 3% |
| **未成交率** | 信号后未下单数 / 总信号数 | ≤ 5% | > 15% |
| **延迟** | 信号到下单间隔（分钟）| ≤ 5 | > 30 |
| **手续费占比** | 累计手续费 / 累计成交额 | ≤ 0.5% | > 1% |

### 3.5 行为指标

| 指标 | 计算 | 健康范围 | 报警阈值 |
|---|---|---|---|
| **连续亏损** | 连续亏损笔数 | ≤ 3 | ≥ 5 |
| **交易频率** | 月交易笔数 | 5-15 | < 3（信号不足）或 > 30（过度交易）|
| **持仓时间** | 平均持仓日数 | 5-25 | < 3（信号失效）或 > 30（被套）|
| **换手率** | 月卖出 / 平均持仓 | 30-100% | > 200% |

---

## 四、数据记录方式

### 4.1 journal.csv（核心交易日志）

字段：
```
date,code,name,action,price,qty,amount,
score,factor_top1,pnl_yuan,pnl_pct,cum_pnl,
exit_type,days_held,slippage_pct,commission,note
```

写入时机：每笔交易完成后立即追加。

示例：
```csv
date,code,name,action,price,qty,amount,score,factor_top1,pnl_yuan,pnl_pct,cum_pnl,exit_type,days_held,slippage_pct,commission,note
2026-09-25,sh601086,国联股份,buy,25.30,1000,25300.0,0.732,high_low_20d,0,0,0,,0,0.005,7.59,
2026-10-30,sh601086,国联股份,sell,32.89,1000,32890.0,0.732,high_low_20d,7590,30.0,7590,tp,30,0.005,9.87,止盈
```

### 4.2 positions.json（实时持仓）

```json
{
  "cash": 920000.0,
  "initial_capital": 1000000.0,
  "holdings": {
    "sh601086": {
      "qty": 1000,
      "cost": 25300.0,
      "entry_price": 25.30,
      "entry_date": "2026-09-25",
      "current_value": 28000.0,
      "max_price": 32.89,
      "days_held": 30
    }
  },
  "last_update": "2026-10-30T18:35:12"
}
```

### 4.3 日报 / 周报 / 月报

- 日报：`data/plan_e/live_v3/daily_reports/daily_YYYYMMDD.md`
- 周报：`data/plan_e/live_v3/weekly_reports/week_YYYYMMDD.md`（周一生成）
- 月报：`data/plan_e/live_v3/monthly_reports/month_YYYYMM.md`（月末生成）

### 4.4 pnl_summary.csv（每日 P&L 快照）

```csv
date,total_equity,cash,holdings_value,total_pnl,daily_pnl,daily_pnl_pct,drawdown,n_holdings
2026-09-25,1000000,920000,80000,0,0,0,0,2
2026-09-26,1012500,920000,92500,12500,12500,0.0125,0,2
```

### 4.5 anomalies.log（异常事件）

```
[2026-10-30 18:35:12] [WARNING] low_win_rate: 本月胜率 45% < 50%
[2026-11-05 18:30:00] [CRITICAL] consecutive_loss: 连续 5 笔亏损，建议暂停 1 周
```

---

## 五、异常处理机制

### 5.1 异常分级

| 等级 | 图标 | 含义 | 处理 |
|:---:|:---:|---|---|
| 🟡 INFO | ℹ️ | 普通提示 | 记录到日志，不阻塞 |
| 🟡 WARNING | ⚠️ | 需关注 | 日报里高亮，邮件提醒 |
| 🔴 CRITICAL | 🚨 | 立即停止 | 暂停策略 1 周 + 复盘 |

### 5.2 自动检测的异常

| 类型 | 触发条件 | 等级 | 自动动作 |
|---|---|:---:|---|
| `low_win_rate` | 月胜率 < 50% | WARNING | 日报提醒 |
| `consecutive_loss` | 连续 5 笔亏 | CRITICAL | 暂停 1 周 |
| `max_drawdown` | 总回撤 > 15% | CRITICAL | 暂停 + 复盘 |
| `ic_decay` | 月度 IC 衰减 > 5pp | WARNING | 重训因子 |
| `position_concentration` | 单只 > 8% | WARNING | 下次再平衡 |
| `signal_lost` | 当日 Top 5% 候选 < 3 | WARNING | 暂停买入 |
| `slippage_high` | 实际滑点 > 3% | WARNING | 检查券商/换通道 |

### 5.3 人工干预触发条件

| 场景 | 处理 |
|---|---|
| 黑天鹅事件（个股 1 天跌 20%）| 强制止损 + 复盘 |
| 政策变化（涨跌停限制调整）| 暂停 1 周，等数据稳定 |
| 极端行情（A 股 -5% 以上）| 暂停 + 评估系统性风险 |
| 数据问题（日 K 缺失 3 日+）| 暂停 + 检查数据源 |
| 系统故障（factor_engine 报错）| 暂停 + 排查 |

### 5.4 暂停与恢复流程

```
触发异常 → 写 anomalies.log → 暂停策略
        ↓
通知到钉钉/邮件/微信
        ↓
人工复盘（查看日报 + journal + 月度 IC）
        ↓
决策：恢复 / 调整 / 终止
        ↓
执行：移除 config.json 中的 paused 标志
```

---

## 六、跟踪结果呈现形式

### 6.1 控制台（每日）

```bash
$ python scripts/plan_e_v3_live_track.py --mode status

===== v3 提前版实盘状态 =====
当前持仓: 4 只
可用现金: ¥620,000
持仓市值: ¥385,000
总权益: ¥1,005,000
累计 P&L: +¥5,000 (累计 +0.5%)
近 30 笔胜率: 65%
```

### 6.2 日报 Markdown（自动生成）

路径：`data/plan_e/live_v3/daily_reports/daily_YYYYMMDD.md`

包含：当日信号 + 建议动作 + 当前持仓 + 异常检测

### 6.3 周报（每周一盘前）

- 上周 P&L
- 周胜率 + 盈亏比
- 当前持仓 + 计划
- Top 5 标的收益贡献
- 异常事件回顾
- 下周关注

### 6.4 月报（每月最后一日盘后）

- 月度收益曲线
- 累计净值 vs 沪深 300
- 因子 RankIC 衰减图（关键监控）
- Top 10 标的收益贡献
- 月度胜率 / 盈亏比 / 最大回撤
- 策略调整建议

### 6.5 季度/年度回顾

- 季度 IC 衰减趋势
- 因子组合权重调整
- 实盘 vs 回测差异分析
- 风险敞口评估
- 仓位调整建议

### 6.6 数据可视化（可选）

- 净值曲线图（plotly）
- 因子 IC 热力图
- 持仓分布饼图
- 月度收益条形图

---

## 七、关键监控规则

### 7.1 每日必看

- [ ] 当日候选是否 ≥ 3 只（信号是否失效）
- [ ] 持仓数量 ≤ 10
- [ ] 单只仓位 ≤ 5%
- [ ] 当日是否触发异常
- [ ] 关键持仓是否触及止盈/止损

### 7.2 每周必看

- [ ] 周胜率 ≥ 50%
- [ ] 周回撤 ≤ -5%
- [ ] journal 是否完整记录
- [ ] 月度 IC 趋势是否稳定

### 7.3 每月必看

- [ ] 月度 IC 衰减是否 < 5pp
- [ ] Top 因子是否仍是 Top 3
- [ ] 是否需要重训因子
- [ ] 累计收益 vs 沪深 300 超额
- [ ] 实盘 vs 回测差异是否在容忍范围

---

## 八、风险与决策清单

### 8.1 启动前确认

- [ ] 回测结果已确认（验证胜率 ≥ 70%）
- [ ] 模拟 1-2 月已通过（胜率 ≥ 65%）
- [ ] 异常处理脚本已部署
- [ ] 监控指标已配置
- [ ] 日报/周报/月报模板已生成
- [ ] 数据备份已就绪
- [ ] 紧急联系人已设置

### 8.2 实盘启动标准（3 条件全满足）

1. ✅ 验证期 21 月胜率 ≥ 70%
2. ✅ 模拟 1-2 月胜率 ≥ 65%
3. ✅ 月度最大回撤 ≤ 5%

### 8.3 终止标准（任一触发）

- 连续 2 月胜率 < 40%
- 总回撤 > 25%
- 因子 IC 连续 3 月 < 0.02
- 系统性风险（市场停盘/政策剧变）

---

## 九、命令速查

```bash
# 1. 每日盘后跑
python scripts/plan_e_v3_live_track.py --mode daily

# 2. 看当前状态
python scripts/plan_e_v3_live_track.py --mode status

# 3. 单跑异常检测
python scripts/plan_e_v3_live_track.py --mode anomaly

# 4. 月报
python scripts/plan_e_v3_live_track.py --mode monthly

# 5. 回填历史
python scripts/plan_e_v3_live_track.py --mode daily --date 2026-09-25

# 6. 看日报
cat data/plan_e/live_v3/daily_reports/daily_$(date +%Y%m%d).md

# 7. 看 journal
tail data/plan_e/live_v3/journal.csv

# 8. 看异常日志
tail data/plan_e/live_v3/anomalies/anomalies.log

# 9. 因子 IC 监控
python scripts/factor_significance.py

# 10. 暂停/恢复
vim data/plan_e/live_v3/config.json  # 设置 paused=true
```

---

## 十、产物清单

| 路径 | 用途 |
|---|---|
| `scripts/plan_e_v3_live_track.py` | 主跟踪脚本 |
| `data/plan_e/live_v3/config.json` | 参数配置 |
| `data/plan_e/live_v3/journal.csv` | 交易日志 |
| `data/plan_e/live_v3/positions.json` | 实时持仓 |
| `data/plan_e/live_v3/pnl_summary.csv` | 每日 P&L 快照 |
| `data/plan_e/live_v3/signals/` | 每日信号 |
| `data/plan_e/live_v3/daily_reports/` | 日报 |
| `data/plan_e/live_v3/weekly_reports/` | 周报 |
| `data/plan_e/live_v3/monthly_reports/` | 月报 |
| `data/plan_e/live_v3/anomalies/anomalies.log` | 异常日志 |

---

## 十一、联系与升级路径

- **异常**：`data/plan_e/live_v3/anomalies/anomalies.log` + 邮件/钉钉
- **数据问题**：检查 `data/parquet/daily/` + `data/sentiment/`
- **策略调整**：复盘因子 IC + 重训模型
- **系统故障**：检查 systemd 日志 `journalctl -u plan_e_v3_daily`

---

**手册结束**。进入实盘前请完成"启动前确认"清单。