#!/usr/bin/env bash
# verify_single_qlora.sh — 单卡 QLoRA 实测验证（自包含、可复现）
# 作用：限定 GPU0 跑有界训练，后台按训练 PID 采样显存，结束后输出指标摘要与峰值显存。
# 用法：bash scripts/verify_single_qlora.sh
set -u -o pipefail   # 补 pipefail 防管道吞错（保留 -u）
cd /home/jiuben/tdx-data-feed

export CUDA_VISIBLE_DEVICES=0
LOG=logs/verify_train.log
VRAM=logs/verify_vram.log
rm -f "$LOG" "$VRAM"

echo ">>> start single-card QLoRA verify @ $(date)"
venv/bin/llamafactory-cli train training/config/tdx_verify_single_qlora.yaml > "$LOG" 2>&1 &
TRPID=$!

# 显存监控：训练进程存活期间每 2s 采样 GPU0 显存/利用率
( while kill -0 "$TRPID" 2>/dev/null; do
    nvidia-smi --query-gpu=index,memory.used,utilization.gpu \
      --format=csv,noheader >> "$VRAM" 2>/dev/null
    sleep 2
  done ) &
MON=$!

wait "$TRPID"
echo ">>> training finished @ $(date) (exit=$?)"

kill "$MON" 2>/dev/null

echo
echo "================ 指标摘要 ================"
grep -E "'loss'|'grad_norm'|'learning_rate'|'epoch'|'step'|train_runtime|train_samples_per_second|eval_loss|eval_runtime" "$LOG" | tail -80

echo
echo "================ 峰值显存(GPU0) ================"
if [ -f "$VRAM" ]; then
  awk -F', ' '$1=="0"{ v=$2; gsub(/ MiB/,"",v); if(v+0>m)m=v } END{print (m?m" MiB":"N/A")}' "$VRAM"
else
  echo "N/A (显存采样未生成)"
fi
echo "================ 结束 ================"
