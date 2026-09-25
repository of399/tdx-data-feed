#!/bin/bash
# train_health.sh — 训练健康监测（崩溃 / loss 异常 / 新 ckpt 自动写 DELIVERY）
# v3: 按进程反查目录（更鲁棒），跳过孤儿目录（带 .no_monitor 标记）
# 触发：systemd user timer 每 5 分钟（或手动跑）

set -uo pipefail

BASE="/home/jiuben/tdx-data-feed"
OUT_BASE="$BASE/train/output"
LOG="$BASE/logs/train_health.log"
DELIVERY="$BASE/DELIVERY.md"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG"; }
alert() {
    log "ALERT: $*"
    systemd-cat -t tdx-train-health echo "[$(ts)] ALERT: $*" 2>/dev/null || true
}

mkdir -p "$(dirname "$LOG")"
echo "===========================================" >> "$LOG"
log "scan-start: 多训练扫描（按进程反查）"

# 1) 找所有训练 wrapper 进程（pgrep 匹配 _train_single.py）
mapfile -t PIDS < <(pgrep -f '_train_single\.py' 2>/dev/null | sort -u)

if [ ${#PIDS[@]} -eq 0 ]; then
    log "✗ 未发现任何 *_train_single.py 进程"
    alert "所有训练进程都不在（wrapper 全挂？）"
    exit 1
fi

log "✓ 发现 ${#PIDS[@]} 个训练 wrapper PID: ${PIDS[*]}"

# 2) 每个 PID 反查对应目录
declare -A SEEN_DIRS
for PID in "${PIDS[@]}"; do
    # 从 /proc/PID/cmdline 提取 wrapper 名
    CMD=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null)
    WRAPPER=$(echo "$CMD" | grep -oE 'v[0-9]+_train_single\.py' | head -1)
    if [ -z "$WRAPPER" ]; then
        log "[PID=$PID] ✗ 无法从 cmdline 提取 wrapper 名: $CMD"
        alert "PID=$PID cmdline 解析失败"
        continue
    fi
    SHORT=$(echo "$WRAPPER" | sed -E 's/_train_single\.py$//')  # v5 / v6

    # 找对应训练目录：qwen3-14b-tdx-vN-*
    OUT_DIR=$(find "$OUT_BASE" -maxdepth 1 -type d -name "qwen3-14b-tdx-${SHORT}-*" -print -quit 2>/dev/null)
    # 退化匹配：qwen3-14b-tdx-vN （无 -xxx 后缀）
    if [ -z "$OUT_DIR" ]; then
        OUT_DIR=$(find "$OUT_BASE" -maxdepth 1 -type d -name "qwen3-14b-tdx-${SHORT}" -print -quit 2>/dev/null)
    fi
    if [ -z "$OUT_DIR" ]; then
        log "[$WRAPPER PID=$PID] ✗ 在 $OUT_BASE 下找不到对应目录（请确认目录命名约定 qwen3-14b-tdx-${SHORT}-*）"
        alert "[$WRAPPER] 对应目录不存在"
        continue
    fi

    TAG=$(basename "$OUT_DIR")
    SEEN_DIRS["$TAG"]=1   # 标记已扫描

    JSONL="$OUT_DIR/trainer_log.jsonl"
    HEALTH_FILE="$OUT_DIR/.health_state"
    CKPT_WATCHED="$OUT_DIR/.ckpt_watched"

    log "─── [$TAG] PID=$PID 检查开始 ───"

    # 3) 读 jsonl 最新状态
    if [ ! -f "$JSONL" ]; then
        log "[$TAG] 训练在跑但 trainer_log.jsonl 未生成（初始化阶段）"
        touch "$HEALTH_FILE"
        continue
    fi

    LAST=$(tail -1 "$JSONL")
    STEP=$(echo "$LAST" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('current_steps', d.get('step', '?')))" 2>/dev/null || echo "?")
    LOSS=$(echo "$LAST" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['loss'])" 2>/dev/null || echo "?")

    # 4) loss 异常检测
    source "$BASE/venv/bin/activate"
    LOSS_REPORT=$(python3 - "$JSONL" "$TAG" << 'PYEOF' 2>&1
import json, sys
JSONL, TAG = sys.argv[1], sys.argv[2]
with open(JSONL) as f:
    rows = [json.loads(l) for l in f if l.strip()]
if len(rows) < 20:
    print(f"[{TAG}] 数据不足（{len(rows)} 行），跳过 loss 异常检测")
    sys.exit(0)
losses = [r["loss"] for r in rows]
recent5 = losses[-5:]
recent_mean = sum(recent5) / 5
hist = losses[:-5]
hist_mean = sum(hist) / len(hist)
hist_std = (sum((l - hist_mean) ** 2 for l in hist) / len(hist)) ** 0.5
threshold = hist_mean + 2 * hist_std
if recent_mean > threshold and recent_mean > 0.5:
    print(f"[{TAG}] ALERT: loss 突增！recent5_mean={recent_mean:.4f} > {threshold:.4f} (hist_mean={hist_mean:.4f} + 2σ={2*hist_std:.4f})")
    sys.exit(10)
print(f"[{TAG}] OK: loss 健康 (recent5={recent_mean:.4f}, hist_mean={hist_mean:.4f} ± {hist_std:.4f})")
PYEOF
    )
    RC=$?
    log "$LOSS_REPORT"

    if [ $RC -eq 10 ]; then
        alert "[$TAG] loss 突增！recent5=$LOSS > threshold"
    fi

    # 5) 检测新 checkpoint
    NEW_CKPTS=$(find "$OUT_DIR" -maxdepth 1 -name "checkpoint-*" -type d -newer "$HEALTH_FILE" 2>/dev/null || true)
    if [ -n "$NEW_CKPTS" ]; then
        log "[$TAG] 新 checkpoint: $(echo $NEW_CKPTS | wc -w) 个"
        for ck in $NEW_CKPTS; do
            ckname=$(basename "$ck")
            cksize=$(du -sh "$ck" 2>/dev/null | cut -f1)
            log "[$TAG] plot: running plot_loss.py $OUT_DIR"
            if /home/jiuben/tdx-data-feed/scripts/plot_loss.py "$OUT_DIR" >> "$LOG" 2>&1; then
                log "[$TAG] plot: PNG ready"
            else
                log "[$TAG] plot: FAIL"
                alert "[$TAG] plot_loss.py 失败"
            fi

            # 首 ckpt 写入 DELIVERY
            if [ ! -f "$CKPT_WATCHED" ]; then
                python3 << DELPYEOF
fp = "$DELIVERY"
tag = "$TAG"
ckname = "$ckname"
ckpath = "$ck"
cksize = "$cksize"
ts_now = "$(ts)"
with open(fp, 'r', encoding='utf-8') as f:
    text = f.read()
new_section = f"""
### 6.2.{tag[-1]} 首 checkpoint 记录（{ts_now} 自动追加）
- **训练**: \`{tag}\`
- **首 ckpt**: \`{ckname}\`
- **路径**: \`{ckpath}\`
- **大小**: {cksize}
- **触发**: train_health.sh 检测到新 ckpt 自动写入
"""
old = "### 6.3 监控"
if old in text:
    if ckname in text:
        print(f"[{tag}] ckpt {ckname} 已在 DELIVERY，跳过")
    else:
        new = new_section + "\n" + old
        text = text.replace(old, new, 1)
        with open(fp, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"[{tag}] ✓ DELIVERY.md 已写入首 ckpt {ckname}")
else:
    print(f"[{tag}] §6.3 未找到，跳过 DELIVERY 写入")
DELPYEOF
                touch "$CKPT_WATCHED"
                log "[$TAG] ✓ DELIVERY.md 已更新"
            fi
        done
    fi

    touch "$HEALTH_FILE"
    log "[$TAG] OK: 健康检查通过 (step=$STEP loss=$LOSS)"
done

# 6) 检查孤儿目录（存在但无 wrapper 进程）→ 标 .no_monitor 但不报警
while IFS= read -r d; do
    TAG=$(basename "$d")
    if [ -z "${SEEN_DIRS[$TAG]:-}" ] && [ ! -f "$d/.no_monitor" ]; then
        log "[orphan] $TAG 没有对应 wrapper 进程（标 .no_monitor 跳过监控）"
        touch "$d/.no_monitor"
    fi
done < <(find "$OUT_BASE" -maxdepth 1 -type d -name "qwen3-14b-tdx-v*" 2>/dev/null)

log "scan-end: 完成"
echo "===========================================" >> "$LOG"