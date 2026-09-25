#!/bin/bash
# deploy_v5.sh - v5 LoRA → merged → GGUF → Ollama
set -e

V5_DIR="/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v5-simple"
# 按 mtime 倒序选最新 ckpt（fast 训练残留 Sep 22 mtime 更早，不会被选中）
LATEST_CKPT=$(ls -1dt $V5_DIR/checkpoint-* 2>/dev/null | head -1)
if [ -z "$LATEST_CKPT" ]; then
    LATEST_CKPT=$(ls -d $V5_DIR/checkpoint-* 2>/dev/null | sort -V | tail -1)
fi
BASE_MODEL="/home/jiuben/models/Qwen3-14B"
V5_MERGED="/home/jiuben/models/Qwen3-14B-tdx-v5-merged"
V5_GGUF="/home/jiuben/models/Qwen3-14B-tdx-v5-merged-f16.gguf"
V5_MODEL_NAME="qwen3-14b-tdx-v5"
LOG=/tmp/v5-deploy.log

echo "=== 部署 v5 到 Ollama ===" | tee $LOG
echo "  checkpoint: $LATEST_CKPT" | tee -a $LOG
echo "  started: $(date)" | tee -a $LOG

if [ -z "$LATEST_CKPT" ]; then
  echo "❌ 无 checkpoint" | tee -a $LOG
  exit 1
fi

# 1. 合并 LoRA → 完整模型
echo "[1/4] 合并 LoRA → merged 模型..." | tee -a $LOG
cd /home/jiuben/tdx-data-feed
rm -rf $V5_MERGED
PYTHONPATH=/home/jiuben/tdx-data-feed venv/bin/python <<EOF
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

# GPU 已被 v5 训练占满 → 强制 CPU offload 合并（28GB bf16 > 16GB available）
print("  loading base (device_map=cpu, low_cpu_mem_usage)...")
base_model = AutoModelForCausalLM.from_pretrained(
    "$BASE_MODEL",
    torch_dtype=torch.bfloat16,
    device_map="cpu",
    low_cpu_mem_usage=True,
)
print("  loading LoRA...")
model = PeftModel.from_pretrained(base_model, "$LATEST_CKPT")
print("  merging...")
merged = model.merge_and_unload()
print(f"  saving to $V5_MERGED")
merged.save_pretrained("$V5_MERGED", safe_serialization=True)
tokenizer = AutoTokenizer.from_pretrained("$LATEST_CKPT")
tokenizer.save_pretrained("$V5_MERGED")
print("  ✅ merged saved")
EOF

# 2. 转 GGUF
echo "[2/4] 转 GGUF (f16)..." | tee -a $LOG
LLAMA_CPP=$(find /home/jiuben -name "convert_hf_to_gguf.py" -path "*/llama.cpp/*" 2>/dev/null | head -1)
# 2026-09-25: vllm-venv 已移除（参考 audit/2026-09-21-vllm-quantize-exploration.md）
# 如果系统没有 llama.cpp, GGUF 转换会跳过. 可手动 install:
#   pip install llama-cpp-python   # 主 venv 已有 gguf 库, 或用 llama.cpp 项目
if [ -z "$LLAMA_CPP" ]; then
  LLAMA_CPP="/home/jiuben/llama.cpp/convert_hf_to_gguf.py"  # fallback 到系统路径
fi
if [ -f "$LLAMA_CPP" ]; then
  cd /home/jiuben
  /home/jiuben/tdx-data-feed/venv/bin/python "$LLAMA_CPP" \
    $V5_MERGED \
    --outfile $V5_GGUF \
    --outtype f16
  echo "  ✅ GGUF saved" | tee -a $LOG
else
  echo "  ⚠ 跳过 GGUF 转换（llama.cpp 工具不在）" | tee -a $LOG
fi

# 3. 写 Modelfile
echo "[3/4] 写 Modelfile..." | tee -a $LOG
V5_MODEFILE="/home/jiuben/models/Modelfile-v5"
cat > $V5_MODEFILE <<MODFILE
# Modelfile · qwen3-14b-tdx-v5 (Qwen3-14B + v5 LoRA merged → GGUF)
# 训练数据: v5_train (12656 条) + vault v4 段 (4733)
# 来源: v4 fast merged → v5 LoRA training

FROM $V5_GGUF

PARAMETER stop "<|im_end|>"
PARAMETER temperature 0.3
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_ctx 4096
PARAMETER repeat_penalty 1.1

TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ .Response }}<|im_end|>"""
MODFILE

# 4. 创建 Ollama 模型
echo "[4/4] ollama create..." | tee -a $LOG
if [ -f "$V5_GGUF" ]; then
  ollama create $V5_MODEL_NAME -f $V5_MODEFILE
  echo "  ✅ Ollama 模型: $V5_MODEL_NAME" | tee -a $LOG
else
  echo "  ⚠ GGUF 缺失，跳过 ollama create" | tee -a $LOG
fi

echo "=== v5 部署完成: $(date) ===" | tee -a $LOG
echo "  merged: $V5_MERGED"
echo "  GGUF: $V5_GGUF"
echo "  ollama: $V5_MODEL_NAME"