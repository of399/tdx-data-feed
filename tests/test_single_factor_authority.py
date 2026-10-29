"""价格派生只经 tdxfeed/adjusted.py：八个模块不得引用因子表。

规则（AST，避开注释与 docstring 误伤）：
  R1 标识符 / 属性名命中 FORBIDDEN；
  R2 import 的模块名命中；
  R3 非 docstring 的字符串常量命中。
"""
from __future__ import annotations

import ast
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
GUARDED = [
    "tdxfeed/features_5d.py",
    "tdxfeed/factors_candidates.py",
    "tdxfeed/factors_polars.py",
    "scripts/train_daban_model.py",
    "scripts/find_d_signal_5d.py",
    "scripts/feature_engineering.py",
    "scripts/train_pnl_proxy.py",
    "tdxfeed/stage1_wide_table.py",
]
FORBIDDEN = {
    "adj_factor", "adj_factor_dir", "adj_factor_path",
    "hfq_factor", "hfq_factor_dir", "hfq_factor_path",
}


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN:
            lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            lines.add(node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] in FORBIDDEN for alias in node.names):
                lines.add(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] in FORBIDDEN \
                    or any(alias.name.split(".")[-1] in FORBIDDEN for alias in node.names):
                lines.add(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs and node.value in FORBIDDEN:
                lines.add(node.lineno)
    return sorted(lines)


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    bad = {}
    for rel in GUARDED:
        p = root / rel
        found = violations(p)
        if found:
            bad[rel] = found
    return bad


def test_guarded_modules_exist():
    for rel in GUARDED:
        assert (ROOT / rel).exists(), rel


def test_no_module_reads_the_factor_tables_directly():
    bad = scan()
    assert not bad, f"这些模块绕过 adjusted.py 直接引用因子表: {bad}"


def test_guard_catches_direct_reference(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        "from tdxfeed.paths import hfq_factor_path\n"
        "A = 'hfq_factor'\n",
        encoding="utf-8",
    )
    assert violations(p) == [1, 2]


def test_guard_ignores_docstring_mention(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        '"""说明文字：不读 adj_factor / hfq_factor。"""\n'
        "B = 1\n",
        encoding="utf-8",
    )
    assert violations(p) == []
