#!/usr/bin/env bash
# v9 训练完成后处理流水线(条件动作B): 合并LoRA → GGUF f16 → Ollama 部署 → 方案D 预测对比
# 每步校验输出, 失败立即停并记录。全程写 logs/v9_post_train.log。
set -u
ROOT=/home/jiuben/tdx-data-feed
LOG="$ROOT/logs/v9_post_train.log"
BASE=/home/jiuben/models/Qwen3-14B
ADAPTER="$ROOT/train/output/qwen3-14b-tdx-v9/checkpoint-6006"
MERGED="$ROOT/models/Qwen3-14B-tdx-v9-merged"
GGUF=/home/jiuben/models/Qwen3-14B-tdx-v9-merged-f16.gguf   # 与 Modelfile-v9 的 FROM 一致
MODELFILE=/home/jiuben/models/Modelfile-v9
cd "$ROOT" || exit 1
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') [post-train] $*" >> "$LOG"; }

log "==== v9 后处理流水线启动 ===="
log "等待训练进程退出(最终评估收尾, 释放GPU/内存)..."
while pgrep -f "tdx_v9_from_scratch" >/dev/null; do sleep 20; done
log "训练已退出, 开始后处理"

# a) 合并 LoRA → 完整 HF 目录 (CPU 安全流式, 默认 device=cpu)
log "step a) merge_lora.py  base=$BASE  adapter=checkpoint-6006  -> $MERGED (cpu)"
venv/bin/python train/scripts/merge_lora.py --base "$BASE" --adapter "$ADAPTER" --output "$MERGED" --device cpu >> "$LOG" 2>&1
if ! ls "$MERGED"/model*.safetensors >/dev/null 2>&1 && [ ! -f "$MERGED/pytorch_model.bin" ]; then
  log "❌ step a 失败: 合并输出缺失"; exit 1
fi
log "✅ step a 完成: $MERGED ($(du -sh "$MERGED" 2>/dev/null | cut -f1))"

# b) 转 GGUF f16 (~29.5GB), 输出到 /home/jiuben/models/ 以匹配 Modelfile-v9
log "step b) convert_hf_to_gguf.py -> $GGUF (f16)"
venv/bin/python /home/jiuben/llama.cpp/convert_hf_to_gguf.py "$MERGED" --outfile "$GGUF" --outtype f16 >> "$LOG" 2>&1
if [ ! -f "$GGUF" ]; then log "❌ step b 失败: GGUF 未生成"; exit 1; fi
log "✅ step b 完成: $GGUF ($(du -h "$GGUF" | cut -f1))"

# c) Ollama 部署
log "step c) ollama create qwen3-14b-tdx-v9 -f Modelfile-v9"
ollama create qwen3-14b-tdx-v9 -f "$MODELFILE" >> "$LOG" 2>&1
if ! ollama list 2>/dev/null | grep -q "qwen3-14b-tdx-v9"; then log "❌ step c 失败: ollama 模型未创建"; exit 1; fi
log "✅ step c 完成: qwen3-14b-tdx-v9 已部署"

# d) 方案 D 预测对比 (Top30, threshold 0.7)
log "step d) predict.py --model qwen3-14b-tdx-v9 --top 30 --threshold 0.7"
venv/bin/python scripts/predict.py --model qwen3-14b-tdx-v9 --top 30 --threshold 0.7 >> "$LOG" 2>&1
log "✅ step d 完成(预测输出见日志; v9_vs_v7r 对比报告另行生成)"
log "🎉 v9 后处理流水线全部完成"
