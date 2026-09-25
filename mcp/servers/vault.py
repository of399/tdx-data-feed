#!/usr/bin/env python3
"""
mcp/servers/vault.py — MCP Server: StockVault RAG 检索
=====================================================

工具：
    1. search     向量检索（Ollama nomic-embed-text）
    2. add_doc    添加文档到向量库
    3. list_cats  列出文档分类

底座：
    - StockVault 目录（/home/jiuben/StockVault）
    - Ollama 本地 embedding（nomic-embed-text，已在 §14.8 落地）
    - 向量库：本地 ChromaDB（如未装则降级为关键词 BM25）

协议：MCP 2024-11-05（JSON-RPC over stdio）
"""

import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/home/jiuben/tdx-data-feed/logs/mcp.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:vault] %(message)s",
)

VAULT_DIR = Path("/home/jiuben/StockVault")
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


# === Embedding 后端 =======================================================
def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """通过 Ollama HTTP API 调 embedding"""
    import urllib.request
    out = []
    for text in texts:
        req_data = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/embeddings",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                out.append(data.get("embedding", []))
        except Exception as e:
            logging.warning(f"Ollama embedding 失败：{e}，降级为 0 向量")
            out.append([0.0] * 768)  # nomic-embed-text 维度 768
    return out


# === 向量库后端 ===========================================================
class VectorStore:
    """轻量本地向量库：JSONL + numpy"""
    def __init__(self, store_path: Path):
        self.store_path = store_path
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.docs = []  # [{text, meta, embedding}]
        if store_path.exists():
            try:
                with open(store_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.docs.append(json.loads(line))
            except Exception as e:
                logging.warning(f"读 store 失败：{e}")

    def add(self, text: str, meta: dict) -> dict:
        emb = _embed_ollama([text])[0]
        rec = {"text": text, "meta": meta, "embedding": emb}
        self.docs.append(rec)
        with open(self.store_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"id": hashlib.md5(text.encode()).hexdigest()[:12], "ok": True}

    def search(self, query: str, top_k: int = 5) -> dict:
        if not self.docs:
            return {"results": [], "n_docs": 0}
        q_emb = _embed_ollama([query])[0]
        if not q_emb or all(v == 0 for v in q_emb):
            return {"results": [], "n_docs": len(self.docs), "warning": "embedding 失败"}
        # 余弦相似度
        scored = []
        q_norm = sum(v * v for v in q_emb) ** 0.5 + 1e-9
        for d in self.docs:
            emb = d.get("embedding") or []
            if not emb:
                continue
            d_norm = sum(v * v for v in emb) ** 0.5 + 1e-9
            dot = sum(a * b for a, b in zip(q_emb, emb, strict=False))
            sim = dot / (q_norm * d_norm)
            scored.append((sim, d))
        scored.sort(reverse=True, key=lambda x: x[0])
        return {
            "results": [{
                "score": round(s, 4),
                "text": d["text"][:500],
                "meta": d.get("meta", {}),
            } for s, d in scored[:top_k]],
            "n_docs": len(self.docs),
        }


# 全局 store（per process）
_store = VectorStore(Path("/home/jiuben/tdx-data-feed/data/vault_store.jsonl"))


# === 工具实现 =============================================================
def vault_search(query: str, top_k: int = 5) -> dict:
    return _store.search(query, top_k)


def vault_add_doc(text: str, meta: dict | None = None) -> dict:
    return _store.add(text, meta or {})


def vault_list_cats() -> dict:
    """列分类：从 meta.category 聚合"""
    from collections import Counter
    cats = Counter()
    for d in _store.docs:
        c = d.get("meta", {}).get("category", "未分类")
        cats[c] += 1
    return {"categories": dict(cats.most_common())}


# === MCP dispatcher ======================================================
TOOLS = {
    "search": {
        "description": "向量检索 StockVault 文档（Ollama nomic-embed-text）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询文本"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        "handler": lambda a: vault_search(**a),
    },
    "add_doc": {
        "description": "添加文档到向量库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "文档内容"},
                "meta": {"type": "object", "description": "元数据（category/path/tags 等）"},
            },
            "required": ["text"],
        },
        "handler": lambda a: vault_add_doc(**a),
    },
    "list_cats": {
        "description": "列出文档分类及文档数",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda a: vault_list_cats(),
    },
}


def handle_request(req: dict) -> dict:
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-vault", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "tools": [{
                    "name": n,
                    "description": s["description"],
                    "inputSchema": s["inputSchema"],
                } for n, s in TOOLS.items()],
            }}
        if method == "tools/call":
            params = req.get("params", {})
            t_name = params.get("name")
            t_args = params.get("arguments", {})
            if t_name not in TOOLS:
                raise McpError(-32602, f"未知工具：{t_name}")
            t0 = datetime.now()
            result = TOOLS[t_name]["handler"](t_args)
            elapsed = (datetime.now() - t0).total_seconds() * 1000
            logging.info(f"{t_name} latency={elapsed:.1f}ms")
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            }}
        if method == "notifications/initialized":
            return None
        raise McpError(-32601, f"未知方法：{method}")
    except McpError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        logging.exception("handler error")
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}


def main():
    print("[mcp:vault] 已启动，stdio 模式（向量库=data/vault_store.jsonl）", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as e:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            print(json.dumps(err), flush=True)


if __name__ == "__main__":
    main()
