#!/usr/bin/env bash
# ============================================================
# v5-VE1-M2 · S8 缓存预热脚本
# 启动时预填常用 LaTeX 对，避免首次冷启动 simplify 卡顿
# ============================================================
set -euo pipefail

VENV_PY="/home/jiuben/tdx-data-feed/venv/bin/python"
V5_ROOT="/home/jiuben/tdx-data-feed/v5"
CACHE_DIR="$V5_ROOT/cache"
AUDIT_DIR="$V5_ROOT/audit"

mkdir -p "$CACHE_DIR" "$AUDIT_DIR"

# 常用 LaTeX 等价对（预热 = 首次等价判定 + 写缓存）
PAIRS=(
  '\int x dx'            '\frac{x^{2}}{2}'
  '\sin^{2}x + \cos^{2}x' '1'
  'e^{x}'                 '\exp x'
  '\frac{d}{dx} x^{2}'    '2 x'
  '\sum_{i=1}^{n} i'      '\frac{n(n+1)}{2}'
  '\log e'                '1'
  '\sqrt{4}'              '2'
  '(a+b)^{2}'             'a^{2} + 2ab + b^{2}'
)

echo "[S8] 缓存预热：$((${#PAIRS[@]}/2)) 对常用 LaTeX"
COUNT=0
for ((i=0; i<${#PAIRS[@]}; i+=2)); do
  A="${PAIRS[i]}"
  B="${PAIRS[i+1]}"
  if "$VENV_PY" - "$A" "$B" <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, "/home/jiuben/tdx-data-feed/v5")
from mcp_math.math_cache import prefilter, set_cached
a, b = sys.argv[1], sys.argv[2]
if not prefilter(a, b).get("reject"):
    set_cached(a, b, "equiv", {"equivalent": True, "method": "prewarm"})
PY
  then
    COUNT=$((COUNT+1))
  fi
done
echo "[S8] 预热完成：$COUNT / $((${#PAIRS[@]}/2)) 对已入缓存"

# 缓存统计
echo "[S8] 缓存当前状态："
"$VENV_PY" "$V5_ROOT/mcp_math/math_cache.py" || true

# 审计目录就绪
echo "[S8] 审计日志目录：$AUDIT_DIR"
ls -ld "$AUDIT_DIR"