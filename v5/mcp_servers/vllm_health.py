"""
vllm_health · v5-VE1-M3 vLLM judge 迁移辅助

为 `llm_ollama.py` 提供：
  - VLLM_HEALTH_URL 环境变量（如 http://127.0.0.1:8001/v1/models）
  - call_vllm_chat(messages, model): 调用 vLLM OpenAI 兼容 API
  - fallback_chain: vLLM (60s) → Ollama (300s) → 错误

设计要点：
  - 不破坏 llm_ollama.py 的现有 Tool 契约
  - 通过环境变量开关，不启用 MCP server 就视而不见
  - 调用走 HTTP OpenAI 兼容 API，与 Ollama 同样易替换
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("vllm_health")

VLLM_ENABLED = os.environ.get("VLLM_ENABLED", "true").lower() == "true"
VLLM_URL = os.environ.get("VLLM_JUDGE_URL", "http://127.0.0.1:8001")
VLLM_MODEL = os.environ.get("VLLM_JUDGE_MODEL", "qwen3-14b-vllm")
VLLM_TIMEOUT_S = float(os.environ.get("VLLM_JUDGE_TIMEOUT_S", "60"))


def is_vllm_available(timeout_s: float = 2.0) -> bool:
    """快速健康检查：检查 /v1/models 端点。"""
    if not VLLM_ENABLED:
        return False
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.get(f"{VLLM_URL}/v1/models")
            if r.status_code == 200:
                return True
    except Exception:
        pass
    return False


def vllm_health_full(timeout_s: float = 5.0) -> dict:
    """详细健康检查：返回服务器状态 + 已加载模型。"""
    if not VLLM_ENABLED:
        return {"available": False, "reason": "VLLM_ENABLED=false"}
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.get(f"{VLLM_URL}/v1/models")
            if r.status_code != 200:
                return {"available": False, "reason": f"http {r.status_code}"}
            data = r.json()
            models = [m.get("id", "") for m in data.get("data", [])]
            return {
                "available": VLLM_MODEL in models or any(VLLM_MODEL in m for m in models),
                "url": VLLM_URL,
                "model": VLLM_MODEL,
                "models_loaded": models,
                "reason": "ready" if models else "no models",
            }
    except Exception as e:
        return {"available": False, "url": VLLM_URL, "reason": f"error: {e!s}"}


def call_vllm_chat(
    messages: list,
    model: str | None = None,
    format: str | None = None,
    temperature: float = 0.3,
    timeout_s: float | None = None,
) -> dict:
    """调用 vLLM OpenAI 兼容 API；失败返回 ok=False。"""
    if not VLLM_ENABLED:
        return {"ok": False, "error": "VLLM_ENABLED=false", "backend": "vllm"}
    use_model = model or VLLM_MODEL
    use_timeout = timeout_s or VLLM_TIMEOUT_S

    payload: dict[str, Any] = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if format == "json":
        payload["response_format"] = {"type": "json_object"}

    try:
        with httpx.Client(timeout=use_timeout) as c:
            r = c.post(f"{VLLM_URL}/v1/chat/completions", json=payload)
        if r.status_code != 200:
            return {
                "ok": False,
                "error": f"vllm http {r.status_code}: {r.text[:200]}",
                "backend": "vllm",
            }
        data = r.json()
        if not data.get("choices"):
            return {"ok": False, "error": "no choices", "backend": "vllm"}
        content = data["choices"][0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return {
            "ok": True,
            "content": content,
            "model": use_model,
            "backend": "vllm",
            "elapsed_s": 0,
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "backend": "vllm"}


def call_vllm_embed(texts: list, model: str | None = None, timeout_s: float = 30.0) -> dict:
    """调用 vLLM OpenAI 兼容 embed API；失败返回 ok=False。"""
    if not VLLM_ENABLED:
        return {"ok": False, "error": "VLLM_ENABLED=false"}
    use_model = model or VLLM_MODEL
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(
                f"{VLLM_URL}/v1/embeddings",
                json={"model": use_model, "input": texts},
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"vllm http {r.status_code}"}
        data = r.json()
        embeddings = [d["embedding"] for d in data.get("data", [])]
        return {"ok": True, "embeddings": embeddings, "model": use_model, "backend": "vllm"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# 自检
# ============================================================================

if __name__ == "__main__":
    print("=== vllm_health 自检 ===\n")
    print(f"VLLM_ENABLED = {VLLM_ENABLED}")
    print(f"VLLM_URL = {VLLM_URL}")
    print(f"VLLM_MODEL = {VLLM_MODEL}")
    print()

    print("[1] 快速健康检查")
    ok = is_vllm_available()
    print(f"  vllm 可用: {ok}")

    print("\n[2] 详细健康检查")
    h = vllm_health_full()
    for k, v in h.items():
        print(f"  {k}: {v}")

    if ok:
        print("\n[3] 测试 vLLM chat")
        r = call_vllm_chat(
            messages=[
                {"role": "system", "content": "你是一个简洁助手。"},
                {"role": "user", "content": "用一句话介绍 Tensor Parallel。"},
            ],
            format=None,
            timeout_s=30,
        )
        print(f"  result: {json.dumps(r, ensure_ascii=False, indent=2)[:500]}")

        print("\n[4] 测试 JSON 模式")
        r = call_vllm_chat(
            messages=[
                {"role": "system", "content": "输出严格 JSON 格式。"},
                {"role": "user", "content": '返回 {"answer": "hello"}'},
            ],
            format="json",
            timeout_s=30,
        )
        print(f"  result: {json.dumps(r, ensure_ascii=False, indent=2)[:500]}")
    else:
        print("\n[3][4] vLLM 不可用，跳过调用测试")
        print("  启动方法：")
        print(
            "    /home/jiuben/tdx-data-feed/venv/bin/python -m vllm.entrypoints.openai.api_server \\"
        )
        print("      --model /home/jiuben/models/Qwen3-14B \\")
        print("      --tensor-parallel-size 2 --dtype bfloat16 --port 8001")

    print("\n=== 自检完毕 ===")
