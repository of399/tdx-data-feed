#!/bin/bash
# v4 全 A 股后台分析 daemon
# 跑全 5566 只，每只约 8.5s，预计 ~13 小时
# 用 systemd-run 监管，自动重启

set -e

cd /home/jiuben/tdx-data-feed

LOG=/tmp/v4-full-daemon.log
PIDFILE=/tmp/v4-full-daemon.pid

echo "=== v4 全 A 股后台分析 ===" | tee -a $LOG
echo "  started: $(date)" | tee -a $LOG
echo "  model: qwen3-14b-tdx-fast:14b" | tee -a $LOG
echo "  total: 5566 只" | tee -a $LOG
echo "  est: ~13 小时" | tee -a $LOG
echo "  skip_existing: true" | tee -a $LOG
echo "" | tee -a $LOG

# 启动
nohup venv/bin/python v5/scripts/v4_batch_analysis.py --all --skip-existing > $LOG 2>&1 &
PID=$!
echo $PID > $PIDFILE

echo "  PID: $PID" | tee -a $LOG
echo "  log: $LOG" | tee -a $LOG
echo "  monitor: tail -f $LOG" | tee -a $LOG
echo "  stop: kill $PID" | tee -a $LOG