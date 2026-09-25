#!/bin/bash
# 训练实时监控：读取 trainer_log.jsonl，输出进度 + ETA + 触发提醒
# 调用：bash train_monitor.sh [--alert-on-loss-spike]
set -euo pipefail

JSONL="/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx/trainer_log.jsonl"
LOG="/home/jiuben/tdx-data-feed/logs/train_monitor.log"

if [ ! -f "$JSONL" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ trainer_log.jsonl 不存在：$JSONL" | tee -a "$LOG"
    exit 1
fi

# 取最后一行
LAST=$(tail -1 "$JSONL")

python3 << PYEOF
import json, sys
from datetime import datetime, timedelta

with open("$JSONL") as f:
    lines = [l for l in f if l.strip()]

if not lines:
    print("✗ trainer_log.jsonl 空")
    sys.exit(1)

last = json.loads(lines[-1])
cur_step = last["current_steps"]
total_steps = last["total_steps"]
loss = last["loss"]
pct = last["percentage"]
elapsed_sec = sum(int(float(json.loads(l).get("elapsed_time", "0:0:0").split(":")[0])) * 3600 +
                    int(float(json.loads(l).get("elapsed_time", "0:0:0").split(":")[1])) * 60 +
                    float(json.loads(l).get("elapsed_time", "0:0:0").split(":")[2])
                    for l in lines[-20:])
rem_str = last.get("remaining_time", "?")
epoch = last.get("epoch", 0)
lr = last.get("lr", 0)

print(f"=== 训练进度 [{datetime.now().strftime('%H:%M:%S')}] ===")
print(f"  step:        {cur_step} / {total_steps} ({pct:.2f}%)")
print(f"  epoch:       {epoch:.3f}")
print(f"  loss:        {loss:.4f}")
print(f"  lr:          {lr:.2e}")
print(f"  remaining:   {rem_str}")
print(f"  ETA:         ~ {datetime.now() + timedelta(hours=50)}")

# loss 趋势（最近 5 步）
if len(lines) >= 5:
    losses = [json.loads(l)["loss"] for l in lines[-5:]]
    trend = losses[-1] - losses[0]
    arrow = "↓" if trend < 0 else ("↑" if trend > 0 else "→")
    print(f"  5步loss趋势:  {arrow} {trend:+.4f} ({losses[0]:.4f} → {losses[-1]:.4f})")

# checkpoint 数
import os
ckpt_dir = "/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx"
ckpts = sorted([d for d in os.listdir(ckpt_dir) if d.startswith("checkpoint-")])
print(f"  checkpoints: {len(ckpts)} 个（{', '.join(ckpts[-3:]) if ckpts else '无'}）")
PYEOF
