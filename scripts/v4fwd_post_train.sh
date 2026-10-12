#!/usr/bin/env bash
# 方案A 实证：训练后自动化流水线（合并→GGUF→Ollama→重生成信号→对比+walk-forward）
# 轮询等待 QLoRA 训练产物就绪后，依次完成导出与评估，全程后台运行、日志落盘。
# 可靠件：source scripts/lib/pipeline.sh 提供的 run_step / assert_min_size / assert_ollama_model，
#        关键步骤失败即中止 + 全程日志落盘 + 产物覆盖校验，杜绝「跑完却 0 覆盖」。
set -e
set -o pipefail
cd /home/jiuben/tdx-data-feed
source scripts/lib/pipeline.sh

BASE=/home/jiuben/models/Qwen3-14B
ADAPTER=/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v4fwd-qlora
MERGED=/home/jiuben/models/Qwen3-14B-tdx-v4fwd-merged
GGUF=/home/jiuben/models/Qwen3-14B-tdx-v4fwd-merged-q8_0.gguf
MF=/home/jiuben/models/Modelfile-v4fwd
NAME=qwen3-14b-tdx-v4fwd
SIG=data/plan_e/llm_sig_v4fwd.csv
LOG=logs/v4fwd_post.log
PIPELINE_LOG="$LOG"
CONVERT=/home/jiuben/llama.cpp/convert_hf_to_gguf.py

echo "[$(date)] 等待训练产物 $ADAPTER/adapter_model.safetensors ..." | tee -a "$LOG"
for i in $(seq 1 600); do  # 最多等 10h
  if [ -f "$ADAPTER/adapter_model.safetensors" ]; then
    echo "[$(date)] 训练产物就绪" | tee -a "$LOG"; break
  fi
  sleep 60
done
if [ ! -f "$ADAPTER/adapter_model.safetensors" ]; then
  echo "[$(date)] ERROR: 训练产物未出现，流水线中止" | tee -a "$LOG"; exit 1
fi

echo "[$(date)] 1) 合并 LoRA -> HF (--device cpu, 防 2x16G CUDA OOM)" | tee -a "$LOG"
rm -rf "$MERGED"
run_step "合并 LoRA -> HF" venv/bin/python train/scripts/merge_lora.py \
  --base "$BASE" --adapter "$ADAPTER" --output "$MERGED" --device cpu
assert_min_size "$MERGED" 10000000000 "合并产物(权重目录 <10GB, 可能 OOM 中断)"

echo "[$(date)] 2) 转 GGUF (q8_0)" | tee -a "$LOG"
run_step "转 GGUF (q8_0)" venv/bin/python "$CONVERT" "$MERGED" --outfile "$GGUF" --outtype q8_0
assert_min_size "$GGUF" 10000000000 "GGUF(<10GB)"

echo "[$(date)] 3) ollama create $NAME" | tee -a "$LOG"
run_step "ollama create $NAME" ollama create "$NAME" -f "$MF"
assert_ollama_model "$NAME"

echo "[$(date)] 4) 重生成方向信号（全 universe，约 1.5h）" | tee -a "$LOG"
run_step "重生成方向信号" venv/bin/python scripts/llm_signal_plan_e.py gen --model "$NAME" --out "$SIG"
assert_min_size "$SIG" 2 "方向信号 CSV(空, 可能 0 覆盖)"

echo "[$(date)] 5) 对比 + walk-forward" | tee -a "$LOG"
run_step "对比 + walk-forward" venv/bin/python scripts/compare_plan_e_signals.py --signal-csv "$SIG" --name v4fwd

echo "[$(date)] 6) oracle 天花板（参考，非关键）" | tee -a "$LOG"
venv/bin/python scripts/oracle_plan_e_ceiling.py --theta 0.02 >>"$LOG" 2>&1 || echo "[$(date)] WARN: oracle 参考步跳过" | tee -a "$LOG"

echo "[$(date)] DONE 全部流水线完成" | tee -a "$LOG"
