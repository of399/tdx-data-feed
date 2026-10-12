#!/usr/bin/env bash
# 方案 E 实盘样本·定期回溯调优
# 1) journal 回灌 → data/double_up/real_trades.csv（累积去重）
# 2) 历史宇宙回测调优：--retune（含北交所压力）+ --by-year
# 3) 实盘样本分析：--real（观测收益 + n≥30 统计显著性闸口）
# 可由 systemd timer (plan-e-real-refresh.timer) 或 cron 周期调度。
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=venv/bin/python

echo "[$(date '+%F %T')] 1/3 构建实盘样本集（journal → data/double_up/real_trades.csv）"
"$VENV" scripts/plan_e_real_samples.py

echo "[$(date '+%F %T')] 2/3 历史宇宙回测调优（retune + 北交所压力 + by-year）"
"$VENV" scripts/plan_e_backtest.py --retune --bj-slippage 0.003
"$VENV" scripts/plan_e_backtest.py --by-year

echo "[$(date '+%F %T')] 3/3 实盘样本分析（观测收益 + n≥30 闸口）"
"$VENV" scripts/plan_e_backtest.py --real

echo "[$(date '+%F %T')] ✅ 全部完成"
