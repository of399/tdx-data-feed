"""
v5/mcp_math/ontology.py 验收测试（D4 知识对齐）

测试 4 项：
  [1] D4.1 概念本体加载 + 查找
  [2] D4.2 双语翻译（中→英、英→中、模糊匹配）
  [3] D4.4 嵌入链接（关键词 + nomic-embed-text 向量相似度）
  [4] HTTP 端点 math_concept_link / math_bilingual_translate
"""

import asyncio
import json
import sys

import httpx

sys.path.insert(0, "/home/jiuben/tdx-data-feed")
sys.path.insert(0, "/home/jiuben/tdx-data-feed/v5")

from mcp_math.ontology import (
    bilingual_translate,
    find_concept,
    link_concepts,
    load_ontology,
)

BASE = "http://127.0.0.1:8002"
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    print("=== D4 知识对齐验收测试 ===\n")

    # [1] 加载 ontology
    print("[1] D4.1 概念本体")
    concepts = load_ontology()
    check("加载 ≥ 40 个概念", len(concepts) >= 40, f"got {len(concepts)}")
    ids = {c.id for c in concepts}
    check("含 derivative", "derivative" in ids)
    check("含 integral", "integral" in ids)
    check("含 eigenvalue", "eigenvalue" in ids)
    check("含 normal_distribution", "normal_distribution" in ids)
    check("含 triangle", "triangle" in ids)

    print("\n[2] D4.1 find_concept")
    # 中文
    r = find_concept("导数", top_k=1)
    check("'导数' → derivative", len(r) > 0 and r[0].id == "derivative", str([c.id for c in r]))
    # 英文
    r = find_concept("integral", top_k=1)
    check("'integral' → integral", len(r) > 0 and r[0].id == "integral", str([c.id for c in r]))
    # 中文多概念
    r = find_concept("特征值", top_k=1)
    check("'特征值' → eigenvalue", len(r) > 0 and r[0].id == "eigenvalue", str([c.id for c in r]))
    # 类别匹配
    r = find_concept("calculus", top_k=3)
    check("'calculus' → 多个 calculus 概念", len(r) >= 1, str([c.id for c in r]))

    print("\n[3] D4.2 双语翻译")
    # 英 → 中
    r = bilingual_translate("derivative")
    check("derivative → 导数", any(t.get("translation") == "导数" for t in r), str(r))
    # 多个英文
    r = bilingual_translate("integral, eigenvalue")
    check("integral + eigenvalue → 2 个翻译", len(r) == 2, str(r))
    # 中 → 英
    r = bilingual_translate("矩阵")
    check("矩阵 → matrix", any(t.get("translation") == "matrix" for t in r), str(r))

    print("\n[4] D4.4 嵌入链接（关键词 + nomic-embed-text）")
    cases = [
        ("导数", "derivative"),
        ("integral", "integral"),
        ("eigenvalue matrix", "eigenvalue"),
        ("正态分布", "normal_distribution"),
        ("勾股定理", "pythagorean_theorem"),
    ]
    for query, expected_top1 in cases:
        results = link_concepts(query, top_k=3)
        top1 = results[0].concept_id if results else None
        check(
            f"'{query}' top1 = {expected_top1}",
            top1 == expected_top1,
            f"got {top1} (scores: {[(r.concept_id, r.score) for r in results[:3]]})",
        )
        if results:
            print(f"   '{query}' → {[(r.concept_id, round(r.score, 1)) for r in results[:3]]}")

    print("\n[5] HTTP 端点")

    async def http_test():
        async with httpx.AsyncClient(base_url=BASE, timeout=10) as c:
            # math_concept_link
            r = await c.post("/tools/math_concept_link", json={"query": "导数", "top_k": 3})
            data = r.json()
            check("HTTP 200", r.status_code == 200)
            check("返回 ≥ 1 个 linked", len(data.get("linked", [])) >= 1)
            check("top1 = derivative", data["linked"][0]["concept_id"] == "derivative")
            print(
                f"   HTTP link top1: {data['linked'][0]['concept_id']} ({data['linked'][0]['score']})"
            )

            # math_bilingual_translate
            r = await c.post("/tools/math_bilingual_translate", json={"text": "derivative"})
            data = r.json()
            check("HTTP bilingual 200", r.status_code == 200)
            check(
                "bilingual 含导数",
                any(t.get("translation") == "导数" for t in data["translations"]),
            )

            # math_ontology_stats
            r = await c.post("/tools/math_ontology_stats", json={})
            data = r.json()
            check("HTTP stats 200", r.status_code == 200)
            check("total_concepts ≥ 40", data.get("total_concepts", 0) >= 40)

    asyncio.run(http_test())

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} pass / {FAIL} fail")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
