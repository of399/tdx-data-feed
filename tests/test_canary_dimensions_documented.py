"""canary 维度数必须与文档一致（KI-DOC-002 的守卫）。

关联台账条目：KI-DOC-002（canary 维度文档写 7 维、实际 8 维 → 现为 9 维）。

做法：从 canary 源码静态解析出真实维度列表（不触发任何检查副作用），
再与 docs/WORKFLOW.md 里的维度表比对 —— 文档与代码必须一致。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "scripts" / "canary_health_check.py"
DOC = ROOT / "docs" / "WORKFLOW.md"

DIM_ROW_RE = re.compile(r"\|\s*canary 维度\s*\|\s*(\d+)\s*[\(（]([^)）]*)[\)）]")
# 文档里用的是全角括号（中文排版），正则两种都要认


def _actual_dimensions() -> list[str]:
    """从 run_all_checks() 的返回列表里静态取出维度名。"""
    tree = ast.parse(CANARY.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "run_all_checks")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    dims: list[str] = []
    for call in ast.walk(ret):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id.startswith("check_")):
            dims.append(call.func.id.removeprefix("check_"))
    return dims


def _doc_dimensions() -> tuple[int, list[str]]:
    text = DOC.read_text(encoding="utf-8")
    m = DIM_ROW_RE.search(text)
    assert m, "docs/WORKFLOW.md 里找不到「canary 维度 | N（...）」行"
    n = int(m.group(1))
    names = [x.strip() for x in m.group(2).split("/") if x.strip()]
    return n, names


def test_doc_dimension_count_matches_code() -> None:
    actual = _actual_dimensions()
    count, names = _doc_dimensions()
    assert count == len(actual), (
        f"文档写 {count} 维，代码实际 {len(actual)} 维：{actual}")
    assert names == actual, f"文档维度名与代码不一致：文档 {names} / 代码 {actual}"


def test_swallowed_errors_dimension_is_documented() -> None:
    """M-4 运营闭环的维度必须出现在文档里（防止又变成隐形能力）。"""
    actual = _actual_dimensions()
    assert "swallowed_errors" in actual
    _count, names = _doc_dimensions()
    assert "swallowed_errors" in names


def test_no_stale_dimension_count_in_docs() -> None:
    """文档里不应再出现旧的 "7 维" 表述。"""
    for p in sorted((ROOT / "docs").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        assert "金丝雀健康检查（7 维）" not in text, f"{p.name} 仍写 7 维"
