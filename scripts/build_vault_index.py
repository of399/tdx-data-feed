#!/usr/bin/env python3
"""构建 StockVault / 项目 vault 的本地检索索引。

默认只写文本块（关键词检索，无需 Ollama）。加 --embed 时调用 Ollama
nomic-embed-text 生成向量，写入 `<out>.vectors.npy` 旁挂文件（float32 矩阵 +
`<out>.vectors.json` 清单），检索自动切换为余弦相似度。

向量不写进 JSONL：6,917 块 × 768 维内联会让索引从 8MB 涨到 ~80MB，
而 MCP server 每次查询都要冷启动重读一遍。旁挂 npy 由 numpy 直接载入，
JSONL 保持纯文本，行序与矩阵行序一一对应，清单里的 rows_hash 用于校验对齐。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/home/jiuben/tdx-data-feed")
DEFAULT_VAULTS = [
    Path("/home/jiuben/StockVault"),
    Path("/home/jiuben/ObsidianVaults/tdx-data-feed"),
]
DEFAULT_OUT = ROOT / "data/vault_store.jsonl"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EXCLUDE_DIRS = {".obsidian", ".trash", ".git", "node_modules", "attachments", ".bak"}

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
LIST_ITEM_RE = re.compile(r"^\s+-\s+(.*)$")
SCALAR_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
# 只有这两个 frontmatter 键会进 meta，其余（tags/book/source…）不参与检索
META_LIST_KEYS = ("related_skills",)


def vectors_paths(out: Path) -> tuple[Path, Path]:
    """索引文件 → (向量 npy, 清单 json) 路径。vault.py 必须用同一套命名。"""
    return (out.with_name(out.stem + ".vectors.npy"),
            out.with_name(out.stem + ".vectors.json"))


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 YAML 前置元数据解析：只认 `key: value` 与 `key:` + `  - item` 两种形态。

    不引入 pyyaml：vault 里几千篇笔记的 frontmatter 有各种历史写法，
    解析失败不该让整个索引构建挂掉，认不出的行直接忽略。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    cur_list: str | None = None
    for ln in m.group(1).split("\n"):
        item = LIST_ITEM_RE.match(ln)
        if item:
            if cur_list:
                meta[cur_list].append(item.group(1).strip())
            continue
        kv = SCALAR_RE.match(ln)
        if not kv:
            cur_list = None
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val:
            meta[key], cur_list = val, None
        else:
            meta[key], cur_list = [], key
    return meta, text[m.end():]


def strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    text = re.sub(r"\r\n?", "\n", strip_frontmatter(text)).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r"(?m)(?=^#{1,6}\s)", text)
    chunks: list[str] = []
    buf = ""
    step = max(1, max_chars - overlap)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(part[i:i + max_chars] for i in range(0, len(part), step))
            continue
        if len(buf) + len(part) + 1 <= max_chars:
            buf = f"{buf}\n{part}".strip()
        else:
            if buf:
                chunks.append(buf)
            tail = buf[-overlap:] if buf else ""
            buf = f"{tail}\n{part}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def pdf_reader():
    """返回 PDF → text 的函数；解析库缺失时返回 None（自动降级为只索引 markdown）。"""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # PyMuPDF 1.x 的旧名
        except ImportError:
            return None

    def read(path: Path) -> str:
        pages = []
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if text:
                    pages.append(f"〔p{i}〕\n{text}")
        return "\n\n".join(pages)

    return read


def iter_documents(vault: Path, read_pdf):
    """遍历 vault 下的 .md（以及解析库可用时的 .pdf），产出 (path, rel, text)。"""
    patterns = ["*.md"] + (["*.pdf"] if read_pdf else [])
    paths = sorted({p for pat in patterns for p in vault.rglob(pat)})
    for path in paths:
        rel = path.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        try:
            if path.suffix.lower() == ".pdf":
                text = read_pdf(path)
            else:
                text = path.read_text(encoding="utf-8")
        except Exception as e:  # 坏 PDF / 编码异常 / 权限：跳过单篇，不中断整库
            print(f"  跳过 {path}: {e}", file=sys.stderr)
            continue
        if not text.strip():
            print(f"  跳过空文档（可能是扫描版 PDF，无文本层）：{path}", file=sys.stderr)
            continue
        yield path, rel, text


def title_of(text: str, rel: Path, markdown: bool = True) -> str:
    """取标题：markdown 认第一个 H1，其余（PDF 等）直接用文件名。

    PDF 正文里也可能出现以 `# ` 开头的行，套 markdown 规则会取到莫名其妙的词。
    """
    if markdown:
        m = re.search(r"(?m)^#\s+(.+)$", text)
        if m:
            return m.group(1).strip()
    return rel.stem


def row_key(meta: dict) -> str:
    """矩阵行 ↔ JSONL 行的对齐键。mcp/servers/vault.py 有一份同公式的副本，改这里必须同步改那边。"""
    return f"{meta.get('vault')}#{meta.get('path')}#{meta.get('chunk')}"


def rows_hash(records: list[dict]) -> str:
    return hashlib.md5("\n".join(row_key(r["meta"]) for r in records).encode()).hexdigest()


def embed_batch(texts: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(texts):
        raise RuntimeError(f"embedding 返回数量不符：{len(embeddings)} != {len(texts)}")
    return embeddings


def build(vaults: list[Path], out: Path, max_chars: int, overlap: int,
          use_embed: bool, limit: int, dry_run: bool, include_pdf: bool = True) -> dict:
    read_pdf = pdf_reader() if include_pdf else None
    if include_pdf and read_pdf is None:
        print("  未安装 pymupdf，本次只索引 .md（pip install pymupdf 后可索引 PDF）",
              file=sys.stderr)
    records = []
    seen = set()
    n_notes_total = 0
    n_pdfs = 0
    for vault in vaults:
        if not vault.is_dir():
            print(f"  跳过不存在的 vault：{vault}", file=sys.stderr)
            continue
        n_notes = 0
        for path, rel, text in iter_documents(vault, read_pdf):
            if limit and len(records) >= limit:
                break
            fm, _body = parse_frontmatter(text)
            is_pdf = path.suffix.lower() == ".pdf"
            if is_pdf:
                n_pdfs += 1
            title = fm.get("title") or title_of(text, rel, markdown=not is_pdf)
            category = rel.parts[0] if len(rel.parts) > 1 else "根目录"
            for idx, chunk in enumerate(chunk_text(text, max_chars, overlap)):
                if limit and len(records) >= limit:
                    break
                chunk = chunk.strip()
                if not chunk:
                    continue
                digest = hashlib.md5(
                    f"{vault.name}:{rel}:{idx}:{chunk[:200]}".encode()
                ).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                meta = {
                    "title": str(title),
                    "path": str(rel),
                    "category": category,
                    "vault": vault.name,
                    "chunk": idx,
                }
                for key in META_LIST_KEYS:
                    if fm.get(key):
                        meta[key] = fm[key]
                records.append({
                    "text": chunk,
                    "meta": meta,
                    "embedding": [],
                    "_digest": digest,
                })
            n_notes += 1
            n_notes_total += 1
            if n_notes % 500 == 0:
                print(f"  {vault.name}: {n_notes} 篇 / {len(records)} 块", file=sys.stderr)

    vectors_file, manifest_file = vectors_paths(out)
    embedded = 0
    if use_embed and records and not dry_run:
        import numpy as np
        print(f"生成向量：{len(records)} 块（{EMBED_MODEL}）", file=sys.stderr)
        batch_size = 32
        matrix = np.zeros((len(records), 1), dtype=np.float32)
        t0 = time.time()
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            vectors = embed_batch([r["text"] for r in batch])
            arr = np.asarray(vectors, dtype=np.float32)
            if arr.ndim != 2:
                raise RuntimeError(f"embedding 维度异常：{arr.shape}")
            if matrix.shape[1] == 1:
                matrix = np.zeros((len(records), arr.shape[1]), dtype=np.float32)
            matrix[i:i + len(batch)] = arr
            embedded += len(batch)
            if (i // batch_size) % 20 == 0:
                print(f"  向量进度 {embedded}/{len(records)}"
                      f"（{time.time() - t0:.0f}s）", file=sys.stderr)
        if not dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            np.save(vectors_file, matrix)
            manifest = {
                "model": EMBED_MODEL,
                "dim": int(matrix.shape[1]),
                "n": int(matrix.shape[0]),
                "rows_hash": rows_hash(records),
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_sec": round(time.time() - t0, 1),
            }
            manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    for rec in records:
        rec.pop("_digest", None)
    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.parent / f"{out.name}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(out)
        if not embedded:
            # 纯词法索引：清掉旧向量，避免 vault.py 拿陈旧矩阵配新文本
            for stale in (vectors_file, manifest_file):
                if stale.exists():
                    stale.unlink()
    return {
        "records": len(records),
        "notes": n_notes_total,
        "pdfs": n_pdfs,
        "out": str(out),
        "embedded": embedded,
        "vectors_file": str(vectors_file) if embedded else None,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 vault 检索索引")
    parser.add_argument("--vault", action="append", type=Path,
                        help="vault 路径，可重复；默认 StockVault + 项目 vault")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--overlap", type=int, default=120)
    parser.add_argument("--embed", action="store_true",
                        help="调用 Ollama nomic-embed-text 生成向量，写入旁挂 .vectors.npy（较慢）")
    parser.add_argument("--no-pdf", action="store_true",
                        help="跳过 .pdf，只索引 .md")
    parser.add_argument("--limit", type=int, default=0, help="仅索引前 N 块（冒烟用）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    vaults = args.vault or DEFAULT_VAULTS
    print(f"vaults: {', '.join(str(v) for v in vaults)}")
    stats = build(vaults, args.out, args.max_chars, args.overlap,
                  args.embed, args.limit, args.dry_run, include_pdf=not args.no_pdf)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
