"""sys.path 引导不得写死绝对路径：脚本要在任意 checkout 路径下可跑。

规则（AST）：`sys.path.insert(...)` / `sys.path.append(...)` 的第一个参数
若是绝对路径字面量（`/...` 或 `C:\\...`），即违规；应改为
`Path(__file__).resolve().parents[N]` 推导。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# 本模块只扫描源码文本，不读 data/；显式退出根 conftest 的无数据跳过。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests"}
_ABS = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in {"insert", "append"}
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            continue
        method = func.attr
        # insert(0, path) 的路径在第二个参数；append(path) 在第一个。
        candidates = node.args[1:] if method == "insert" else node.args[:1]
        if any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and _ABS.match(arg.value)
            for arg in candidates
        ):
            lines.append(node.lineno)
    return sorted(lines)


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    bad: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            found = violations(p)
        except SyntaxError:
            continue
        if found:
            bad[rel.as_posix()] = found
    return bad


def test_no_hardcoded_syspath_bootstraps():
    bad = scan()
    assert not bad, "以下文件用绝对路径引导 sys.path：\n" + "\n".join(
        f"{f}  {lines}" for f, lines in sorted(bad.items())
    )


def test_guard_catches_hardcoded_insert_and_append(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import sys\n"
        "sys.path.insert(0, '/home/u/proj')\n"
        "sys.path.append('/opt/x')\n",
        encoding="utf-8",
    )
    assert violations(sample) == [2, 3]


def test_guard_accepts_relative_derivation(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "sys.path.insert(0, str(ROOT))\n"
        "sys.path.insert(0, str(ROOT / 'scripts'))\n",
        encoding="utf-8",
    )
    assert violations(sample) == []
