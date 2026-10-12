#!/bin/bash
# deploy_v7.sh - 部署 qwen3-14b-tdx-v7 到 Ollama
#
# 与 v5/v6 不同：v7 的合并 GGUF 已生成并落盘（见 models/），本脚本只负责
# 用既有 Modelfile 重新 ollama create；如需从 LoRA 重新合并，请改用
# train/scripts/deploy_v7_resume.sh（部署 v7r / v7r1115 变体）。
set -euo pipefail

V7_GGUF="/home/jiuben/models/Qwen3-14B-tdx-v7-merged-f16.gguf"
V7_MODEFILE="/home/jiuben/models/Modelfile-v7"
V7_MODEL_NAME="qwen3-14b-tdx-v7"
LOG=/tmp/v7-deploy.log

echo "=== 部署 v7 到 Ollama ===" | tee "$LOG"
echo "  model:   $V7_MODEL_NAME" | tee -a "$LOG"
echo "  started: $(date)" | tee -a "$LOG"

if [ ! -f "$V7_GGUF" ]; then
  echo "❌ 合并 GGUF 缺失: $V7_GGUF（需先 merge LoRA → GGUF）" | tee -a "$LOG"
  exit 1
fi
if [ ! -f "$V7_MODEFILE" ]; then
  echo "❌ Modelfile 缺失: $V7_MODEFILE" | tee -a "$LOG"
  exit 1
fi

echo "[1/1] ollama create $V7_MODEL_NAME -f $V7_MODEFILE" | tee -a "$LOG"
ollama create "$V7_MODEL_NAME" -f "$V7_MODEFILE" 2>&1 | tee -a "$LOG"

echo "=== v7 部署完成: $(date) ===" | tee -a "$LOG"
echo "  ollama: $V7_MODEL_NAME"
