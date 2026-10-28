"""theory-reference（财经七书讲义检索）路由、vault 目录过滤与交叉链接测试。

覆盖三条不变量：
1. 讲义检索只在「查询不含股票代码」时触发，绝不劫持个股/新闻查询；
2. `09-理论讲义` 与个股笔记双向隔离——讲义查询锁进该目录，个股查询排除该目录；
3. build_vault_index.py 与 mcp/servers/vault.py 各自持有一份行序指纹公式，必须一致，
   否则向量矩阵会与文本错位（错位时只能退回词法，不能给出错误的相似度）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skill_router.orchestrator import Orchestrator  # noqa: E402

VAULT_ROOT = Path("/home/jiuben/ObsidianVaults/tdx-data-feed")
THEORY_DIR = VAULT_ROOT / "09-理论讲义"
VAULT_STORE = ROOT / "data/vault_store.jsonl"
SKILL_YAML = ROOT / "skills/theory-reference.yaml"


def _load(name: str, relpath: str):
    """按文件路径载入模块：mcp/servers 与 scripts 都不是包，无法直接 import。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vault = _load("vault_server_under_test", "mcp/servers/vault.py")
bvi = _load("build_vault_index_under_test", "scripts/build_vault_index.py")
imp = _load("import_theory_notes_under_test", "scripts/import_theory_notes.py")

THEORY_CATEGORY = "09-理论讲义"


@pytest.fixture(scope="module")
def orch() -> Orchestrator:
    return Orchestrator(skills_dir=ROOT / "skills", mcp_dir=ROOT / "mcp" / "servers")


@pytest.fixture(scope="module")
def skill() -> dict:
    return yaml.safe_load(SKILL_YAML.read_text(encoding="utf-8"))


# === 1. 意图路由 ==========================================================
THEORY_QUERIES = [
    "什么是异方差？对回归推断有什么影响",
    "投资学里的有效市场假说定义是什么",
    "计量经济学第 4 章讲了什么",
    "CAPM 的公式怎么推导",
    "会计学和公司理财的区别是什么",
    "讲义里的杜邦分解怎么讲",
    "20261021 什么是打板",          # 日期串不能被当成股票代码
]

# (查询, 期望意图)：这些都必须留在原有链路上
NON_THEORY = [
    ("sh600000 怎么看？", "kline-analysis"),
    ("sh600519 财务怎么样？营收近 4 季度趋势", "financial-summary"),
    ("sh600519 的护城河是什么？ROE 怎么样", "financial-summary"),
    ("sh600000 的复权因子", "adj-factor"),
    ("sz000858 有锤子形态吗？MACD 金叉吗？", "indicator-match"),
    ("sh600000 最近有什么新闻？", "news-summary"),
    ("600000 浦发银行 新闻", "news-summary"),
    ("sh600000 什么是市盈率", "financial-summary"),   # 带代码就不走讲义
    ("推荐今日打板股票", "daban"),
    ("大盘为什么跌", "indicator-match"),              # 「为什么」不足以触发讲义
]


@pytest.mark.parametrize("query", THEORY_QUERIES)
def test_theory_queries_route_to_theory_reference(orch, query):
    assert orch.route(query) == "theory-reference"


@pytest.mark.parametrize("query,expected", NON_THEORY)
def test_non_theory_queries_keep_their_route(orch, query, expected):
    assert orch.route(query) == expected


def test_book_aliases_are_not_shadowed(orch):
    """长书名必须排在短别名前面，否则「金融市场与金融机构」会被截成「金融市场」。"""
    assert orch.extract_book("金融市场与金融机构的讲义") == "金融市场与金融机构"
    assert orch.extract_book("金融市场怎么分类") == "金融市场与金融机构"


def test_extract_book_only_narrows_on_a_single_book(orch):
    assert orch.extract_book("投资学里的有效市场假说") == "投资学"
    assert orch.extract_book("计量经济学第 4 章") == "计量经济学"
    # 命中多本 → 跨书比较，锁死单本会漏答案
    assert orch.extract_book("会计学和公司理财的区别") == "all"
    assert orch.extract_book("什么是异方差") == "all"


# === 2. vault 目录过滤 ====================================================
@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_theory_search_stays_inside_theory_dir():
    r = vault._store.search("异方差 标准误", top_k=5, category=THEORY_CATEGORY)
    assert r["mode"] in ("vector", "lexical")
    assert r["results"], "讲义库应有命中"
    assert all(x["meta"]["category"] == THEORY_CATEGORY for x in r["results"])
    assert all(x["meta"]["path"].startswith(THEORY_CATEGORY) for x in r["results"])


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_exclude_category_protects_stock_queries():
    """个股/新闻检索排除讲义后，top-k 不得再被讲义正文占位。"""
    query = "600000 浦发银行 新闻"
    r = vault._store.search(query, top_k=5, exclude_category=THEORY_CATEGORY)
    assert r["results"]
    assert not any(x["meta"]["path"].startswith(THEORY_CATEGORY) for x in r["results"])
    n_theory = len(vault._store.filtered(category=THEORY_CATEGORY))
    assert n_theory > 0, "讲义未入索引，隔离测试无意义"
    assert r["n_matched"] == r["n_docs"] - n_theory


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_exclude_category_accepts_a_list():
    banned = [THEORY_CATEGORY, "08-报告"]
    r = vault._store.search("银行", top_k=3, exclude_category=banned)
    assert all(x["meta"]["category"] not in banned for x in r["results"])
    assert r["n_matched"] < r["n_docs"]


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_path_prefix_narrows_to_one_book():
    prefix = f"{THEORY_CATEGORY}/06-计量经济学"
    r = vault._store.search("回归", top_k=3, category=THEORY_CATEGORY, path_prefix=prefix)
    assert all(x["meta"]["path"].startswith(prefix) for x in r["results"])


def test_unknown_filter_returns_no_match_with_hint():
    r = vault._store.search("任意查询", category="__不存在的目录__")
    assert r["mode"] == "no_match"
    assert r["results"] == [] and r["n_matched"] == 0
    assert r["hint"] and r["filter"]["category"] == "__不存在的目录__"


# === 3. 行序指纹：两份公式必须一致 =========================================
def test_rows_hash_formula_is_identical_on_both_sides():
    records = [
        {"text": f"正文 {i}", "meta": {"vault": "v", "path": f"a/{i}.md", "chunk": i}}
        for i in range(3)
    ]
    assert vault._rows_hash(records) == bvi.rows_hash(records)


def _write_store(path: Path, docs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs),
                    encoding="utf-8")


def _one_doc() -> list[dict]:
    return [{"text": "异方差只伤标准误，不影响 OLS 无偏性",
             "meta": {"vault": "v", "path": "a.md", "chunk": 0, "category": "c"},
             "embedding": []}]


def _write_vectors(store: Path, rows: int, dim: int, rows_hash: str) -> None:
    npy, manifest = vault._vectors_paths(store)
    np.save(npy, np.ones((rows, dim), dtype=np.float32))
    manifest.write_text(json.dumps({"model": "nomic-embed-text", "dim": dim,
                                    "n": rows, "rows_hash": rows_hash}),
                        encoding="utf-8")


def test_aligned_vectors_enable_semantic_search(tmp_path):
    store = tmp_path / "s.jsonl"
    docs = _one_doc()
    _write_store(store, docs)
    _write_vectors(store, rows=1, dim=4, rows_hash=bvi.rows_hash(docs))
    st = vault.VectorStore(store)
    assert st.semantic_ready and st.embed_dim == 4


def test_row_count_mismatch_falls_back_to_lexical(tmp_path):
    store = tmp_path / "s.jsonl"
    docs = _one_doc()
    _write_store(store, docs)
    _write_vectors(store, rows=2, dim=4, rows_hash=bvi.rows_hash(docs))
    st = vault.VectorStore(store)
    assert not st.semantic_ready
    assert "不一致" in st.semantic_hint


def test_stale_rows_hash_falls_back_with_hint(tmp_path):
    """笔记改了但向量没重建：宁可退回词法，也不能拿错位向量算相似度。"""
    store = tmp_path / "s.jsonl"
    _write_store(store, _one_doc())
    _write_vectors(store, rows=1, dim=4, rows_hash="deadbeef")
    st = vault.VectorStore(store)
    assert not st.semantic_ready
    r = st.search("异方差")
    assert r["mode"] == "lexical"
    assert "不匹配" in r["hint"]
    assert r["results"], "词法兜底仍应命中"


def test_missing_vectors_reports_how_to_enable(tmp_path):
    store = tmp_path / "s.jsonl"
    _write_store(store, _one_doc())
    st = vault.VectorStore(store)
    assert not st.semantic_ready
    assert "--embed" in st.semantic_hint
    assert st.search("异方差")["mode"] == "lexical"


def test_add_doc_invalidates_vector_matrix(tmp_path):
    store = tmp_path / "s.jsonl"
    docs = _one_doc()
    _write_store(store, docs)
    _write_vectors(store, rows=1, dim=4, rows_hash=bvi.rows_hash(docs))
    st = vault.VectorStore(store)
    assert st.semantic_ready
    st.add("新文档", {"category": "c"})
    assert not st.semantic_ready
    assert "add_doc" in st.semantic_hint


# === 4. frontmatter 解析与交叉链接 =========================================
FM_SAMPLE = """---
title: 公司理财 · 模块 1　财务的起点：报表、现金流与折现
category: 09-理论讲义
book: 公司理财
tags:
  - 财经七书
  - 模块1
related_skills:
  - financial-summary
  - financial-statement
source: 财经七书学习讲义_模块精讲版.md
---

# 正文标题
"""


def test_parse_frontmatter_extracts_related_skills():
    fm, body = bvi.parse_frontmatter(FM_SAMPLE)
    assert fm["title"].startswith("公司理财")
    assert fm["category"] == THEORY_CATEGORY
    assert fm["related_skills"] == ["financial-summary", "financial-statement"]
    assert fm["tags"] == ["财经七书", "模块1"]
    assert body.lstrip().startswith("# 正文标题")


def test_parse_frontmatter_absent_is_a_noop():
    fm, body = bvi.parse_frontmatter("# 只有正文\n")
    assert fm == {}
    assert body.startswith("# 只有正文")


def test_parse_frontmatter_tolerates_messy_yaml():
    """认不出的行直接忽略：几千篇历史笔记的 frontmatter 写法各异，不能因此中断构建。"""
    messy = "---\n标题: 中文键\ntitle: a: b: c\n  - 悬空列表项\nnested:\n  deep: 1\n---\n正文\n"
    fm, body = bvi.parse_frontmatter(messy)
    assert fm["title"] == "a: b: c"
    assert "标题" not in fm and "nested" not in fm.get("title", "")
    assert body == "正文\n"


def test_every_book_dir_has_a_skill_mapping():
    """新增分册时若忘了配 BOOK_SKILLS，交叉链接会静默失效——这里锁住。"""
    if not THEORY_DIR.exists():
        pytest.skip("讲义笔记未导入")
    dirs = {p.name for p in THEORY_DIR.iterdir() if p.is_dir()}
    unmapped = [d for d in dirs if not any(
        d.endswith(imp.slug(book)) for book in imp.BOOK_SKILLS)]
    assert not unmapped, f"以下分册未在 BOOK_SKILLS 中登记：{unmapped}"


def test_theory_overview_notes_carry_related_skills():
    if not THEORY_DIR.exists():
        pytest.skip("讲义笔记未导入")
    checked = 0
    for book, skills in imp.BOOK_SKILLS.items():
        if not skills:
            continue
        dirname = next((p for p in THEORY_DIR.iterdir()
                        if p.is_dir() and p.name.endswith(imp.slug(book))), None)
        assert dirname, f"《{book}》目录缺失"
        fm, _ = bvi.parse_frontmatter((dirname / "00-总览.md").read_text(encoding="utf-8"))
        assert fm.get("related_skills") == skills, f"《{book}》交叉链接不符"
        checked += 1
    assert checked >= 7, f"至少 7 本书应带交叉链接，实际 {checked}"


# === 5. 技能契约 ==========================================================
def test_skill_is_pure_tool_and_book_paths_cover_the_enum(skill):
    assert skill["name"] == "theory-reference"
    assert skill["llm"]["model_id"] == "none", "讲义检索必须是纯检索，不走 LLM"
    assert skill["llm"]["inference"] is None
    books = skill["input_schema"]["properties"]["book"]["enum"]
    assert set(books) <= set(skill["book_paths"])
    assert "mcp:tdx-vault.search" in skill["tools"]


def test_book_paths_point_at_real_dirs(skill):
    if not THEORY_DIR.exists():
        pytest.skip("讲义笔记未导入")
    for book, prefix in skill["book_paths"].items():
        assert (VAULT_ROOT / prefix).is_dir(), f"{book} 的路径不存在：{prefix}"


# === 6. 端到端（需要索引 + MCP 子进程）=====================================
@pytest.fixture()
def runtime():
    from skill_router.runtime import SkillRuntime
    rt = SkillRuntime(ROOT / "skills", ROOT / "mcp" / "servers")
    rt.load_all_skills()
    try:
        yield rt
    finally:
        rt.shutdown()


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_runtime_theory_reference_returns_original_text(runtime):
    r = runtime.execute_skill("theory-reference",
                              {"query": "异方差 标准误", "book": "计量经济学"})
    assert r["ok"], r.get("error")
    assert r["mode"] in ("vector", "lexical")
    assert r["hits"], "计量经济学分册应有命中"
    prefix = f"{THEORY_CATEGORY}/06-计量经济学"
    assert all(h["path"].startswith(prefix) for h in r["hits"])
    assert all(h["text"] for h in r["hits"])
    # C5 交叉链接：计量经济学 → backtest / indicator-match
    assert set(r["related_skills"]) & {"backtest", "indicator-match"}


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_runtime_rejects_theory_reference_without_query(runtime):
    r = runtime.execute_skill("theory-reference", {})
    assert not r["ok"]
    assert "query" in r["error"]


@pytest.mark.skipif(not VAULT_STORE.exists(), reason="vault 索引未构建")
def test_runtime_news_fallback_excludes_theory(runtime):
    """runtime 的 news-summary 兜底必须带上 exclude_category，否则讲义会混进新闻。"""
    from skill_router.runtime import THEORY_CATEGORY as RT_CATEGORY
    assert RT_CATEGORY == THEORY_CATEGORY
    resp = runtime.call_mcp("tdx-vault", "search", {
        "query": "600000 浦发银行 新闻",
        "top_k": 5,
        "exclude_category": RT_CATEGORY,
    })
    assert resp.get("results")
    assert not any(x["meta"]["path"].startswith(THEORY_CATEGORY)
                   for x in resp["results"])
