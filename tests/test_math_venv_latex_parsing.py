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
5. 主 venv 的 llamafactory/omegaconf 仍可用 —— 这套方案的代价必须是零；
6. **扫描式**锁定「用解释器执行数学代码」的每一行：解释器只能是 venv-math。白名单会漏网
   （2026-10-25 就漏了 `v5/scripts/tdx_verification.py` 那条内层 harness）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATH_REQS = ROOT / "v5" / "requirements-math.txt"
MATH_PY = ROOT / "venv-math" / "bin" / "python"
UNIT = ROOT / "v5" / "systemd" / "math-sympy-http.service"

# 主 venv 解释器路径的字面量。拼出来而不是直写，是为了让下面那条扫描守卫不会扫到自己。
MAIN_PY = "venv" + "/bin" + "/python"
# 出现这个标记的行所在命令，说明它正在**用某个解释器执行数学栈**（math_sympy 也覆盖 test_math_sympy）。
MATH_MARKER = "math_sympy"


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


def _math_interpreter() -> str:
    """数学栈实际用哪个解释器 = 单元 ExecStart 的第一个 token。

    从单元文件现取，不写死 `venv-math`：以后再迁解释器时守卫跟着部署走，而不是反过来
    要求部署迁回守卫记住的那个路径。
    """
    exec_lines = [
        ln.strip() for ln in _read(UNIT).splitlines() if ln.strip().startswith("ExecStart=")
    ]
    assert exec_lines, "单元里没有 ExecStart，无法判定权威解释器"
    return exec_lines[0].split("=", 1)[1].split()[0]


def _tracked(prefix_globs: tuple[str, ...]) -> list[Path]:
    """git 跟踪的源文件；venv/、venv-math/、data/ 都在 .gitignore 里，天然不在扫描面上。"""
    out = subprocess.run(
        ["git", "ls-files", *prefix_globs],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return [ROOT / p for p in out.stdout.splitlines() if "_archive/" not in p]


def test_no_source_line_runs_math_code_with_the_main_venv():
    """凡是「用解释器执行数学代码」的地方，解释器必须是权威那一个。

    红过一次（2026-10-25）：`v5/scripts/tdx_verification.py` 把内层 harness 写死成主 venv，
    17 条断言当场报 `requires the antlr4 … 4.11` 并静默掉进 AST 兜底，而线上 8002 明明是
    venv-math。既有守卫只枚举「拉起 math_sympy_http.py 的脚本」，这个入口就在网外——所以这里
    不写白名单，改成扫全部跟踪的 `.py`/`.sh`（`.json` 注册片段是 command/args 分离的形状，单列
    一条）。窗口取 3 行，因为 `command=…` 与 `args=[…math_sympy.py]`
    常常不在同一行（`v5/tests/test_math_sympy.py` 的 stdio 客户端就是这种形状）。
    """
    authority = _math_interpreter()
    assert "venv-math" in authority, f"权威解释器已不是 venv-math：{authority}"

    offenders: list[str] = []
    for path in _tracked(("*.py", "*.sh")):
        if path == Path(__file__):
            continue  # 本文件里出现的 MAIN_PY 是拼出来的常量，不是启动命令
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines):
            if MAIN_PY not in line:
                continue
            window = "\n".join(lines[i : i + 3])
            if MATH_MARKER in window:
                offenders.append(f"{path.relative_to(ROOT)}:{i + 1}: {line.strip()[:100]}")

    assert not offenders, (
        "这些行用主 venv 执行数学代码：sympy 段会整片抛 ImportError 并静默降级"
        "（主 venv 的 antlr 被 omegaconf 锁在 4.9.*）。应改为权威解释器 "
        f"{authority}：\n" + "\n".join(offenders)
    )


@pytest.mark.xfail(
    reason=(
        "v5/scripts/s7-mcp-servers.json 的 math-sympy.command 仍写主 venv —— 该文件当前带"
        "另一会话未提交的改动（s7 注册名），共享工作树下不便一起提交。等那轮入库后把 command "
        "改成数学解释器并去掉本标记转正（strict=True：改对了会红，提醒转正）。"
    ),
    strict=True,
)
def test_mcp_registration_fragment_points_math_server_at_the_math_venv():
    frag = json.loads(_read(ROOT / "v5" / "scripts" / "s7-mcp-servers.json"))
    authority = _math_interpreter()
    for name, cfg in frag["mcpServers"].items():
        args = " ".join(cfg.get("args", []))
        if MATH_MARKER in args:
            assert cfg.get("command") == authority, (
                f"{name} 用 {cfg.get('command')} 起数学 stdio server，权威解释器是 {authority}"
            )
