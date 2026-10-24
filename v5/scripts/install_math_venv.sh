#!/usr/bin/env bash
# 建 v5 数学服务专用解释器 venv-math/（antlr4 4.11），与训练栈隔离。
#
# 为什么要它：sympy 1.14 的 parse_latex 要求 antlr4-python3-runtime 4.11.*，而主 venv 的
# 4.9.3 被 omegaconf==2.3.1（llamafactory 带入）以 ==4.9.* 锁死，同一解释器不可兼得。
# 详见 v5/requirements-math.txt 与 tests/test_math_venv_latex_parsing.py。
#
# 幂等：重复执行只做「缺则建、有则按清单补装、最后自检」。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MATH_VENV="$ROOT/venv-math"
REQS="$ROOT/v5/requirements-math.txt"

# 用主 venv 的解释器建，保证两边 Python 小版本一致（否则两套环境算出的 sympy 结果可能不同）
BASE_PY="$ROOT/venv/bin/python"
[ -x "$BASE_PY" ] || BASE_PY="$(command -v python3)"

if [ ! -d "$MATH_VENV" ]; then
  echo "▶ 创建 $MATH_VENV（解释器：$BASE_PY）"
  "$BASE_PY" -m venv "$MATH_VENV"
fi

echo "▶ 装依赖（$REQS）"
"$MATH_VENV/bin/pip" install --upgrade pip --quiet
"$MATH_VENV/bin/pip" install --requirement "$REQS"

echo "▶ 自检 1：antlr4 版本必须过 sympy 的 4.11 门"
"$MATH_VENV/bin/python" - <<'PY'
import importlib.metadata as m
v = m.version("antlr4-python3-runtime")
assert v.startswith("4.11"), f"antlr4-python3-runtime={v}，sympy 要 4.11.*"
print(f"  antlr4-python3-runtime {v} ✓  sympy {m.version('sympy')}")
PY

echo "▶ 自检 2：parse_latex 必须真算得出来（这是本次修动的靶子）"
"$MATH_VENV/bin/python" - <<'PY'
from sympy.parsing.latex import parse_latex
got = str(parse_latex("x + 1"))
assert got == "x + 1", f"parse_latex 返回 {got!r}"
frac = parse_latex(r"\frac{a}{b}")
assert set(free := [str(s) for s in frac.free_symbols]) == {"a", "b"}, frac
print(f"  x + 1 ✓   \\frac{{a}}{{b}} → {frac} ✓")
PY

echo "▶ 自检 3：训练栈不该被牵进来（venv-math 里不能有 omegaconf）"
if "$MATH_VENV/bin/python" -c "import omegaconf" 2>/dev/null; then
  echo "✗ venv-math 里出现了 omegaconf：它会重新锁来 4.9.*，隔离失效" >&2
  exit 1
fi
echo "  无 omegaconf ✓"

echo
echo "✓ venv-math 就绪。生效需要（系统单元，需 sudo）："
echo "    sudo cp $ROOT/v5/systemd/math-sympy-http.service /etc/systemd/system/"
echo "    sudo systemctl daemon-reload && sudo systemctl restart math-sympy-http"
