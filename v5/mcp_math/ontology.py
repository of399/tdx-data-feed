"""
mcp_math/ontology.py · v5-VE1-M2 §12.5 D4.1+D4.2 知识对齐核心

D4.1 概念本体：math_ontology.yaml 加载 + 关系查询
D4.2 双语词典：双语术语映射 + 模糊匹配
D4.4 概念嵌入：用 nomic-embed-text 把概念和 LaTeX 嵌入向量空间

接口：
  load_ontology() → list[Concept]
  find_concept(query) → list[Concept] (按关键词 + 名称匹配)
  bilingual_translate(text, src_lang) → list[{en, zh}]
  embed_concepts() → {concept_id: vector}（首次调用懒加载）
  link_concepts(latex_or_text, top_k=5) → list[LinkedConcept]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 让 standalone 跑时能找到 yaml/json
try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_PKG_DIR = Path(__file__).parent
_ONTOLOGY_PATH = _PKG_DIR / "math_ontology.yaml"
_ONTOLOGY_JSON_PATH = _PKG_DIR / "math_ontology.json"  # YAML → JSON 镜像


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class Concept:
    id: str
    name: str
    name_en: str
    category: str
    is_a: list[str] = field(default_factory=list)
    part_of: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class LinkedConcept:
    concept_id: str
    name: str
    name_en: str
    category: str
    score: float  # 综合分（keyword + 嵌入）
    matched_keywords: list[str] = field(default_factory=list)
    vector_sim: float | None = None  # 嵌入相似度（如果启用）


# ============================================================================
# D4.1 加载 ontology
# ============================================================================


def _parse_simple_yaml(path: Path) -> dict:
    """加载 ontology。优先 JSON（无歧义），回退 YAML。"""
    json_path = path.parent / (path.stem + ".json")
    if json_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))
    if HAS_YAML:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    # 退路：手写极简解析（只支持列表/字典/字符串）
    text = path.read_text(encoding="utf-8")
    result: dict = {"concepts": [], "relations": {}, "stats": {}}
    current_section = None
    current_list = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.endswith(":"):
            key = stripped[:-1]
            if key in ("concepts", "relations", "stats"):
                current_section = key
                current_list = None
            continue

        if line.startswith("  - "):
            item = stripped[2:].strip()
            if current_section == "concepts":
                if item.endswith(":"):
                    current_list = {item[:-1]: ""}
                    result["concepts"].append(current_list)
                else:
                    if result["concepts"]:
                        result["concepts"][-1]["__raw"] = item
            elif current_section == "relations":
                pass
            continue

        if current_list is not None and ":" in stripped and not line.startswith("    "):
            key, _, val = stripped.partition(":")
            current_list[key.strip()] = val.strip()
            continue

    return result


_CONCEPTS: list[Concept] | None = None


def load_ontology(force_reload: bool = False) -> list[Concept]:
    """加载 ontology（懒加载 + 缓存）。"""
    global _CONCEPTS
    if _CONCEPTS is not None and not force_reload:
        return _CONCEPTS

    if not _ONTOLOGY_PATH.exists():
        return []

    raw = _parse_simple_yaml(_ONTOLOGY_PATH)
    concepts = []
    for c in raw.get("concepts", []):
        # 跳过 simple 解析中的不完整项
        if isinstance(c, dict) and "id" in c:
            concepts.append(
                Concept(
                    id=c["id"],
                    name=c.get("name", ""),
                    name_en=c.get("name_en", ""),
                    category=c.get("category", "unknown"),
                    is_a=_ensure_list(c.get("is_a")),
                    part_of=_ensure_list(c.get("part_of")),
                    requires=_ensure_list(c.get("requires")),
                    related=_ensure_list(c.get("related")),
                    keywords=_ensure_list(c.get("keywords")),
                )
            )
    _CONCEPTS = concepts
    return concepts


def _ensure_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


# ============================================================================
# D4.2 双语词典 + 模糊匹配
# ============================================================================

# 简易双语映射（来自 ontology + 手工补充 200+ 常用术语）
_BILINGUAL: dict[str, str] = {}  # en -> zh
_BILINGUAL_REV: dict[str, str] = {}  # zh -> en


def _init_bilingual_from_ontology():
    """从 ontology 反向生成双语词典。"""
    global _BILINGUAL, _BILINGUAL_REV
    if _BILINGUAL:
        return
    for c in load_ontology():
        _BILINGUAL[c.name_en.lower()] = c.name
        _BILINGUAL_REV[c.name] = c.name_en.lower()
        # 也把 keywords 里的英文部分纳入
        for kw in c.keywords:
            kw_lower = kw.lower()
            if kw_lower.isascii():
                _BILINGUAL.setdefault(kw_lower, c.name)


_init_bilingual_from_ontology()


def bilingual_translate(text: str, src: str = "auto") -> list[dict]:
    """把一段术语翻译成双语列表。src: 'en' | 'zh' | 'auto'"""
    text = text.strip()
    if not text:
        return []
    # 用换行/逗号/分号切分多个术语
    terms = re.split(r"[\n,;，；]+", text)
    results = []
    for term in terms:
        term = term.strip()
        if not term:
            continue
        if not term.isascii() and (src in ("auto", "zh")):
            # 中文 → 英文
            en = _BILINGUAL_REV.get(term)
            if en:
                results.append({"term": term, "lang": "zh", "translation": en})
                continue
            # fuzzy：包含匹配
            for zh, e in _BILINGUAL_REV.items():
                if term in zh or zh in term:
                    results.append({"term": term, "lang": "zh", "translation": e, "match": "fuzzy"})
                    break
        elif term.isascii() and (src in ("auto", "en")):
            # 英文 → 中文
            zh = _BILINGUAL.get(term.lower())
            if zh:
                results.append({"term": term, "lang": "en", "translation": zh})
                continue
            for e, z in _BILINGUAL.items():
                if term.lower() in e or e in term.lower():
                    results.append({"term": term, "lang": "en", "translation": z, "match": "fuzzy"})
                    break
    return results


# ============================================================================
# D4.1 概念查找
# ============================================================================


def find_concept(query: str, top_k: int = 5) -> list[Concept]:
    """按关键词 + 名称模糊匹配找概念。"""
    query = query.strip().lower()
    if not query:
        return []
    concepts = load_ontology()
    scored = []
    for c in concepts:
        score = 0
        matched = []
        # 精确匹配 id/name/name_en
        if c.id == query:
            score += 100
        if c.name == query or c.name_en.lower() == query:
            score += 80
        # 关键词匹配
        for kw in c.keywords:
            kw_lower = kw.lower()
            if kw_lower == query:
                score += 60
                matched.append(kw)
            elif query in kw_lower or kw_lower in query:
                score += 20
                matched.append(kw)
        # category 匹配
        if c.category in query:
            score += 5
        if score > 0:
            scored.append((score, matched, c))
    scored.sort(key=lambda x: (-x[0], x[2].id))
    return [c for _, _, c in scored[:top_k]]


# ============================================================================
# D4.4 嵌入链接（D4.1 关键词 + nomic-embed-text 向量相似度）
# ============================================================================


def _ollama_embed_sync(texts: list[str]) -> list[list[float]]:
    """同步调用 Ollama embed API。失败返回空列表。"""
    try:
        import httpx
    except ImportError:
        return []
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                "http://127.0.0.1:11434/api/embed",
                json={"model": "nomic-embed-text:latest", "input": texts, "keep_alive": "5m"},
            )
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("embeddings", [])
    except Exception:
        return []


_EMBED_CACHE: dict[str, list[float]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embed_concepts(force_rebuild: bool = False) -> dict[str, list[float]]:
    """把每个概念（含 name_en + name + keywords）嵌入为向量。"""
    global _EMBED_CACHE
    if _EMBED_CACHE and not force_rebuild:
        return _EMBED_CACHE
    concepts = load_ontology()
    if not concepts:
        return {}
    texts = []
    ids = []
    for c in concepts:
        text = f"{c.name_en} {c.name} {' '.join(c.keywords)}"
        texts.append(text)
        ids.append(c.id)
    embeddings = _ollama_embed_sync(texts)
    if not embeddings:
        return {}
    for cid, emb in zip(ids, embeddings, strict=False):
        _EMBED_CACHE[cid] = emb
    return _EMBED_CACHE


def link_concepts(query: str, top_k: int = 5, use_embed: bool = True) -> list[LinkedConcept]:
    """
    给定一段文字/LaTeX，找最相关的 ontology 概念。
    综合分 = 关键词匹配分 + (嵌入相似度 × 50)
    """
    if not query.strip():
        return []

    concepts = load_ontology()
    find_concept(query, top_k=len(concepts))

    # 关键词分
    keyword_scores: dict[str, tuple[float, list[str]]] = {}
    query_lower = query.lower()
    for c in concepts:
        score = 0
        matched = []
        if c.id in query_lower or c.name in query or c.name_en.lower() in query_lower:
            score += 80
        for kw in c.keywords:
            if kw.lower() in query_lower:
                score += 40
                matched.append(kw)
        if c.category in query_lower:
            score += 10
        if score > 0:
            keyword_scores[c.id] = (score, matched)

    # 嵌入相似度
    vector_sims: dict[str, float] = {}
    if use_embed:
        cache = embed_concepts()
        if cache:
            q_emb = _ollama_embed_sync([query])
            if q_emb:
                q_vec = q_emb[0]
                for cid, emb in cache.items():
                    sim = _cosine(q_vec, emb)
                    vector_sims[cid] = sim

    # 合并
    linked = []
    for c in concepts:
        kw_score, matched = keyword_scores.get(c.id, (0, []))
        v_sim = vector_sims.get(c.id, 0.0)
        combined = kw_score + v_sim * 50
        if combined > 0:
            linked.append(
                LinkedConcept(
                    concept_id=c.id,
                    name=c.name,
                    name_en=c.name_en,
                    category=c.category,
                    score=round(combined, 2),
                    matched_keywords=matched,
                    vector_sim=round(v_sim, 3) if v_sim else None,
                )
            )
    linked.sort(key=lambda x: -x.score)
    return linked[:top_k]


# ============================================================================
# 自检
# ============================================================================

if __name__ == "__main__":
    print("=== ontology 自检 ===\n")

    print("[1] 加载 ontology")
    concepts = load_ontology()
    print(f"   加载 {len(concepts)} 个概念")
    if concepts:
        print(f"   示例: {concepts[0].id} ({concepts[0].name_en})")

    print("\n[2] 概念查找 find_concept")
    for q in ["导数", "integral", "特征值", "矩阵"]:
        results = find_concept(q, top_k=2)
        names = [f"{c.id}({c.name_en})" for c in results]
        print(f"   '{q}' → {names}")

    print("\n[3] 双语翻译")
    for t in ["derivative", "矩阵 特征值"]:
        print(f"   {t!r} → {bilingual_translate(t)}")

    print("\n[4] 嵌入链接（nomic-embed-text）")
    emb_cache = embed_concepts()
    print(f"   嵌入 {len(emb_cache)} 个概念")
    if emb_cache:
        for q in ["导数", "eigenvalue matrix", "积分", "正态分布"]:
            results = link_concepts(q, top_k=3)
            print(f"   '{q}' → {[(r.concept_id, r.score) for r in results]}")

    print("\n=== 自检完毕 ===")
