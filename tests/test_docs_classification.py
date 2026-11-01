"""docs/ 分层：现状规格与历史快照必须可区分。

AGENTS 已约定“历史产物不是现状”：docs/ 下带日期的 CODE_AUDIT_*、COMPARE_*、
*_RESEARCH_* 等是一次性研究快照，写作历史结论而非现状规格。本守卫把这条客观化：
  - 核心现状规格登记在册，必须存在且文件名不带日期；
  - 带日期的根级文档一律视为历史快照，不得混进核心现状规格；
  - INDEX.md 必须写明历史快照约定，让读者第一眼就能分辨。
不搬移文件（历史快照被上百处交叉引用，搬移会断链）；物理归档留作可选后续。
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


def test_index_states_the_historical_convention():
    text = (DOCS / "INDEX.md").read_text(encoding="utf-8")
    assert "历史快照" in text and "非现状" in text, (
        "INDEX.md 须写明：带日期文档是历史快照、非现状规格"
    )
