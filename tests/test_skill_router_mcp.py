"""P1-1：skill_router 的 MCP 客户端测试基座。

不拉起真实 MCP server 子进程，只覆盖客户端自身的健壮路径：
- 脚本不存在 → start() 抛 FileNotFoundError（不静默）
- 子进程返回非 JSON → call_tool 回退 {"raw": text} 而非抛栈（SEM002 兜底）
"""
from pathlib import Path

from skill_router.runtime import McpClient


def test_mcpclient_missing_script_raises():
    c = McpClient("ghost", Path("/no/such/script.py"))
    try:
        c.start()
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass


def test_call_tool_nonjson_falls_back():
    # 用假 proc 占位，绕过 _request 的真实子进程读写
    c = McpClient("fake", Path("/dev/null"))
    c.proc = object()
    c._request = lambda *a, **k: {  # type: ignore[method-assign]
        "result": {"content": [{"text": "not json"}]}
    }
    assert c.call_tool("x", {}) == {"raw": "not json"}
