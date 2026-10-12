#!/usr/bin/env bash
# pipeline.sh — 训练/部署流水线通用安全件（v1, 2026-10-09）
#
# 提供 run_step / assert_min_size / assert_ollama_model，统一：
#   1) 关键命令失败即中止（替代 `cmd | tail` 掩盖失败）
#   2) stdout/stderr 全量写入 PIPELINE_LOG（不再被 tail 截断导致排查无据）
#   3) 产物覆盖校验（拦 "exit 0 却产物为空/损坏" 的语义级静默失败）
#
# 用法:
#   source scripts/lib/pipeline.sh
#   PIPELINE_LOG=logs/xxx.log run_step "合并 LoRA" venv/bin/python train/scripts/merge_lora.py ...
#
# 注意：本文件只定义函数，不强制修改调用方的 set 选项；调用方应自行
#       维持 `set -euo pipefail`（或在脚本顶部声明）。

# 执行一个关键命令；失败则打印 ERROR 并 exit 1；输出全量落盘。
run_step() {
  local desc="$1"; shift
  local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
  local log="${PIPELINE_LOG:-/dev/null}"
  echo "[$ts] ▶ $desc" | tee -a "$log"
  if ! "$@" >>"$log" 2>&1; then
    echo "[$ts] ✗ ERROR: $desc 失败 (exit=$?), 流水线中止" | tee -a "$log"
    exit 1
  fi
  echo "[$ts] ✓ $desc 完成" | tee -a "$log"
}

# 断言路径存在且字节数 >= min；目录则累加其下文件大小。
# 拦 "exit 0 却产物为空/损坏"（如 Ollama 空模型、0 行 CSV、缺失 GGUF）。
assert_min_size() {
  local f="$1" min="$2" desc="${3:-$1}"
  local log="${PIPELINE_LOG:-/dev/null}"
  local sz=0
  if [ -d "$f" ]; then
    sz=$(find "$f" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
  else
    sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  fi
  if [ ! -e "$f" ] || [ "$sz" -lt "$min" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ ERROR: $desc 缺失或过小 (<$min bytes, 实际 $sz), 流水线中止" \
      | tee -a "$log"
    exit 1
  fi
}

# 断言 Ollama 已注册指定模型（拦 ollama create exit 0 却未注册）。
assert_ollama_model() {
  local name="$1"
  local log="${PIPELINE_LOG:-/dev/null}"
  if ! ollama list 2>/dev/null | grep -q "$name"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ ERROR: ollama 模型未注册: $name, 流水线中止" \
      | tee -a "$log"
    exit 1
  fi
}
