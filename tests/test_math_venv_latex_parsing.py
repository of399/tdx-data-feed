"""数学 MCP 服务的解释器隔离：LaTeX 解析要用带 antlr4 4.11 的独立 venv。

诊断（2026-10-24 实测闭环）：`v5/mcp_servers/math_sympy.py` 与 `v5/mcp_math/parallel.py`
全走 `sympy.parsing.latex.parse_latex`，而 sympy 1.14.0 在
`sympy/parsing/latex/_parse_latex_antlr.py` 里硬性要求 `antlr4-python3-runtime` 的版本
以 `4.11` 开头；主 `venv/` 装的是 **4.9.3**，被 `omegaconf 2.3.1` 以 `==4.9.*` 锁死
（链路 `llamafactory 0.9.5 → omegaconf → antlr4`）。两个约束在同一解释器里不可同时满足，
于是 sympy 段对**任何** LaTeX 输入都抛 ImportError，回退链一路掉到 numeric/llm。
`pip check` 看不见它（antlr 对 sympy 是 optional extra），所以这件事一直是静默的。

处置（用户选定方案 A）：给数学服务一套独立解释器 `venv-math/`，只装服务端运行时
（fastapi / uvicorn / sympy + antlr4 4.11 + 两个惰性依赖），训练栈（llamafactory/omegaconf）
原样留在主 venv，一行不动。

锁五件事：
1. `v5/requirements-math.txt` 钉住 antlr4 4.11，并且**不含** llamafactory/omegaconf；
2. 系统单元的 `ExecStart` 与 `PATH` 都指向 `venv-math/bin/python`（否则改清单不改进程，等于没修）；
3. 手工拉起脚本也走同一解释器，别再出现「脚本用主 venv、服务用 math venv」两套口径；
4. `venv-math/` 在 `.gitignore` 里；
5. 主 venv 的 llamafactory/omegaconf 仍可用 —— 这套方案的代价必须是零。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATH_REQS = ROOT / "v5" / "requirements-math.txt"
MATH_PY = ROOT / "venv-math" / "bin" / "python"
UNIT = ROOT / "v5" / "systemd" / "math-sympy-http.service"


def _read(path: Path) -> str:
    assert path.exists(), f"缺少文件 {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


@pytest.mark.skipif(not MATH_PY.exists(), reason="venv-math 只在本机存在，不进 git")
def test_math_venv_imports_the_whole_server_chain():
    """清单是否齐，不看清单看导入：拿 `--help` 走一遍模块顶层就够。

    第一版清单漏了 `mcp` 与 `pylatexenc`（`math_sympy.py` 复用 stdio MCP server 的 Tool 实现），
    服务在 `from mcp.server import Server` 当场崩。只钉 antlr4 版本不足以证明这套解释器能用。
    """
    proc = subprocess.run(
        [str(MATH_PY), "v5/mcp_servers/math_sympy_http.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "No module named" not in output, f"数学 venv 缺依赖：{output}"


def test_math_requirements_pin_antlr_411_and_exclude_the_training_stack():
    """只看生效的依赖行，注释里解释「为什么不装 llamafactory」不该把测试搞红。"""
    text = _read(MATH_REQS)
    requires = [
        ln.split("#")[0].strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    joined = "\n".join(requires)
    assert "antlr4-python3-runtime==4.11" in joined, (
        "数学 venv 不钉 antlr4 4.11，sympy 的 LaTeX 解析门就过不去"
    )
    assert "sympy==" in joined, "sympy 不钉版本，主 venv 升版时这套隔离会漂"
    for banned in ("llamafactory", "omegaconf", "torch"):
        assert banned not in joined, (
            f"{banned} 不该进数学服务清单：它会把 4.9.* 的锁重新带回来"
        )


@pytest.mark.skipif(not MATH_PY.exists(), reason="venv-math 只在本机存在，不进 git")
def test_math_venv_actually_parses_latex():
    """真正的验收：在这个解释器里 `parse_latex('x + 1')` 必须算得出来。"""
    proc = subprocess.run(
        [
            str(MATH_PY),
            "-c",
            "from sympy.parsing.latex import parse_latex;"
            "print(parse_latex('x + 1'))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "requires the antlr4" not in proc.stderr, proc.stderr
    assert proc.stdout.strip() == "x + 1", proc.stdout


def test_live_service_and_its_launch_scripts_use_the_math_venv():
    unit = _read(UNIT)
    exec_line = [ln.strip() for ln in unit.splitlines() if ln.strip().startswith("ExecStart=")]
    assert exec_line, "单元里没有 ExecStart"
    assert "venv-math/bin/python" in exec_line[0], (
        f"服务仍用主 venv 起，改清单等于没修：{exec_line[0]}"
    )
    assert "venv-math/bin" in unit.split("Environment=\"PATH=")[1].split("\"")[0], (
        "PATH 没排在 venv 之前，子进程里 `python` 仍会解析到主 venv"
    )

    for rel in ("v5/scripts/kill_math_test.sh", "v5/scripts/test_pushplus_e2e.sh"):
        text = _read(ROOT / rel)
        launching = [
            ln for ln in text.splitlines() if "math_sympy_http.py" in ln and "python" in ln
        ]
        assert launching, f"{rel} 找不到拉起命令"
        bad = [ln for ln in launching if "venv-math" not in ln]
        assert not bad, f"{rel} 仍在用主 venv 手工起服务：{bad}"


def test_math_venv_is_gitignored():
    text = _read(ROOT / ".gitignore")
    assert "venv-math/" in text, "venv-math/ 没进 .gitignore，会被当成源码提交"


def test_agents_md_documents_the_two_interpreters():
    """AGENTS.md 说「Python 走仓库自己的 venv」，现在这条对一个进程不再成立。

    不写清楚，下一个 agent 会照着用 `venv/bin/python` 起数学服务，把 antlr 版本门
    重新踩回去。断言取机械锚点而非整句措辞，免得文档换个说法就红。
    """
    text = _read(ROOT / "AGENTS.md")
    assert "venv-math" in text, "AGENTS.md 没提第二套解释器"
    assert "math_sympy_http" in text, "AGENTS.md 没说清哪个进程要用 venv-math"



def test_training_stack_stays_intact_in_the_main_venv():
    """方案 A 的前提是训练线一行不动：主 venv 里 llamafactory 仍装、omegaconf 仍锁 4.9.*。"""
    if not (ROOT / "venv" / "bin" / "python").exists():
        pytest.skip("主 venv 不在本机")
    proc = subprocess.run(
        [
            str(ROOT / "venv" / "bin" / "python"),
            "-c",
            "import importlib.metadata as m, omegaconf;"
            "print(m.version('antlr4-python3-runtime'))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().startswith("4.9"), (
        f"主 venv 的 antlr 被改成了 {proc.stdout.strip()}：训练栈不该为数学服务买单"
    )
