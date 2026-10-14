#!/usr/bin/env bash
# v9 DDP 训练看门狗（监控/接管双模式）
# - 若训练已在跑：仅监控，直到其退出再接管（不会重复启动双份训练）
# - 接管/重启前：清理孤儿进程 + 剥离最新 checkpoint 的 optimizer/scheduler/rng 状态
#   （规避续训时 torch C++ 层 "invalid argument at line 580 in pythonInterface.cpp" 的 CUDA 错误，
#    保留 LoRA 权重，以全新 optimizer/rng 续训）
# - 干净退出(rc=0, 即 Training completed)时停止；否则最多重试 MAX 次，每次保留详细日志。
set -u
ROOT=/home/jiuben/tdx-data-feed
CFG=training/config/tdx_v9_from_scratch.yaml
WLOG="$ROOT/logs/v9_watchdog.log"
TLOG="$ROOT/logs/train_tdx_v9_from_scratch.log"
OUT="$ROOT/train/output/qwen3-14b-tdx-v9"
MAX=20
cd "$ROOT" || exit 1

is_running() { pgrep -f "training/config/tdx_v9_from_scratch.yaml" >/dev/null; }
cleanup() { pkill -9 -f "training/config/tdx_v9_from_scratch.yaml" 2>/dev/null; sleep 3; }
strip_opt_state() {
  local ck
  ck=$(ls -d "$OUT"/checkpoint-* 2>/dev/null | sort -t- -k2 -n | tail -1)
  [ -z "$ck" ] && return 0
  mkdir -p "$ck/_optstate_backup"
  for f in optimizer.pt scheduler.pt rng_state_0.pth rng_state_1.pth; do
    [ -f "$ck/$f" ] && mv "$ck/$f" "$ck/_optstate_backup/" 2>/dev/null
  done
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] stripped optimizer/rng state from $ck" >> "$WLOG"
}

echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] start (monitor-or-launch mode)" >> "$WLOG"

if is_running; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] training already running — monitoring only" >> "$WLOG"
  while is_running; do sleep 30; done
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] training exited — entering restart loop" >> "$WLOG"
fi

n=0
while [ "$n" -lt "$MAX" ]; do
  n=$((n+1))
  cleanup
  strip_opt_state
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  echo "$ts [watchdog] attempt $n/$MAX -> launching train_lora.py" >> "$WLOG"
  venv/bin/python training/scripts/train_lora.py "$CFG" >> "$WLOG" 2>&1
  rc=$?
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  cp -f "$TLOG" "$ROOT/logs/v9_train_attempt_${n}.log" 2>/dev/null
  echo "$ts [watchdog] train_lora exited rc=$rc (detail -> v9_train_attempt_${n}.log)" >> "$WLOG"
  if [ "$rc" -eq 0 ]; then
    echo "$ts [watchdog] clean exit (training completed) -> stop watchdog" >> "$WLOG"
    break
  fi
  echo "$ts [watchdog] non-zero exit; retry in 45s" >> "$WLOG"
  sleep 45
done
[ "$n" -ge "$MAX" ] && echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] gave up after $MAX attempts — manual intervention needed" >> "$WLOG"
