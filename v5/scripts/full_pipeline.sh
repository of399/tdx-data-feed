#!/bin/bash
# full_pipeline.sh - v5 → 部署 → 评估 → v6 → 部署 → v5 全 A 一键流水线
set -e
cd /home/jiuben/tdx-data-feed
PIPE_LOG=/tmp/pipeline.log
echo "=== 全流水线启动: $(date) ===" | tee $PIPE_LOG

# 1. 等 v5 完成
echo "[1/6] 等 v5 训练 (PID 445413)..." | tee -a $PIPE_LOG
while ps -p 445413 > /dev/null 2>&1; do
  sleep 60
done
echo "  ✓ v5 完成: $(date)" | tee -a $PIPE_LOG

# 2. 部署 v5
echo "[2/6] 部署 v5 到 Ollama..." | tee -a $PIPE_LOG
bash v5/scripts/deploy_v5.sh 2>&1 | tee -a $PIPE_LOG

# 3. 评估 v5 vs v4
echo "[3/6] 评估 v5..." | tee -a $PIPE_LOG
venv/bin/python v5/scripts/v5_eval.py 2>&1 | tee -a $PIPE_LOG

# 4. 释放 Ollama 显存
echo "[4/6] 释放 Ollama 显存..." | tee -a $PIPE_LOG
curl -s -X POST http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-14b-tdx-v5","prompt":"x","stream":false,"options":{"num_predict":1,"num_ctx":128},"keep_alive":0}' >/dev/null
sleep 10

# 5. 启动 v6 训练
echo "[5/6] 启动 v6 训练 (5h)..." | tee -a $PIPE_LOG
bash v5/scripts/v6_train_2gpu.sh 2>&1 | tee -a $PIPE_LOG
V6_PID=$(pgrep -f "v6_train_wrapper.py" | head -1)
echo "  v6 PID: $V6_PID" | tee -a $PIPE_LOG
while ps -p $V6_PID > /dev/null 2>&1; do
  sleep 60
done
echo "  ✓ v6 完成: $(date)" | tee -a $PIPE_LOG

# 6. 部署 v6 + 启动 v5 全 A
echo "[6/6] 部署 v6 + 启动 v5 全 A..." | tee -a $PIPE_LOG
bash v5/scripts/deploy_v6.sh 2>&1 | tee -a $PIPE_LOG
bash v5/scripts/v5_full_a_daemon.sh 2>&1 | tee -a $PIPE_LOG

echo "=== 全部完成: $(date) ===" | tee -a $PIPE_LOG
