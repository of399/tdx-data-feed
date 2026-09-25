#!/bin/bash
# v5_full_a_daemon.sh - 启动 v5 模型跑全 A 股再分析
# v6 部署后启动（避免 GPU 冲突）

set -e

cd /home/jiuben/tdx-data-feed
LOG=/tmp/v5-full-A.log
JSONL=/home/jiuben/tdx-data-feed/v5/reports/v5-batch-$(date +%Y%m%d-%H%M%S).jsonl

echo "=== v5 全 A 股再分析 ===" | tee $LOG
echo "  started: $(date)" | tee -a $LOG
echo "  model: qwen3-14b-tdx-v5" | tee -a $LOG
echo "  target: 4629 只 A 股" | tee -a $LOG
echo "  est: ~13 小时" | tee -a $LOG
echo "  log: $LOG" | tee -a $LOG
echo "" | tee -a $LOG

# 启动 v5 后台分析
nohup venv/bin/python v5/scripts/v4_batch_analysis.py \
  --model qwen3-14b-tdx-v5 \
  --a-share-only \
  --skip-existing \
  > $LOG 2>&1 &
PID=$!
echo "  PID: $PID" | tee -a $LOG

# 写 monitoring 命令
cat > /tmp/v5_full_monitor.sh <<'EOF'
#!/bin/bash
echo "=== v5 全 A 股状态 ==="
ps -p $PID -o pid,etime,cmd 2>&1 | head -3
tail -3 $LOG
EOF
chmod +x /tmp/v5_full_monitor.sh

echo "  monitor: tail -f $LOG" | tee -a $LOG
echo "  stop: kill $PID" | tee -a $LOG
echo "  完成后会自动退出" | tee -a $LOG