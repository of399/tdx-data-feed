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

# 方案 B 2026-10-16：v4fwd sanity 集成（acc/sp/cls ≥ 0.33 判停训）
SANITY_DIR="$ROOT/logs/v4fwd_sanity"
SANITY_CKPT_NAME="checkpoint-200"
mkdir -p "$SANITY_DIR"

maybe_run_sanity() {
  local ck="$OUT/$SANITY_CKPT_NAME"
  local sd="$SANITY_DIR/$SANITY_CKPT_NAME"
  [ -d "$ck" ] || return 0
  [ -f "$sd.PASS" ] && return 0     # 已通过
  [ -f "$sd.FAILED" ] && return 0    # 已失败（不再重试）
  [ -f "$sd.RUNNING" ] && return 0   # 已在跑
  touch "$sd.RUNNING"
  echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] 检测到 $SANITY_CKPT_NAME 落地,启动后台 sanity" >> "$WLOG"
  (
    venv/bin/python training/eval/run_v4_eval.py \
      --ckpt "$ck" --limit 200 --no-backtest \
      --out-prefix "$SANITY_CKPT_NAME" > "$sd.stdout" 2>&1
    local rc=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] run_v4_eval rc=$rc" >> "$WLOG"
    if [ "$rc" -ne 0 ]; then
      touch "$sd.FAILED"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] FAILED (rc=$rc,run_v4_eval 异常)" >> "$WLOG"
      exit 0
    fi
    local mfile
    mfile=$(ls "$ROOT/training/eval/metrics_${SANITY_CKPT_NAME}_"*.json 2>/dev/null | tail -1)
    if [ -z "$mfile" ]; then
      touch "$sd.FAILED"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] FAILED (no metrics_$SANITY_CKPT_NAME.json)" >> "$WLOG"
      exit 0
    fi
    local pass
    pass=$(python3 -c "
import json
m = json.load(open('$mfile'))
ok = (m.get('direction_accuracy', 0) >= 0.33
      and m.get('spearman', 0) >= 0.33
      and m.get('cls_acc_up', 0) >= 0.33
      and m.get('cls_acc_down', 0) >= 0.33
      and m.get('cls_acc_flat', 0) >= 0.33)
print(int(ok))
")
    if [ "$pass" = "1" ]; then
      touch "$sd.PASS"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] PASS (5 指标均 ≥ 0.33)" >> "$WLOG"
    else
      touch "$sd.FAILED"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [sanity] FAILED (5 指标不达标,见 $sd.stdout)" >> "$WLOG"
    fi
  ) &
  disown 2>/dev/null || true
}

check_sanity_result() {
  local sd="$SANITY_DIR/$SANITY_CKPT_NAME"
  [ -f "$sd.FAILED" ] || return 0
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] sanity 失败 — 杀训练(不重启)" >> "$WLOG"
  pkill -TERM -f "training/config/tdx_v9_from_scratch.yaml" 2>/dev/null
  sleep 5
  pkill -9 -f "training/config/tdx_v9_from_scratch.yaml" 2>/dev/null
  return 1
}

if is_running; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] training already running — monitoring only" >> "$WLOG"
  while is_running; do
    sleep 30
    maybe_run_sanity
    check_sanity_result || break
  done
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
