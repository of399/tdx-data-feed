#!/bin/bash
# deploy_v6.sh - v6 LoRA → merged → GGUF → Ollama（v5 后用）
# 与 deploy_v5.sh 几乎相同，仅路径换 v6
set -e

V6_DIR="/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v6-gpu1"
# 按 mtime 倒序选最新 ckpt
LATEST_CKPT=$(ls -1dt $V6_DIR/checkpoint-* 2>/dev/null | head -1)
if [ -z "$LATEST_CKPT" ]; then
    LATEST_CKPT=$(ls -d $V6_DIR/checkpoint-* 2>/dev/null | sort -V | tail -1)
fi
BASE_MODEL="/home/jiuben/models/Qwen3-14B"
V6_MERGED="/home/jiuben/models/Qwen3-14B-tdx-v6-merged"
V6_GGUF="/home/jiuben/models/Qwen3-14B-tdx-v6-merged-f16.gguf"
V6_MODEL_NAME="qwen3-14b-tdx-v6"
LOG=/tmp/v6-deploy.log

echo "=== 部署 v6 到 Ollama ===" | tee $LOG
echo "  checkpoint: $LATEST_CKPT" | tee -a $LOG
echo "  started: $(date)" | tee -a $LOG

if [ -z "$LATEST_CKPT" ]; then
  echo "❌ 无 checkpoint" | tee -a $LOG
  exit 1
fi

# 1. 合并 LoRA
echo "[1/4] 合并 LoRA..." | tee -a $LOG
cd /home/jiuben/tdx-data-feed
rm -rf $V6_MERGED
PYTHONPATH=/home/jiuben/tdx-data-feed venv/bin/python <<EOF
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base_model = AutoModelForCausalLM.from_pretrained(
    "$BASE_MODEL",
    torch_dtype=torch.bfloat16,
    device_map="cpu",
    low_cpu_mem_usage=True,
)
model = PeftModel.from_pretrained(base_model, "$LATEST_CKPT")
merged = model.merge_and_unload()
merged.save_pretrained("$V6_MERGED", safe_serialization=True)
tokenizer = AutoTokenizer.from_pretrained("$LATEST_CKPT")
tokenizer.save_pretrained("$V6_MERGED")
print("  ✅ merged saved")
EOF

# 2. 转 GGUF
echo "[2/4] 转 GGUF..." | tee -a $LOG
LLAMA_CPP=$(find /home/jiuben -name "convert_hf_to_gguf.py" -path "*/llama.cpp/*" 2>/dev/null | head -1)
# 2026-09-25: vllm-venv 已移除（参考 audit/2026-09-21-vllm-quantize-exploration.md）
if [ -z "$LLAMA_CPP" ]; then
  LLAMA_CPP="/home/jiuben/llama.cpp/convert_hf_to_gguf.py"
fi
if [ -f "$LLAMA_CPP" ]; then
  /home/jiuben/tdx-data-feed/venv/bin/python "$LLAMA_CPP" \
    $V6_MERGED --outfile $V6_GGUF --outtype f16
  echo "  ✅ GGUF saved" | tee -a $LOG
else
  echo "  ⚠ 跳过 GGUF 转换（llama.cpp 工具不在）" | tee -a $LOG
fi

# 3. Modelfile
echo "[3/4] Modelfile..." | tee -a $LOG
V6_MODEFILE="/home/jiuben/models/Modelfile-v6"
cat > $V6_MODEFILE <<MODFILE
# Modelfile · qwen3-14b-tdx-v6 (v5 → v6 LoRA merged)
FROM $V6_GGUF
PARAMETER stop "<|im_end|>"
PARAMETER temperature 0.3
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_ctx 4096
PARAMETER repeat_penalty 1.1
MODFILE

# 4. ollama create
echo "[4/4] ollama create..." | tee -a $LOG
if [ -f "$V6_GGUF" ]; then
  ollama create $V6_MODEL_NAME -f $V6_MODEFILE
fi
echo "=== v6 部署完成: $(date) ==="