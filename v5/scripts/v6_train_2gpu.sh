#!/bin/bash
# v6 双卡 DDP 训练启动器
set -e
cd /home/jiuben/tdx-data-feed

LOG=/tmp/v6-train.log
echo "=== v6 双卡训练 ===" > $LOG
echo "  started: $(date)" >> $LOG

nohup env PATH=/home/jiuben/tdx-data-feed/venv/bin:$PATH \
  OMP_NUM_THREADS=1 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  CUDA_VISIBLE_DEVICES=0,1 \
  venv/bin/accelerate launch --num_processes 2 --mixed_precision bf16 --num_machines 1 \
  /tmp/v6_train_wrapper.py > $LOG 2>&1 &
PID=$!
echo "  PID: $PID" >> $LOG
echo "  log: $LOG" >> $LOG
echo "  monitor: tail -f $LOG"
