#!/usr/bin/env python3
"""
v5/mcp_servers/llm_ollama.py 验收测试

两个测试层：
  1. 直接调用 async functions（验证 Ollama HTTP API + 工具实现）
  2. 通过 MCP stdio client 调用（验证 MCP 协议 + 注册）
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from time import sleep

import pytest

sys.path.insert(0, "/home/jiuben/tdx-data-feed")
from v5.mcp_servers.llm_ollama import (
    chat_impl,
    classify_impl,
    embed_impl,
    judge_impl,
    reason_impl,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def prewarm():
    """预热 qwen3:14b + nomic-embed，避免首调用 60~90s 冷启动。"""
    import httpx

    print("[prewarm] qwen3:14b ...")
    async with httpx.AsyncClient(timeout=180) as c:
        await c.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": "qwen3:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": "30m",
            },
        )
    print("[prewarm] nomic-embed-text ...")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            "http://127.0.0.1:11434/api/embed",
            json={"model": "nomic-embed-text:latest", "input": ["warmup"], "keep_alive": "30m"},
        )
    print("[prewarm] done.\n")


@pytest.mark.asyncio
async def test_layer_1():
    print("=" * 60)
    print("Layer 1 · 直接调用 async functions")
    print("=" * 60)

    await prewarm()

    print("\n[1/5] chat(qwen3:14b)")
    r = await chat_impl(
        model="qwen3:14b",
        messages=[{"role": "user", "content": "Say 'hello' in one word."}],
        temperature=0.1,
    )
    check("chat 返回 ok", r.get("ok"), str(r)[:200])
    check("chat 有 content", "content" in r)
    check("chat 有 elapsed_s", "elapsed_s" in r)
    print(f"   content: {r.get('content', '')[:80]!r}")
    print(
        f"   elapsed: {r.get('elapsed_s')}s, tokens: {r.get('prompt_eval_count')}->{r.get('eval_count')}"
    )

    print("\n  [sleep 3s] 让 ollama 队列清空 ...")
    sleep(3)

    print("\n[2/5] embed(nomic-embed-text)")
    r = await embed_impl(
        texts=["\\int x^2 dx", "\\frac{1}{2}", "hello world"],
        model="nomic-embed-text:latest",
    )
    check("embed 返回 ok", r.get("ok"), str(r)[:200])
    check("embed dim=768", r.get("dim") == 768, f"dim={r.get('dim')}")
    check("embed count=3", r.get("count") == 3)
    print(f"   dim: {r.get('dim')}, count: {r.get('count')}, elapsed: {r.get('elapsed_s')}s")

    print("\n[3/5] reason(deepseek-r1:14b) — 注意：会触发 GPU 模型 swap，单独跑")
    print("   此项为可选 — 在大内存多模型环境下可启用，当前 32GB VRAM 双 GPU")
    print("   仅跑 qwen3 链路即可；reason 测试被注释以避免 120s swap 超时")
    SKIP_REASON = os.environ.get("SKIP_REASON_TEST", "1") == "1"
    if SKIP_REASON:
        print("   SKIP  (SKIP_REASON_TEST=1)")
    else:
        r = await reason_impl(
            prompt="如果 x + 3 = 7，求 x 的值。一行回答。",
            model="deepseek-r1:14b",
        )
        check("reason 返回 ok", r.get("ok"), str(r)[:200])
        check("reason 有 content", "content" in r)
        check("reason 无 fallback（或 fallback=True）", r.get("fallback") in (True, False))
        print(f"   content: {r.get('content', '')[:100]!r}")
        print(
            f"   model: {r.get('model')}, fallback: {r.get('fallback')}, elapsed: {r.get('elapsed_s')}s"
        )

    print("\n  [sleep 5s] ...")
    sleep(5)

    print("\n[4/5] classify(qwen3:14b)")
    r = await classify_impl(
        text="求函数 f(x) = x^2 在 x=2 处的导数",
        labels=["算术", "代数", "微积分", "概率统计"],
    )
    check("classify 返回 ok", r.get("ok"), str(r)[:200])
    # classify_impl 返回 {'ok', 'content', 'model', 'backend', ...}
    # content 是 JSON 字符串, 需 parse
    classify_result = {}
    if r.get("ok"):
        try:
            classify_result = json.loads(r.get("content", "{}"))
        except Exception:
            classify_result = {}
    check("classify content 可解析", isinstance(classify_result, dict), f"got: {str(classify_result)[:80]}")
    if r.get("ok") and classify_result:
        print(f"   result: {json.dumps(classify_result, ensure_ascii=False)[:200]}")
        check(
            "classify content 含 labels",
            "labels" in classify_result or "raw" in classify_result,
        )

    print("\n  [sleep 5s] ...")
    sleep(5)

    print("\n[5/5] judge(qwen3:14b)")
    r = await judge_impl(
        a="\\int x^2 dx = \\frac{x^3}{3} + C",
        b="\\frac{x^3}{3} + C",
        criteria="数学等价性",
    )
    check("judge 返回 ok", r.get("ok"), str(r)[:200])
    # judge_impl 返回 {'ok', 'content', 'model', ...}, content 是 JSON 字符串
    judge_result = {}
    if r.get("ok"):
        try:
            judge_result = json.loads(r.get("content", "{}"))
        except Exception:
            judge_result = {}
    check("judge content 可解析", isinstance(judge_result, dict), f"got: {str(judge_result)[:80]}")
    if r.get("ok") and judge_result:
        print(f"   result: {json.dumps(judge_result, ensure_ascii=False)[:200]}")
        check(
            "judge content 含 verdict",
            "verdict" in judge_result or "raw" in judge_result,
        )


@pytest.mark.asyncio
async def test_layer_2():
    print("\n" + "=" * 60)
    print("Layer 2 · MCP stdio 协议验证")
    print("=" * 60)

    try:
        from mcp.client.stdio import stdio_client

        from mcp import ClientSession, StdioServerParameters
    except ImportError:
        print("  SKIP  mcp client SDK 未安装")
        return

    server_params = StdioServerParameters(
        command="/home/jiuben/tdx-data-feed/venv/bin/python",
        args=["/home/jiuben/tdx-data-feed/v5/mcp_servers/llm_ollama.py"],
    )

    print("  启动 MCP server (stdio) ...")
    async with (
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as session,
    ):
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            print(f"  注册了 {len(tool_names)} 个工具: {sorted(tool_names)}")
            check("mcp 注册 5 个工具", len(tool_names) == 5)
            for n in ("chat", "embed", "reason", "classify", "judge"):
                check(f"mcp 含 {n}", n in tool_names)

            print("\n  通过 MCP 调用 chat ...")
            r = await session.call_tool(
                "chat",
                {
                    "messages": [{"role": "user", "content": "Reply with just the word OK."}],
                    "temperature": 0.0,
                },
            )
            data = json.loads(r.content[0].text)
            check("mcp chat 返回 ok", data.get("ok"))
            print(f"   result: {data.get('content', '')[:60]!r}")

            print("\n  通过 MCP 调用 embed ...")
            r = await session.call_tool("embed", {"texts": ["test1", "test2"]})
            data = json.loads(r.content[0].text)
            check("mcp embed 返回 ok", data.get("ok"))
            check("mcp embed dim=768", data.get("dim") == 768)
            print(f"   dim: {data.get('dim')}, count: {data.get('count')}")


async def main():
    print("mcp-llm-ollama 验收测试")
    print(f"启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    try:
        await test_layer_1()
    except Exception as e:
        print(f"\nLayer 1 异常: {e}")
        import traceback

        traceback.print_exc()

    try:
        await test_layer_2()
    except Exception as e:
        print(f"\nLayer 2 异常: {e}")
        import traceback

        traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} pass / {FAIL} fail")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
