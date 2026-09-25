#!/usr/bin/env bash
# ⚠️  DEPRECATED (2026-09-25)
# vllm-venv 已移除（B 方案止损, 见 audit/2026-09-21-vllm-quantize-exploration.md）
# 量化 14B f16 在 2×16GB 硬件上 OOM (硬上限). 此脚本保留仅为历史参考.
# 如需量化: 用 24GB+ 单卡机器, 跑 quantize_awq.py 主入口
#
# 输入: /home/jiuben/models/Qwen3-14B-tdx-fast-merged/  (28GB f16)
# 输出: /home/jiuben/models/Qwen3-14B-tdx-fast-awq/      (~7GB int4)
# 量级: 2×RTX 5060 Ti 16GB, TP=2, max_calib_samples=32, max_calib_seq_len=512
# 预期: 30-60 分钟（但会 OOM, 见审计报告）

set -euo pipefail

VENV=/home/jiuben/tdx-data-feed/vllm-venv   # DEPRECATED, 已删除
SRC=/home/jiuben/models/Qwen3-14B-tdx-fast-merged
DST=/home/jiuben/models/Qwen3-14B-tdx-fast-awq
LOG=/home/jiuben/models/awq-quantize-2026-09-21.log

if [ ! -d "$SRC" ]; then
  echo "ERR: 源模型不存在: $SRC" >&2; exit 1
fi

if [ -d "$DST" ] && [ "$(ls -A "$DST" 2>/dev/null)" ]; then
  echo "WARN: 输出目录已存在且非空: $DST" >&2
  echo "      如果要重新量化, 请先 rm -rf $DST" >&2
  ls -la "$DST"
  exit 1
fi

mkdir -p "$(dirname "$LOG")"
mkdir -p "$DST"

echo "===== AWQ 量化开始 $(date '+%Y-%m-%d %H:%M:%S') =====" | tee -a "$LOG"
echo "venv:      $VENV" | tee -a "$LOG"
echo "src:       $SRC ($(du -sh "$SRC" | cut -f1))" | tee -a "$LOG"
echo "dst:       $DST" | tee -a "$LOG"
echo "log:       $LOG" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== 量化前 GPU 状态 ===" | tee -a "$LOG"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== 启动量化 (后台) ===" | tee -a "$LOG"
echo " 预计 30-60 分钟" | tee -a "$LOG"

# 用 setsid + nohup 确保父 shell 退出也不被杀
setsid nohup "$VENV/bin/python" /home/jiuben/tdx-data-feed/v5/scripts/quantize_awq.py "$SRC" "$DST" \
  > "$LOG" 2>&1 < /dev/null &
QPID=$!
echo "  PID=$QPID" | tee -a "$LOG"

# 写一个 .pid 文件, 方便后续 check / kill
echo "$QPID" > /tmp/awq-quantize.pid

echo "" | tee -a "$LOG"
echo "=== 进度轮询 (每 60s, 持续 90 分钟上限) ===" | tee -a "$LOG"

ELAPSED=0
MAX_WAIT=5400   # 90 分钟上限
PROBE_INTERVAL=60

while kill -0 "$QPID" 2>/dev/null; do
  sleep $PROBE_INTERVAL
  ELAPSED=$((ELAPSED + PROBE_INTERVAL))

  if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo "[$(date '+%H:%M:%S')] 超时 ($MAX_WAIT s), 强杀 QPID=$QPID" | tee -a "$LOG"
    kill -TERM "$QPID" 2>/dev/null || true
    sleep 5
    kill -KILL "$QPID" 2>/dev/null || true
    break
  fi

  echo "" | tee -a "$LOG"
  echo "[$(date '+%H:%M:%S')] +${ELAPSED}s 进度:" | tee -a "$LOG"
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | tee -a "$LOG"
  # tail 量化进度
  echo "--- 量化日志最近 5 行 ---" | tee -a "$LOG"
  tail -5 "$LOG" 2>/dev/null | tee -a "$LOG"
done

echo "" | tee -a "$LOG"
echo "=== 量化结束 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"

# 检查 exit status
wait "$QPID" 2>/dev/null
RC=$?

if [ $RC -eq 0 ]; then
  echo "✓ 量化成功" | tee -a "$LOG"
  echo "" | tee -a "$LOG"
  echo "=== 输出目录 ===" | tee -a "$LOG"
  ls -la "$DST" | tee -a "$LOG"
  echo "" | tee -a "$LOG"
  echo "  大小: $(du -sh "$DST" | cut -f1)" | tee -a "$LOG"
else
  echo "✗ 量化失败 RC=$RC" | tee -a "$LOG"
  echo "  tail -100 $LOG" | tee -a "$LOG"
  exit "$RC"
fi