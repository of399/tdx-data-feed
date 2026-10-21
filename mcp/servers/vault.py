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
import math
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
except ImportError:  # numpy 缺失时向量检索不可用，自动退回词法
    np = None

LOG_FILE = Path("/home/jiuben/tdx-data-feed/logs/mcp.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:vault] %(message)s",
)

VAULT_DIR = Path("/home/jiuben/StockVault")
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
# MCP server 是每次调用新起的短命进程，让 Ollama 侧常驻模型，
# 否则每条查询都要付一次 ~7s 的冷加载，会撞上 theory-reference 的超时。
EMBED_KEEP_ALIVE = "15m"


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


# === Embedding 后端 =======================================================
def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """通过 Ollama /api/embed 批量取向量（与 build_vault_index.py 同一端点，保证可比）"""
    import urllib.request
    req_data = json.dumps({
        "model": EMBED_MODEL, "input": texts, "keep_alive": EMBED_KEEP_ALIVE,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        # 冷启动要先加载模型，超时必须给足
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        embeddings = data.get("embeddings") or []
        if len(embeddings) == len(texts):
            return embeddings
        logging.warning(f"embedding 数量不符（{len(embeddings)} != {len(texts)}），退回词法")
    except Exception as e:
        logging.warning(f"Ollama embedding 失败：{e}，本次查询退回关键词检索")
    return [[] for _ in texts]


def _query_terms(query: str) -> set[str]:
    """中英混合的轻量分词：英文/数字词 + 中文二元组。"""
    q = query.lower()
    terms = {w for w in re.findall(r"[a-z0-9_]{2,}", q)}
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            terms.add(seg)
        else:
            terms.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return terms


# === 向量库后端 ===========================================================
def _vectors_paths(store_path: Path) -> tuple[Path, Path]:
    """索引文件 → (向量 npy, 清单 json)。必须与 scripts/build_vault_index.py 同公式。"""
    return (store_path.with_name(store_path.stem + ".vectors.npy"),
            store_path.with_name(store_path.stem + ".vectors.json"))


def _rows_hash(docs: list[dict]) -> str:
    """行序指纹，用于校验向量矩阵与 JSONL 是否仍然对齐。"""
    keys = (f"{(d.get('meta') or {}).get('vault')}#"
            f"{(d.get('meta') or {}).get('path')}#"
            f"{(d.get('meta') or {}).get('chunk')}" for d in docs)
    return hashlib.md5("\n".join(keys).encode()).hexdigest()


class VectorStore:
    """轻量本地向量库：JSONL 存文本 + 旁挂 .npy 存向量（numpy 余弦检索）"""
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
        self._mat = None          # float32 (n_docs, dim)，行序与 self.docs 一致
        self._norms = None
        self.embed_model = None
        self.embed_dim = None
        self.semantic_hint = None
        self._load_vectors()

    def _load_vectors(self) -> None:
        """载入旁挂向量。任何不一致都退回词法检索——绝不拿错位的向量算相似度。"""
        if np is None:
            self.semantic_hint = "numpy 不可用，向量检索已禁用"
            return
        npy, manifest = _vectors_paths(self.store_path)
        if not (npy.exists() and manifest.exists()):
            self.semantic_hint = "无向量文件，运行 scripts/build_vault_index.py --embed 可开启语义检索"
            return
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            mat = np.load(npy)
        except Exception as e:
            self.semantic_hint = f"向量文件读取失败（{e}），已退回词法检索"
            logging.warning(self.semantic_hint)
            return
        if mat.ndim != 2 or mat.shape[0] != len(self.docs):
            self.semantic_hint = (f"向量 {mat.shape} 与索引 {len(self.docs)} 行不一致，"
                                  "请重跑 scripts/build_vault_index.py --embed")
            logging.warning(self.semantic_hint)
            return
        if _rows_hash(self.docs) != info.get("rows_hash"):
            self.semantic_hint = "笔记已变更但向量未重建（行序指纹不匹配），请重跑 --embed"
            logging.warning(self.semantic_hint)
            return
        self._mat = mat.astype(np.float32, copy=False)
        self._norms = np.linalg.norm(self._mat, axis=1) + 1e-9
        self.embed_model = info.get("model")
        self.embed_dim = int(info.get("dim") or mat.shape[1])

    @property
    def semantic_ready(self) -> bool:
        """全量向量才走语义检索；混合/缺失一律退回关键词，避免半库失效。"""
        return self._mat is not None and self._mat.shape[0] == len(self.docs)

    def add(self, text: str, meta: dict) -> dict:
        rec = {"text": text, "meta": meta, "embedding": []}
        self.docs.append(rec)
        if self._mat is not None:
            self._mat = self._norms = None
            self.semantic_hint = ("add_doc 后向量矩阵已失效，"
                                  "重跑 scripts/build_vault_index.py --embed 可恢复语义检索")
        with open(self.store_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"id": hashlib.md5(text.encode()).hexdigest()[:12], "ok": True,
                "semantic_ready": self.semantic_ready}

    def _match_indices(self, category: str | None = None,
                       path_prefix: str | None = None,
                       exclude_category=None) -> list[int]:
        banned = None
        if exclude_category:
            banned = ({exclude_category} if isinstance(exclude_category, str)
                      else set(exclude_category))
        idx = []
        for i, d in enumerate(self.docs):
            meta = d.get("meta") or {}
            if category and meta.get("category") != category:
                continue
            if path_prefix and not str(meta.get("path", "")).startswith(path_prefix):
                continue
            if banned is not None and meta.get("category") in banned:
                continue
            idx.append(i)
        return idx

    def filtered(self, category: str | None = None,
                 path_prefix: str | None = None,
                 exclude_category=None) -> list[dict]:
        """按 meta.category（vault 一级目录）/ meta.path 前缀筛文档。"""
        return [self.docs[i]
                for i in self._match_indices(category, path_prefix, exclude_category)]

    def _lexical_search(self, query: str, top_k: int,
                        docs: list[dict] | None = None) -> list[tuple[float, dict]]:
        terms = _query_terms(query)
        if not terms:
            return []
        scored = []
        for d in (self.docs if docs is None else docs):
            text = d.get("text", "").lower()[:4000]
            meta = d.get("meta", {}) or {}
            title = str(meta.get("title", "")).lower()
            path = str(meta.get("path", "")).lower()
            hits = sum(text.count(t) for t in terms)
            title_hits = sum(title.count(t) for t in terms)
            path_hits = sum(path.count(t) for t in terms)
            if hits == 0 and title_hits == 0 and path_hits == 0:
                continue
            # 标题/路径命中加权，长度归一避免长文占优
            score = (hits + 2.0 * title_hits + 1.0 * path_hits) / math.sqrt(len(text) + 1)
            scored.append((score, d))
        scored.sort(reverse=True, key=lambda x: x[0])
        return scored[:top_k]

    def _vector_search(self, query: str, top_k: int,
                       idx: list[int]) -> list[tuple[float, dict]] | None:
        """numpy 余弦相似度；不可用时返回 None 让调用方退回词法。"""
        if not self.semantic_ready or not idx:
            return None
        q_emb = _embed_ollama([query])[0]
        if not q_emb:
            return None
        q = np.asarray(q_emb, dtype=np.float32)
        if q.shape[0] != self._mat.shape[1]:
            logging.warning(f"查询向量维度 {q.shape[0]} != 索引维度 {self._mat.shape[1]}")
            return None
        rows = np.asarray(idx)
        q_norm = float(np.linalg.norm(q)) + 1e-9
        sims = self._mat[rows] @ q / (self._norms[rows] * q_norm)
        order = np.argsort(-sims)[:top_k]
        return [(float(sims[j]), self.docs[int(rows[j])]) for j in order]

    def search(self, query: str, top_k: int = 5,
               category: str | None = None,
               path_prefix: str | None = None,
               exclude_category=None) -> dict:
        if not self.docs:
            return {"results": [], "n_docs": 0, "mode": "empty",
                    "hint": "索引为空，请运行 scripts/build_vault_index.py"}
        idx = self._match_indices(category, path_prefix, exclude_category)
        flt = {"category": category, "path_prefix": path_prefix,
               "exclude_category": exclude_category}
        if not idx:
            return {"results": [], "n_docs": len(self.docs), "n_matched": 0,
                    "mode": "no_match", "filter": flt,
                    "hint": "过滤后无文档，检查 category / path_prefix 是否正确"}
        scored = self._vector_search(query, top_k, idx)
        mode = "vector"
        if scored is None:
            scored = self._lexical_search(query, top_k, [self.docs[i] for i in idx])
            mode = "lexical"
        out = {
            "results": [{
                "score": round(s, 4),
                "text": d["text"][:500],
                "meta": d.get("meta", {}),
            } for s, d in scored],
            "n_docs": len(self.docs),
            "n_matched": len(idx),
            "mode": mode,
            "filter": flt,
        }
        if mode == "vector":
            out["embed"] = {"model": self.embed_model, "dim": self.embed_dim}
        elif self.semantic_hint:
            out["hint"] = self.semantic_hint
        return out


# 全局 store（per process）
_store = VectorStore(Path("/home/jiuben/tdx-data-feed/data/vault_store.jsonl"))


# === 工具实现 =============================================================
def vault_search(query: str, top_k: int = 5,
                 category: str | None = None,
                 path_prefix: str | None = None,
                 exclude_category=None) -> dict:
    return _store.search(query, top_k, category, path_prefix, exclude_category)


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
        "description": ("检索 vault 文档（Ollama nomic-embed-text 向量，未嵌入时降级关键词）。"
                        "category 限定 vault 一级目录（如 09-理论讲义），"
                        "path_prefix 限定路径前缀（如 09-理论讲义/01-投资学），"
                        "exclude_category 排除目录（个股/新闻类查询应排除 09-理论讲义，避免被讲义挤占）；"
                        "三者可组合，全留空为全库检索"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询文本"},
                "top_k": {"type": "integer", "default": 5},
                "category": {"type": "string",
                             "description": "vault 一级目录（meta.category），如 09-理论讲义 / 04-策略与方案"},
                "path_prefix": {"type": "string",
                                "description": "笔记相对路径前缀，用于限定单本书/单子目录"},
                "exclude_category": {"type": ["string", "array"], "items": {"type": "string"},
                                     "description": "要排除的 vault 一级目录，可传单个字符串或数组"},
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
