"""守卫：v5 数学服务的集成测试不得依赖活体的 8002 部署。

为什么要钉住这条：`v5/tests/test_ask_path_integration.py` 原来把
`API = "http://127.0.0.1:8002"` 写死，于是「sympy 段到底能不能解析 LaTeX」
这个判定寄生在**线上那台服务用的是哪套解释器**上——要让测试变绿必须先
用 root 换系统单元，绿灯因此证明不了代码、只证明了运维动作做过。
改为自起自停后，同一套断言在任何无特权 shell 里都能独立成立。
"""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
V5_TESTS = ROOT / "v5" / "tests"
CONFTEST = V5_TESTS / "conftest.py"
UNIT = ROOT / "v5" / "systemd" / "math-sympy-http.service"

LIVE_8002 = re.compile(r"127\.0\.0\.1:8002")


def _collected_test_files():
    """只看 pytest 真会收集的文件。

    `test_math_sympy_http.py` / `test_d4_ontology.py` 是 `__main__` 门后的验收脚本
    （模块级 0 个 `def test_`，pytest 收不到用例），人工跑时故意打线上部署，
    不属于「自动化绿灯的前提」，所以不在本条规则内。
    """
    files = []
    for path in sorted(V5_TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*def test_|^\s*class Test", text, re.M):
            files.append((path, text))
    return files


def test_no_collected_v5_test_hardcodes_the_live_8002_deployment():
    """被 pytest 收集的 v5 测试都不该出现 8002 字面量（含 docstring）。

    线上服务归 systemd（root）管，自动化绿灯不该把它当前提。
    """
    offenders = {}
    for path, text in _collected_test_files():
        hits = [ln for ln in text.splitlines() if LIVE_8002.search(ln)]
        if hits:
            offenders[path.relative_to(ROOT).as_posix()] = hits
    assert _collected_test_files(), "v5/tests 一个被收集的文件都没有，规则失去意义"
    assert not offenders, f"仍写死活体部署，测试结论会寄生在线上服务的解释器上：{offenders}"


def test_conftest_starts_the_service_from_the_committed_unit():
    """自起的实例必须跑「单元文件里那条命令」，而不是另抄一份口径。

    否则测试绿的是 fixture 里手写的解释器，单元真换成别的照样发现不了。
    """
    assert CONFTEST.exists(), "缺 v5/tests/conftest.py，集成测试无处自起服务"
    text = CONFTEST.read_text(encoding="utf-8")
    missing = [a for a in ("math-sympy-http.service", "ExecStart", "Environment") if a not in text]
    assert not missing, f"conftest 未从单元文件解析启动口径，缺 {missing}"


def test_unit_still_points_at_the_math_venv():
    """单元本身也得守住 venv-math——这是自起实例能解析 LaTeX 的前提。"""
    exec_line = next(
        (ln for ln in UNIT.read_text(encoding="utf-8").splitlines() if ln.startswith("ExecStart")),
        "",
    )
    assert "venv-math/bin/python" in exec_line, f"ExecStart 不是数学 venv：{exec_line!r}"


@pytest.mark.parametrize("rel", ["test_ask_path_integration.py"])
def test_the_http_integration_suite_takes_the_injected_base_url(rel):
    """测试模块应通过 fixture 拿 base URL，不再有模块级常量。"""
    text = (V5_TESTS / rel).read_text(encoding="utf-8")
    assert re.search(r"^API\s*=", text, re.M) is None, f"{rel} 仍有模块级 API 常量，注入点没接上"
    assert "math_api" in text, f"{rel} 未使用 math_api fixture"


def test_math_api_base_override_yields_a_usable_base_url():
    """MATH_API_BASE 这条支路必须真能跑——它是切换后验证线上部署的唯一入口。

    第一版在生成器型 fixture 里写 `return override`，pytest 直接
    `ValueError: math_api did not yield a value`，8 条用例全 ERROR；
    由于默认路径（自起实例）照常绿，这个 bug 只有在显式设变量时才暴露。
    这里打一个必定连不上的空闲端口：断言失败原因来自连接被拒，
    而不是 fixture 没产出值。
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "v5/tests/test_ask_path_integration.py",
            "-x",
            "-q",
            "-k",
            "test_chain_starts_with_sympy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "MATH_API_BASE": f"http://127.0.0.1:{dead_port}"},
    )
    output = result.stdout + result.stderr
    assert "did not yield a value" not in output, f"fixture 的 override 分支没产出值：\n{output}"
    assert result.returncode != 0, f"打不通的地址却全绿，说明请求根本没发出去：\n{output}"
    assert "Connection refused" in output or "Refused" in output, (
        f"失败原因不是连接被拒，注入的 base URL 没被用上：\n{output[-1500:]}"
    )
