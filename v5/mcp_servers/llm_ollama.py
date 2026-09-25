"""
llm_ollama.py · v5-VE1-M2/M3 主 LLM MCP server

5 个 Tool：chat / embed / reason / classify / judge
后端：Ollama (默认) + vLLM TP=2 BF16 (M3 启用)

fallback_chain: vLLM (60s) → Ollama (300s) → 错误
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# 把同目录的 vllm_health 加进来
sys.path.insert(0, str(Path(__file__).parent))
try:
    from vllm_health import (
        VLLM_ENABLED as _VLLM_ENABLED_GLOBAL,
        call_vllm_chat,
        call_vllm_embed,
        is_vllm_available,
        vllm_health_full,
    )

    HAS_VLLM = True
except ImportError:
    HAS_VLLM = False
    _VLLM_ENABLED_GLOBAL = False

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ============================================================================
# 配置
# ============================================================================

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_CHAT_MODEL = os.environ.get("DEFAULT_CHAT_MODEL", "qwen3:14b")
DEFAULT_REASON_MODEL = os.environ.get("DEFAULT_REASON_MODEL", "deepseek-r1:14b")
DEFAULT_EMBED_MODEL = os.environ.get("DEFAULT_EMBED_MODEL", "nomic-embed-text:latest")
DEFAULT_JUDGE_MODEL = os.environ.get("DEFAULT_JUDGE_MODEL", "qwen3:14b")

CHAT_TIMEOUT_S = float(os.environ.get("LLM_CHAT_TIMEOUT_S", "300"))
REASON_TIMEOUT_S = float(os.environ.get("LLM_REASON_TIMEOUT_S", "8"))
EMBED_TIMEOUT_S = float(os.environ.get("LLM_EMBED_TIMEOUT_S", "30"))

VLLM_ENABLED = os.environ.get("VLLM_ENABLED", "true").lower() == "true" and HAS_VLLM
VLLM_URL = os.environ.get("VLLM_JUDGE_URL", "http://127.0.0.1:8001")
VLLM_TIMEOUT_S = float(os.environ.get("VLLM_JUDGE_TIMEOUT_S", "60"))

AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG = AUDIT_DIR / "llm-calls.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-llm-ollama")

server = Server("llm-ollama")


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _audit(entry: dict):
    """审计：每次调用落一行 JSONL。"""
    entry["ts"] = _utc_now()
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"audit log failed: {e}")


# ============================================================================
# 核心调用：Ollama
# ============================================================================


async def _ollama_chat(
    model: str,
    messages: list,
    format: str | None = None,
    temperature: float = 0.7,
    timeout_s: float = CHAT_TIMEOUT_S,
) -> dict:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": temperature},
    }
    if format == "json":
        payload["format"] = "json"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
        if r.status_code != 200:
            return {"ok": False, "error": f"ollama http {r.status_code}: {r.text[:200]}"}
        data = r.json()
        content = data.get("message", {}).get("content", "")
        return {
            "ok": True,
            "content": content,
            "model": model,
            "backend": "ollama",
            "elapsed_s": round(time.time() - t0, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "elapsed_s": round(time.time() - t0, 2)}


async def _ollama_embed(
    texts: list, model: str = DEFAULT_EMBED_MODEL, timeout_s: float = EMBED_TIMEOUT_S
) -> dict:
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": model, "input": texts, "keep_alive": "30m"},
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"ollama http {r.status_code}"}
        data = r.json()
        return {
            "ok": True,
            "embeddings": data.get("embeddings", []),
            "model": model,
            "backend": "ollama",
            "elapsed_s": round(time.time() - t0, 3),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# Tool 实现（Ollama + vLLM fallback）
# ============================================================================


async def chat_impl(
    model: str = DEFAULT_CHAT_MODEL,
    messages: list | None = None,
    format: str | None = None,
    temperature: float = 0.7,
) -> dict:
    if messages is None:
        return {"ok": False, "error": "messages 不能为空"}
    _audit({"tool": "chat", "model": model, "format": format, "msgs": len(messages)})

    # 1) vLLM 路径（如果可用）
    if VLLM_ENABLED and is_vllm_available(timeout_s=2):
        r = await asyncio.to_thread(
            call_vllm_chat,
            messages=messages,
            model=None,
            format=format,
            temperature=temperature,
            timeout_s=VLLM_TIMEOUT_S,
        )
        r["source"] = "vllm"
        if r.get("ok"):
            return r

    # 2) Ollama fallback
    r = await _ollama_chat(model, messages, format, temperature)
    r["source"] = "ollama"
    return r


async def embed_impl(texts: list | None = None, model: str = DEFAULT_EMBED_MODEL) -> dict:
    if not texts:
        return {"ok": False, "error": "texts 不能为空"}
    _audit({"tool": "embed", "model": model, "n": len(texts)})

    if VLLM_ENABLED and is_vllm_available(timeout_s=2):
        r = await asyncio.to_thread(call_vllm_embed, texts=texts, model=None)
        r["source"] = "vllm"
        if r.get("ok"):
            return r

    return await _ollama_embed(texts, model)


async def reason_impl(
    prompt: str, model: str = DEFAULT_REASON_MODEL, fallback_model: str = DEFAULT_CHAT_MODEL
) -> dict:
    _audit({"tool": "reason", "model": model})
    messages = [{"role": "user", "content": prompt}]
    # 推理严格短超时：避免 GPU swap 卡死
    r = await _ollama_chat(
        model, messages, format=None, temperature=0.3, timeout_s=REASON_TIMEOUT_S
    )
    if r.get("ok"):
        return r
    # fallback 到 chat model
    log.info(f"reason fallback: {model} → {fallback_model}")
    r2 = await _ollama_chat(
        fallback_model, messages, format=None, temperature=0.3, timeout_s=CHAT_TIMEOUT_S
    )
    r2["fallback"] = True
    r2["fallback_from"] = model
    return r2


async def classify_impl(
    text: str, labels: list | None = None, model: str = DEFAULT_CHAT_MODEL
) -> dict:
    if not labels:
        labels = ["数学", "物理", "化学", "生物", "计算机", "其他"]
    sys_prompt = (
        f"你是分类助手。请把给定文本分到以下类别之一：{labels}。\n"
        f'严格按 JSON 格式输出：{{"labels": ["<closest>"], "confidence": [<float 0~1>]}}\n'
        f"只能输出 JSON，不要解释。"
    )
    _audit({"tool": "classify", "model": model, "labels": labels})
    return await chat_impl(
        model=model,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text[:4000]},
        ],
        format="json",
        temperature=0.1,
    )


async def judge_impl(
    a: str, b: str, criteria: str = "语义等价", model: str = DEFAULT_JUDGE_MODEL
) -> dict:
    sys_prompt = (
        "你是等价/相似判定助手。根据给定 criteria 判定两个对象的符合程度。\n"
        '严格按 JSON 输出：{"verdict": "YES|NO|PARTIAL", "score": <float 0~1>, "rationale": "<简短理由>"}\n'
        "只能输出 JSON。"
    )
    user_prompt = (
        f"对象 A：{a[:2000]}\n\n对象 B：{b[:2000]}\n\n判据：{criteria}\n\n请判定并给出 JSON。"
    )
    _audit(
        {"tool": "judge", "model": model, "criteria": criteria, "a_len": len(a), "b_len": len(b)}
    )
    return await chat_impl(
        model=model,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        format="json",
        temperature=0.1,
    )


async def vllm_status_impl() -> dict:
    """诊断 Tool：返回 vLLM + Ollama 状态。"""
    return {
        "vllm_enabled": VLLM_ENABLED,
        "vllm": vllm_health_full()
        if HAS_VLLM
        else {"available": False, "reason": "vllm_health not imported"},
        "ollama_url": OLLAMA_URL,
        "models": {
            "chat": DEFAULT_CHAT_MODEL,
            "reason": DEFAULT_REASON_MODEL,
            "embed": DEFAULT_EMBED_MODEL,
            "judge": DEFAULT_JUDGE_MODEL,
        },
    }


# ============================================================================
# MCP Tool 注册
# ============================================================================


@server.list_tools()
async def list_tools() -> list:
    return [
        Tool(
            name="chat",
            description=f"通用 chat；模型={DEFAULT_CHAT_MODEL}；fallback: vLLM→Ollama",
            inputSchema={
                "type": "object",
                "properties": {
                    "messages": {"type": "array", "items": {"type": "object"}},
                    "model": {"type": "string"},
                    "format": {"type": "string", "enum": ["json", "text"]},
                    "temperature": {"type": "number", "default": 0.7},
                },
                "required": ["messages"],
            },
        ),
        Tool(
            name="embed",
            description=f"向量化；模型={DEFAULT_EMBED_MODEL}；768 维",
            inputSchema={
                "type": "object",
                "properties": {
                    "texts": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
                "required": ["texts"],
            },
        ),
        Tool(
            name="reason",
            description=f"推理（8s 超时降级到 chat）；模型={DEFAULT_REASON_MODEL}",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="classify",
            description="分类；返回 JSON {labels, confidence}",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="judge",
            description="等价/相似判定；返回 JSON {verdict, score, rationale}",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "criteria": {"type": "string", "default": "语义等价"},
                    "model": {"type": "string"},
                },
                "required": ["a", "b"],
            },
        ),
        Tool(
            name="vllm_status",
            description="【M3 诊断】vLLM + Ollama 双栈健康检查",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    log.info(f"call_tool: {name} args={list(arguments.keys())}")
    try:
        if name == "chat":
            res = await chat_impl(
                model=arguments.get("model", DEFAULT_CHAT_MODEL),
                messages=arguments.get("messages", []),
                format=arguments.get("format"),
                temperature=arguments.get("temperature", 0.7),
            )
        elif name == "embed":
            res = await embed_impl(
                texts=arguments.get("texts", []),
                model=arguments.get("model", DEFAULT_EMBED_MODEL),
            )
        elif name == "reason":
            res = await reason_impl(
                prompt=arguments.get("prompt", ""),
                model=arguments.get("model", DEFAULT_REASON_MODEL),
            )
        elif name == "classify":
            res = await classify_impl(
                text=arguments.get("text", ""),
                labels=arguments.get("labels"),
                model=arguments.get("model", DEFAULT_CHAT_MODEL),
            )
        elif name == "judge":
            res = await judge_impl(
                a=arguments.get("a", ""),
                b=arguments.get("b", ""),
                criteria=arguments.get("criteria", "语义等价"),
                model=arguments.get("model", DEFAULT_JUDGE_MODEL),
            )
        elif name == "vllm_status":
            res = await vllm_status_impl()
        else:
            res = {"ok": False, "error": f"unknown tool: {name}"}
    except Exception as e:
        log.exception(f"tool {name} failed")
        res = {"ok": False, "error": str(e)}

    return [TextContent(type="text", text=json.dumps(res, ensure_ascii=False, indent=2))]


async def main():
    log.info(f"llm-ollama MCP server starting | VLLM_ENABLED={VLLM_ENABLED}")
    log.info(f"  OLLAMA: {OLLAMA_URL}")
    log.info(f"  vLLM:   {VLLM_URL if HAS_VLLM else '(unavailable)'}")
    log.info(f"  fallback chain: vLLM ({VLLM_TIMEOUT_S}s) → Ollama ({CHAT_TIMEOUT_S}s)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
