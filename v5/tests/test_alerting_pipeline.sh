#!/usr/bin/env bash
# v5 告警链路验证 CLI wrapper
# 封装 pytest + 自动 sink 起/停 + JSON 报告 + 退出码约定
#
# 用法:
#   ./v5/tests/test_alerting_pipeline.sh                          # quick (5s, 非侵入式)
#   ./v5/tests/test_alerting_pipeline.sh --mode full              # full (140s, 侵入式 kill -STOP)
#   ./v5/tests/test_alerting_pipeline.sh --mode full --auto-sink  # full + 自动起/停 webhook sink
#   ./v5/tests/test_alerting_pipeline.sh --report-json /tmp/r.json
#
# 退出码:
#   0  = 全部 PASS
#   1  = 至少 1 个 FAIL
#   2  = 启动错误（前置条件不满足）
#   130 = 用户 Ctrl-C
set -uo pipefail

PROJ_ROOT=/home/jiuben/tdx-data-feed
TESTS_DIR=$PROJ_ROOT/v5/tests
PYTEST_BIN=$PROJ_ROOT/venv/bin/pytest
TEST_FILE=$TESTS_DIR/test_alerting_pipeline.py
REPORT_DEFAULT=/tmp/alerting-pipeline-report.json

# ---------- args ----------
MODE="quick"           # quick | full
AUTO_SINK=0
REPORT_JSON=""
EXTRA_ARGS=()

usage() {
  sed -n '3,11p' "$0"
  echo ""
  echo "选项:"
  echo "  --mode MODE         quick (5s) 或 full (140s 侵入式)"
  echo "  --auto-sink         full 模式自动起/停 webhook sink 在 :5001"
  echo "  --report-json PATH  输出 JSON 报告 (default: $REPORT_DEFAULT)"
  echo "  --help              显示本帮助"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)        MODE="$2"; shift 2 ;;
    --auto-sink)   AUTO_SINK=1; shift ;;
    --report-json) REPORT_JSON="$2"; shift 2 ;;
    --help|-h)     usage 0 ;;
    -v|-s|--tb=short|--tb=long)  EXTRA_ARGS+=("$1"); shift ;;
    *)             echo "ERR unknown arg: $1" >&2; usage 1 ;;
  esac
done

[[ "$MODE" == "quick" || "$MODE" == "full" ]] || { echo "ERR invalid --mode: $MODE" >&2; exit 2; }

if [[ -z "$REPORT_JSON" ]]; then
  REPORT_JSON="$REPORT_DEFAULT"
fi

# ---------- preflight ----------
[[ -f "$PYTEST_BIN" ]] || { echo "ERR $PYTEST_BIN not found" >&2; exit 2; }
[[ -f "$TEST_FILE" ]] || { echo "ERR $TEST_FILE not found" >&2; exit 2; }

cd "$PROJ_ROOT" || { echo "ERR: cannot cd to $PROJ_ROOT" >&2; exit 2; }

echo "=== 告警链路验证 ==="
echo "  mode:        $MODE"
echo "  auto-sink:   $AUTO_SINK"
echo "  report:      $REPORT_JSON"
echo "  test file:   $TEST_FILE"
echo ""

# ---------- env ----------
export REPORT_JSON="$REPORT_JSON"
EXTRA_ENV=()
if [[ "$MODE" == "full" ]]; then
  export VERIFY_PIPELINE_FULL=1
  if [[ "$AUTO_SINK" == "1" ]]; then
    export AUTOSINK=1
  fi
fi

# ---------- run ----------
case "$MODE" in
  quick) PYTEST_TARGET="$TEST_FILE" ;;
  full)  PYTEST_TARGET="$TEST_FILE::TestPipelineKillStop" ;;
esac

START=$(date +%s)
"$PYTEST_BIN" "$PYTEST_TARGET" -v "${EXTRA_ARGS[@]}" 2>&1
RC=$?
END=$(date +%s)
ELAPSED=$((END - START))

echo ""
echo "=== 完成 (耗时 ${ELAPSED}s, exit=$RC) ==="

# ---------- 报告落盘 (我们自己生成, 不依赖 pytest hook) ----------
cat > "$REPORT_JSON" <<JSON_EOF
{
  "started_at":  "$(date -d @$START '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date -r $START '+%Y-%m-%dT%H:%M:%S%z')",
  "finished_at": "$(date -d @$END   '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date -r $END   '+%Y-%m-%dT%H:%M:%S%z')",
  "elapsed_s":   $ELAPSED,
  "mode":        "$MODE",
  "auto_sink":   $AUTO_SINK,
  "exit_code":   $RC,
  "result":      "$([ $RC -eq 0 ] && echo PASS || echo FAIL)"
}
JSON_EOF
echo ""
echo "=== JSON 报告: $REPORT_JSON ==="
cat "$REPORT_JSON"
echo ""

# 退出码透传 (让 sop-weekly / CI 能正确判断 fail)
exit "$RC"