#!/bin/bash
# 等待 v4 daemon (PID 198712) 完成后自动跑 vault_to_v5.py
# 使用方法: nohup ./wait_then_v5.sh > /tmp/wait_then_v5.log 2>&1 &

DAEMON_PID=198712
LOG=/home/jiuben/tdx-data-feed/v5/reports/v5-generation.log
echo "=== 等待 daemon ${DAEMON_PID} 完成后跑 v5 ===" | tee -a $LOG
echo "  started: $(date)" | tee -a $LOG

# 轮询等待
while ps -p $DAEMON_PID > /dev/null 2>&1; do
    sleep 60
done

echo "  daemon ${DAEMON_PID} 已完成: $(date)" | tee -a $LOG

# 跑 v5
cd /home/jiuben/tdx-data-feed
echo "  开始生成 v5 训练数据..." | tee -a $LOG
venv/bin/python v5/scripts/vault_to_v5.py 2>&1 | tee -a $LOG

echo "  v5 生成完毕: $(date)" | tee -a $LOG

# 跑 v5 验证
echo "  v5_train.json 行数:" | tee -a $LOG
wc -l data/v5_train/v5_train.json | tee -a $LOG
wc -l data/v5_train/v5_val.json | tee -a $LOG