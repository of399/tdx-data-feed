#!/usr/bin/env bash
# v5/scripts/sop-weekly.sh · 每周一 09:00 跑告警链路 quick 验证
# 设计参考: v5/docs/cron-sop.md
# 接入: crontab -e 加 '0 9 * * 1 bash /home/jiuben/tdx-data-feed/v5/scripts/sop-weekly.sh'
# 输出: /tmp/sop-weekly.log + /tmp/sop-weekly-report.json
set -uo pipefail

PROJ=/home/jiuben/tdx-data-feed
LOG=/tmp/sop-weekly.log
REPORT=/tmp/sop-weekly-report.json
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== [$TS] sop-weekly start ===" >> "$LOG"
bash "$PROJ/v5/tests/test_alerting_pipeline.sh" \
  --report-json "$REPORT" >> "$LOG" 2>&1
RC=$?
echo "=== [$TS] sop-weekly end rc=$RC report=$REPORT ===" >> "$LOG"

# 失败时（rc != 0）写哨兵文件, 供后续告警接入用
if [ "$RC" -ne 0 ]; then
    echo "$TS rc=$RC" > /tmp/sop-weekly.FAILED
fi

exit "$RC"