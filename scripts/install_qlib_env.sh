#!/usr/bin/env bash
# 建 qlib 专用解释器 venv-qlib/（Python 3.12 + pyqlib），与主 venv/训练栈完全隔离。
#
# 为什么要它：pyqlib 0.9.7（PyPI 最新版）的 wheel tag 只到 cp312，主 venv 是
# Python 3.14.4 装不上；而且它的依赖树（mlflow / gym / jupyter / pymongo / redis /
# cvxpy）会污染训练栈与 CI 门禁的精简集 requirements-ci.txt。详见
# requirements-qlib.txt 与 tests/test_qlib_env_island.py。
#
# 这个环境只做一件事：借 qlib 的表达式引擎产出 Alpha158 的「表达式字符串」，
# 数值计算回主 venv 做。**不要**在这里 import tdxfeed，也不要在这里下 qlib 社区
# 数据包（cn_data 的复权口径不受控，禁止）。
#
# 幂等：重复执行只做「缺则建、有则按清单补装、最后自检」。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QLIB_VENV="$ROOT/venv-qlib"
REQS="$ROOT/requirements-qlib.txt"
export QLIB_VENV

# conda 前缀环境不像 venv 那样自动关掉 PEP 370 用户站点目录：不关的话 pip 会说
# 「Requirement already satisfied: pyarrow in ~/.local/lib/python3.12/site-packages」，
# 岛就悄悄长在别人的 site-packages 上了（换台机器、或者那人清一下 ~/.local 就散架）。
# pip 解析与自检导入都要吃这个变量，所以放在最前面 export。
export PYTHONNOUSERSITE=1

# conda 不在 PATH 就退回本机绝对路径；两处都没有就明确失败——不要静默改用系统
# python3，那会装出一个 Python 版本不对、且没有 wheel 可用（cp312 门）的环境。
CONDA="$(command -v conda 2>/dev/null || true)"
[ -n "$CONDA" ] || CONDA="/opt/miniforge3/bin/conda"
if [ ! -x "$CONDA" ]; then
  echo "✗ 找不到 conda（PATH 与 /opt/miniforge3/bin/conda 均无）：venv-qlib 需要 Python 3.12，本机系统解释器（3.14）给不出，且 pyqlib 无 cp314 wheel" >&2
  exit 1
fi

if [ ! -x "$QLIB_VENV/bin/python" ]; then
  echo "▶ 创建 $QLIB_VENV（$CONDA create -y --prefix ... python=3.12）"
  "$CONDA" create -y --prefix "$QLIB_VENV" python=3.12 --no-default-packages
else
  echo "▶ $QLIB_VENV 已存在，跳过创建（幂等）"
fi

echo "▶ 装依赖（$REQS）"
"$QLIB_VENV/bin/pip" install --upgrade pip --quiet
"$QLIB_VENV/bin/pip" install --requirement "$REQS"

echo "▶ 自检 1：解释器与钉住的依赖版本"
export REQS
"$QLIB_VENV/bin/python" - <<'PY'
import os
import sys
from importlib.metadata import version as v
from pathlib import Path

assert sys.version_info[:2] == (3, 12), f"Python 是 {sys.version.split()[0]}，pyqlib wheel 只到 cp312"
# 版本判据直接读 requirements-qlib.txt，不在这里抄第二份清单：金标准
# （config/alpha158_ops.json）的 provenance 记的是 pandas/numpy 的具体版本，
# 守卫 tests/test_alpha158_semantics.py 拿它对账，装错岛必须在这里就挡住，
# 而不是等工件与解释器各说各话。
pins = {}
for line in Path(os.environ["REQS"]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "==" in line:
        name, _, want = line.partition("==")
        pins[name.strip()] = want.strip()
assert pins, f"{os.environ['REQS']} 里没有任何 == 钉版，自检等于没跑"
for dist, want in pins.items():
    got = v(dist)
    assert got == want, f"{dist}={got}，清单钉 {want}：与已入库金标准的口径不一致，按清单重装或重导工件"
print(f"  Python {sys.version.split()[0]} ✓  " + "  ".join(f"{d} {w} ✓" for d, w in pins.items()))
PY

echo "▶ 自检 2：Alpha158 表达式引擎必须真出得来 158 条（9 kbar + 4 price + 29 rolling×5 窗）"
"$QLIB_VENV/bin/python" - <<'PY'
import os
import sys
from qlib.contrib.data.loader import Alpha158DL

# 岛必须是自己完整的一个：qlib 与所有依赖都得落在 venv-qlib/ 里，不许借 ~/.local
island = os.environ["QLIB_VENV"]
assert __import__("qlib").__file__.startswith(island), __import__("qlib").__file__
borrowed = [p for p in sys.path if ".local/lib" in p]
assert not borrowed, f"岛在读用户站点目录，换机器就散架：{borrowed}"

fields, names = Alpha158DL.get_feature_config()
assert len(fields) == 158, f"表达式条数 {len(fields)}，Alpha158 应为 158"
assert len(names) == len(fields), f"names {len(names)} 与 fields {len(fields)} 不齐"
assert len(set(names)) == len(names), "因子名有重复，导出器按名字建列会撞车"
assert all(isinstance(f, str) for f in fields), "表达式不是字符串，跨解释器传递的契约断了"
print(f"  {len(fields)} 条表达式 ✓  例如 {names[0]}={fields[0]} / {names[-1]}={fields[-1]}")
PY

echo "▶ 自检 3：主 venv 里 qlib 必须装不上/导入不了（隔离的反向证明）"
MAIN_PY="$ROOT/venv/bin/python"
if [ -x "$MAIN_PY" ]; then
  # 这里刻意把 PYTHONNOUSERSITE 撤掉：借用户站点目录能装上的 qlib 也算泄漏。
  if env -u PYTHONNOUSERSITE "$MAIN_PY" -c "import qlib" 2>/dev/null; then
    echo "✗ 主 venv 里居然 import 得动 qlib：隔离失效，生产读路径随时会被牵进这套依赖" >&2
    exit 1
  fi
  echo "  主 venv 无 qlib ✓"
else
  echo "  （主 venv 不在这台机器上，跳过自检 3）"
fi

echo
echo "✓ venv-qlib 就绪。用法：$QLIB_VENV/bin/python（只跑 qlib 侧；不要下 cn_data，不要写本仓库 data/）"
