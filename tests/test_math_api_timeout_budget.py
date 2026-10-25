"""v5 数学 HTTP 用例的读超时预算守卫。

背景：切到 venv-math 后 sympy/ask 段是真的在执行了 —— 热态单请求 5~9.5s，
冷启动首轮（Ollama 要把模型装起来）能顶穿原先写死的 30s，把环境抖动伪装成
功能失败。这里守两件事：

1. 预算要够（太紧 = 假红）；
2. 预算仍可被证伪（太松/无限 = 真卡死也测不出来），且冷启动该由 fixture
   的 warm-up 吸收，而不是让每个用例各自赌运气。
"""

from __future__ import annotations

import importlib.util
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE_MODULE = ROOT / "v5" / "tests" / "test_ask_path_integration.py"
CONFTEST = ROOT / "v5" / "tests" / "conftest.py"


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("ask_path_probe", PROBE_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def silent_base():
    """只 accept、永不回应的 HTTP 端点 —— 任何请求都必须撞超时"""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    held: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # 收下连接但一个字都不回

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        for conn in held:
            conn.close()
        server.close()
        thread.join(timeout=2)


def test_budget_is_env_tunable(silent_base):
    """MATH_API_TIMEOUT_S 必须真的被采纳 —— 否则预算只能靠改代码调"""
    module = _load_probe_module()
    os.environ["MATH_API_TIMEOUT_S"] = "2"
    try:
        started = time.monotonic()
        code, body = module._post_tool(
            silent_base, "math_strategy_route_and_execute", {"latex": "x + 1", "action": "simplify", "var": "x"}
        )
        elapsed = time.monotonic() - started
    finally:
        os.environ.pop("MATH_API_TIMEOUT_S", None)

    assert code == 0, f"静默端点必须被判失败，实际 code={code} body={body}"
    assert elapsed < 10, f"env 预算没生效：elapsed={elapsed:.1f}s，设的是 2s"


def test_default_budget_clears_a_cold_llm_fallback(silent_base):
    """默认预算要容得下 llm 兜底层的冷启动（实测热态 9.5s，冷态可顶穿 30s）

    上限也守着：预算无限等于没超时，服务器真卡死时测试只会挂住不会报红。
    """
    assert "MATH_API_TIMEOUT_S" not in os.environ, "上一条用例泄漏了环境变量"
    module = _load_probe_module()
    budget = float(module.DEFAULT_TIMEOUT_S)
    assert 60 <= budget <= 180, (
        f"默认预算 {budget}s 不合理：< 60 会把冷启动 Ollama 装模型的耗时伪装成功能失败；"
        "> 180 的话 make test 最坏耗时不可接受"
    )


def test_conftest_warms_the_chain_before_handing_out_the_base_url():
    """冷启动开销要落在 fixture 的 warm-up 里，而不是落在第一个用例上

    只查形状：math_api 在 yield 之前必须调用 warm_up —— 顺序反过来的话
    第一个用例照样是那个付冷启动代价的人，守卫要的就是这个顺序。
    """
    text = CONFTEST.read_text(encoding="utf-8")
    assert "def warm_up" in text, "conftest 没有 warm_up：冷启动耗时没人吸收，只会以超时假红出现"
    match = re.search(r"def math_api\((?P<body>.*?)\n(?:@pytest|def |\Z)", text, re.S)
    assert match, "找不到 math_api fixture 的函数体"
    body = match.group("body")
    warm_at = body.find("warm_up(")
    yield_at = body.find("yield")
    assert warm_at != -1, "math_api 里没有调用 warm_up"
    assert yield_at != -1 and warm_at < yield_at, (
        f"warm_up 在 yield 之后才跑（warm_at={warm_at} yield_at={yield_at}）："
        "用例已经拿不到吸收冷启动的机会了"
    )
