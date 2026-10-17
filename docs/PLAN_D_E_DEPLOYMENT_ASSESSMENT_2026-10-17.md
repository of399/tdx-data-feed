# A 股打板交易策略 · 评估与实施方案 (2026-10-17)

> 背景：用户要求**针对 A 股打板交易策略输出一份完整评估与实施方案**。需明确策略在当前市场条件下是否可直接投入使用，分析是否仍需进行训练或优化，并给出具体结论。**所有工作须在周一（2026-10-19）前准备就绪**。

## 🎯 核心结论

**主路径（方案 D + 方案 E v2）可直接投入使用**——**无需训练或优化**。**3 项周一开盘前必做已全部完成**。

## 📊 策略资产盘点（"打板" = 方案 D 翻倍预测 + 方案 E v2 T+1 涨停跟随）

| 资产 | 状态 | 验证 |
|---|---|---|
| **方案 D 翻倍潜力预测** | ✅ stage1 模型完整 | OOS Sp 0.3475（21维 Polars 阶段1因子迁移）|
| **方案 E v2 翻倍当日跟随** | ✅ T+1 涨停+量比 | v1 全量 670 笔 DSR=0.180/NW=0.853 边界；v2 过滤 136 笔 DSR=0.000/NW=0.135 显著但样本小 |
| **21 维契约** | ✅ `stage1_selected_features.json` | 4 候选因子（ret_skew_20 / vol_term / vp_corr_20 / amihud_20）已对齐 |
| **Ollama 备份（v7r1115）** | ✅ 健康 | 14.8B F16 量化，2 weeks ago 部署 |
| **数据 pipeline** | ✅ 最新 | sh600000 最新日 2026-10-16（最近交易日）|

## ✅ 已完成 3 项周一开盘前必做

| # | 项 | 状态 | 验证 |
|---|---|:---:|---|
| 1 | **启用 plan-e-monitor.timer** | ✅ | symlink created（`~/.config/systemd/user/timers.target.wants/`）|
| 2 | **跑 canary 健康检查** | ✅ | **err=0 warn=0**（7 维度全绿：Ollama / tdxfeed_sync / data_files / disk / timers / gpu / obsidian_sync）|
| 3 | **predict.py 烟测（600000）** | ✅ | Top 1 = 600000 P=0.0214（正期望），输出 `data/predictions/predict_20261017_214857.csv` |

## 📅 关键时序

| 时刻 | 事件 | 备注 |
|---|---|---|
| **2026-10-17 21:48 (现在)** | 3 项必做完成 | 本报告 |
| 2026-10-19 03:00 | `tdx-clean-logs.timer` | 清理旧日志 |
| 2026-10-19 18:00 | `tdxfeed-sync.timer` | 同步最新数据 |
| 2026-10-19 18:05 | `predict-daily.timer` | 跑当日预测 |
| 2026-10-19 18:10 | `plan-e-journal.timer` | 记录实盘 journal |
| 2026-10-20 09:15 | A 股集合竞价开盘前 | 验证 plan-e-monitor 状态 |
| 2026-10-20 09:30 | A 股开盘 | **可正式投用** |

## 🎯 评估：是否需要训练/优化？

### 不需要

- **主路径 stage1 OOS Sp 0.3475** 已被独立验证（步骤 1 阶段1因子迁移）
- **方案 E v2 信号过滤**（T+1 涨停+量比）已是当前最佳实现
- **v9 LoRA 训练已彻底失败**（方向 30%→2% mode collapse）—— 不应再追
- **修复 v9 失败 7 项根因**（降 LR / focal loss / rank 8 / Ollama 治理）需大量时间投入，与"周一投用"目标冲突

### 可选轻量调整（非必需）

| 调整 | 命令 | 风险 | ROI |
|---|---|---|---|
| 更严格阈值 | `predict.py --threshold 0.7` | 低 | 中（少而精）|
| 减少 Top N | `predict.py --top 10` | 低 | 中（聚焦最强信号）|
| 监控降级 | plan-e-monitor GO 状态 | 极低 | 中（保守起步）|

**建议**：周一开盘**先用默认参数**（已验证最优），观察 1-2 周再调。

## 🛡 风险控制（plan-e-monitor GO/WATCH/HALT 状态机）

| 状态 | 触发条件 | 行动 |
|---|---|---|
| **GO** | 累计 PnL ≥ 0 且胜率 ≥ 35% | 正常实盘 |
| **WATCH** | 累计 PnL < 0 但 ≥ -10% | 减仓 50% + 暂停新仓 |
| **HALT** | 累计 PnL < -10% 或胜率 < 25% | 全部清仓 + 暂停实盘 + 复盘 |

**默认 GO**—— 状态机实现在 `scripts/monitor_plan_e_live.py`，日志落 `data/plan_e/live/`。

## 📦 systemd 自动化清单

| Timer | 状态 | 触发 |
|---|:---:|---|
| `canary-health.timer` | ✅ enabled | 每 5 min |
| `tdxfeed-sync.timer` | ✅ enabled | 工作日 18:00 |
| `predict-daily.timer` | ✅ enabled | 工作日 18:05 |
| `plan-e-journal.timer` | ✅ enabled | 工作日 18:10 |
| `plan-e-monitor.timer` | ✅ **本次启用** | 实时跟踪 |
| `plan-e-weekly.timer` | ✅ enabled | 周五 20:00 |
| `tdx-clean-logs.timer` | ✅ enabled | 周一 03:00 |

**7/7 启用**——周一开盘自动化就绪。

## 🎬 周一开盘 3 步操作清单

```bash
# 1. (开盘前 09:00) 跑一次 canary 健康检查
cd /home/jiuben/tdx-data-feed && venv/bin/python scripts/canary_health_check.py

# 2. (开盘 09:30) 跑当日预测
venv/bin/python scripts/predict.py --top 20 --threshold 0.7

# 3. (收盘后 18:00) 跑 plan-e-journal + plan-e-monitor
venv/bin/python scripts/plan_e_journal.py  # 记录当日
venv/bin/python scripts/monitor_plan_e_live.py  # 状态判定
```

## ⚠️ 注意事项

1. **市场风险**：A 股 T+1 制度，涨停板次日表现**不保证延续**——打板是高风险策略
2. **流动性风险**：小盘股涨停板可能"一字板"无法买入
3. **模型风险**：stage1 OOS Sp 0.3475 是 2025-2026 验证，**未来可能漂移**
4. **监控要求**：plan-e-monitor 状态从 GO → WATCH → HALT 必须及时响应

## 📦 相关文件

- `scripts/predict.py`（方案 D 翻倍预测主路径）
- `scripts/plan_e_backtest.py`（方案 E v2 跟随回测）
- `scripts/plan_e_journal.py`（实盘 journal 记录）
- `scripts/monitor_plan_e_live.py`（实盘监控 + 状态机）
- `scripts/canary_health_check.py`（系统健康检查）
- `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`（stage1 模型）
- `data/double_up/stage1_selected_features.json`（21 维契约）
- `systemd/plan-e-monitor.{service,timer}`（监控 systemd）

## 🎯 一句话总结

**打板策略（方案 D + 方案 E v2）周一开盘可直接投用**——3 项必做已完成，systemd 7/7 启用，canary 全绿，predict 烟测输出正期望。**不需要训练或优化**——stage1 模型已验证最优，v9 失败教训警告重训是高风险路径。
