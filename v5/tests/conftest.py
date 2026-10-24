"""v5 数学服务集成测试的运行底座：自己起一个实例，不寄生线上部署。

线上 `math-sympy-http.service` 是系统单元（root 才能装/重启）。测试若直接打它，
结论就变成「线上那台进程当时用的哪套解释器」——换解释器要先跑 sudo，绿灯证明不了代码。
这里改为按仓库里那份单元自起自停：解释器、Environment、脚本路径全部从单元解析，
端口向内核要一个空闲的，测完终止。这样同一条断言在无特权 shell 里也成立。

想验线上部署本身：显式设 MATH_API_BASE=http://127.0.0.1:8002 跑同一批用例。
"""

import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "v5" / "systemd" / "math-sympy-http.service"
READY_TIMEOUT_S = 60


def _parse_unit(unit_text: str) -> tuple[list[str], dict[str, str]]:
    """从单元文件取出「启动命令 argv」与「Environment 键值」。

    ExecStart 用 `\\` 折行，得顺着续行拼回一条；argv 砍到脚本路径为止，
    端口与 host/workers 由测试自己给。
    """
    env: dict[str, str] = {}
    for line in unit_text.splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            body = line[len("Environment=") :].strip('"')
            key, _, value = body.partition("=")
            env[key] = value

    lines = unit_text.splitlines()
    start_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("ExecStart=")),
        None,
    )
    assert start_idx is not None, "单元里没有 ExecStart= 行"
    command = lines[start_idx][len("ExecStart=") :]
    nxt = start_idx + 1
    while command.rstrip().endswith("\\") and nxt < len(lines):
        command = command.rstrip()[:-1] + " " + lines[nxt].strip()
        nxt += 1

    argv = shlex.split(command)
    script_idx = next((i for i, tok in enumerate(argv) if tok.endswith(".py")), None)
    assert script_idx is not None, f"ExecStart 里没有 .py 入口：{command!r}"
    return argv[: script_idx + 1], env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def math_api(tmp_path_factory):
    """返回 base URL（不含尾斜杠）；session 起一次，进程内复用。"""
    override = os.environ.get("MATH_API_BASE")
    if override:
        # 生成器型 fixture 不能早退：pytest 要求它 yield 出值，
        # `return` 会得到 "math_api did not yield a value"。
        yield override.rstrip("/")
        return

    if not UNIT.exists():
        pytest.skip(f"缺单元文件 {UNIT.relative_to(ROOT)}，无从解析启动口径")
    argv, env = _parse_unit(UNIT.read_text(encoding="utf-8"))

    interpreter = Path(argv[0])
    if not interpreter.exists():
        pytest.skip(
            f"单元指定的解释器不存在 {interpreter}；"
            "建法 bash v5/scripts/install_math_venv.sh"
        )

    port = _free_port()
    log_path = tmp_path_factory.mktemp("math-http") / "server.log"
    child_env = {**os.environ, **env}
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [*argv, "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
            cwd=ROOT,
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + READY_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"按单元口径自起的 math-sympy-http 当场退出 rc={proc.returncode}：\n"
                    f"{log_path.read_text(errors='replace')[-2000:]}"
                )
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError) as e:
                last_error = str(e)
            time.sleep(0.5)
        else:
            proc.terminate()
            pytest.fail(f"{base}/health 在 {READY_TIMEOUT_S}s 内没就绪，最后错误 {last_error}")

    try:
        yield base
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
