#!/bin/bash
# GPU 温度采样监控脚本
# Path: /home/jiuben/tdx-data-feed/scripts/monitor_gpu_temp.sh
#
# Usage: bash monitor_gpu_temp.sh <duration_sec> <interval_sec>
#   default: 1800 sec (30 min) duration, 30 sec interval
#
# Writes CSV to logs/gpu_temp_monitor_<timestamp>.log:
#   iso_time,gpu0_temp,gpu0_util,gpu1_temp,gpu1_util,gpu0_mem,gpu1_mem

set -euo pipefail

DURATION="${1:-1800}"     # 30 min default
INTERVAL="${2:-30}"       # 30 sec default
LOG_DIR=/home/jiuben/tdx-data-feed/logs
LOG="$LOG_DIR/gpu_temp_monitor_$(date '+%Y%m%d_%H%M%S').log"
PIDFILE="$LOG_DIR/.gpu_temp_monitor.pid"

mkdir -p "$LOG_DIR"

# header
echo "iso_time,gpu0_temp_c,gpu0_util_pct,gpu0_mem_mib,gpu1_temp_c,gpu1_util_pct,gpu1_mem_mib" > "$LOG"

start_ts=$(date +%s)
end_ts=$(( start_ts + DURATION ))
count=0

# trap to clean pidfile
trap 'rm -f "$PIDFILE"' EXIT
echo $$ > "$PIDFILE"

echo "[monitor] started at $(date '+%Y-%m-%d %H:%M:%S')"
echo "[monitor] duration=${DURATION}s interval=${INTERVAL}s -> $LOG"

while [ "$(date +%s)" -lt "$end_ts" ]; do
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    # query both GPUs in one call
    out=$(nvidia-smi --query-gpu=index,temperature.gpu,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null || echo "")
    if [ -n "$out" ]; then
        # parse two lines
        g0=$(echo "$out" | awk -F, '$1==0 {print $2","$3","$4}')
        g1=$(echo "$out" | awk -F, '$1==1 {print $2","$3","$4}')
        echo "$ts,${g0},${g1}" >> "$LOG"
        count=$(( count + 1 ))
    else
        echo "$ts,NA,NA,NA,NA,NA,NA" >> "$LOG"
    fi
    sleep "$INTERVAL"
done

echo "[monitor] stopped at $(date '+%Y-%m-%d %H:%M:%S') ($count samples)" >> "$LOG"