"""docs/ 分层：现状规格与历史快照必须可区分。

AGENTS 已约定“历史产物不是现状”：带日期的 CODE_AUDIT_*、COMPARE_*、
*_RESEARCH_* 等是一次性研究快照，写作历史结论而非现状规格。本守卫把这条客观化：
  - 核心现状规格登记在册，必须存在且文件名不带日期；
  - 带日期的历史快照物理归档在 docs/archive/，不得散落在 docs/ 根目录；
  - INDEX.md 必须写明历史快照约定，让读者第一眼就能分辨。
"""
from __future__ import annotations

import re
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

CURRENT_SPECS = {
    "ARCHITECTURE.md",
    "API.md",
    "WORKFLOW.md",
    "PLAN_E.md",
    "MULTIFACTOR.md",
    "VERSIONING.md",
    "INDEX.md",
    "LIVE_TRACKING_GUIDE.md",
    "HYBRID_LLM_STRATEGY.md",
    "tdx-data-feed-数据字典.md",
    "打板程序使用说明.md",
}

DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}|20\d{6})")


def test_core_specs_exist_and_are_undated():
    for name in sorted(CURRENT_SPECS):
        assert (DOCS / name).exists(), f"核心现状规格缺失: {name}"
        assert not DATE_RE.search(name), f"核心现状规格不应带日期（那是历史快照）: {name}"


def test_dated_root_docs_are_not_core_specs():
    dated = {p.name for p in DOCS.glob("*.md") if DATE_RE.search(p.name)}
    overlap = dated & CURRENT_SPECS
    assert not overlap, f"带日期的历史快照被当成核心现状规格: {sorted(overlap)}"


def test_no_dated_docs_in_docs_root():
    dated = sorted(p.name for p in DOCS.glob("*.md") if DATE_RE.search(p.name))
    assert not dated, f"带日期的历史快照应归档到 docs/archive/，不要留在根目录: {dated}"


def test_archive_holds_dated_snapshots():
    archive = DOCS / "archive"
    assert archive.is_dir(), "docs/archive/ 目录不存在（历史快照物理归档位置）"
    snapshots = [p.name for p in archive.glob("*.md") if DATE_RE.search(p.name)]
    assert snapshots, "docs/archive/ 里没有带日期的快照，归档形同虚设"


def test_index_states_the_historical_convention():
    text = (DOCS / "INDEX.md").read_text(encoding="utf-8")
    assert "历史快照" in text and "非现状" in text, (
        "INDEX.md 须写明：带日期文档是历史快照、非现状规格"
    )
    assert "docs/archive" in text, "INDEX.md 须写明历史快照的物理归档位置 docs/archive/"
