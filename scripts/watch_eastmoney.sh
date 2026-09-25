#!/bin/bash
# Eastmoney recovery monitor + auto-trigger core-full-em min_sync
# Path: /home/jiuben/tdx-data-feed/scripts/watch_eastmoney.sh
#
# Cron entry: */30 * * * * bash /home/jiuben/tdx-data-feed/scripts/watch_eastmoney.sh

set -euo pipefail

LOG=/home/jiuben/tdx-data-feed/logs/watch_eastmoney.log
LOCK=/home/jiuben/tdx-data-feed/logs/.eastmoney-watch.lock
LAST_RUN=/home/jiuben/tdx-data-feed/logs/.eastmoney-last-run
MIN_INTERVAL=1800
STATE_OK=/home/jiuben/tdx-data-feed/logs/.eastmoney-recovered

ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG"
}

# probe interval check
if [ -f "$LAST_RUN" ]; then
    last_ts=$(stat -c %Y "$LAST_RUN")
    now_ts=$(date +%s)
    diff=$(( now_ts - last_ts ))
    if [ "$diff" -lt "$MIN_INTERVAL" ]; then
        log "skip: last check $diff seconds ago (< $MIN_INTERVAL)"
        exit 0
    fi
fi

# lock against concurrent runs
if [ -f "$LOCK" ]; then
    log "skip: lock exists ($LOCK)"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

# probe Eastmoney
http_code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&fields1=f1&fields2=f51&klt=101&fqt=0&beg=20240101&end=20261231" 2>&1 || echo "ERR")
size=$(curl -sS -o /dev/null -w "%{size_download}" --max-time 10 \
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&fields1=f1&fields2=f51&klt=101&fqt=0&beg=20240101&end=20261231" 2>&1 || echo "0")

log "probe: http=$http_code size=$size"

if [ "$http_code" = "200" ] && [ "$size" -gt 50 ]; then
    log "OK: Eastmoney RECOVERED (http 200, size $size)"
    if [ -f "$STATE_OK" ]; then
        log "skip: already triggered (state file $STATE_OK exists)"
    else
        log "trigger: starting min_sync2.py --mode core-full-em (background)"
        cd /home/jiuben/tdx-data-feed
        nohup setsid /home/jiuben/tdx-data-feed/venv/bin/python \
            min_sync2.py --mode core-full-em \
            > logs/min_core_full_em.log 2>&1 < /dev/null &
        disown
        touch "$STATE_OK"
        log "OK: triggered pid=$!"
    fi
else
    log "FAIL: Eastmoney still down (http $http_code, size $size)"
fi

touch "$LAST_RUN"
log "next check in $MIN_INTERVAL seconds"