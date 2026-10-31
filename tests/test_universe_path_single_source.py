"""名单文件路径必须从 `tdxfeed.paths.universe_path()` 取，不得自拼。

规则（AST，非正则，避免注释与文档字符串误伤）：
  R1 任意调用的直接实参里出现含 all_a_stocks.json 的字面量；
  R2 任何 `a / b` 拼接里出现该字面量；
  R3 赋值/返回的字面量（≤160 字符）里出现该字面量。
白名单只有两个路径权威层（v5 是零耦合孪生）。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 本模块只读源码文本，不读 data/；docstring 含 data/ 字样会被根 conftest 误判，故显式退出。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
MARK = "all_a_stocks.json"
WHITELIST = {"tdxfeed/paths.py", "v5/scripts/_paths.py"}
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests"}


def _strs(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _suspect(node: ast.AST) -> bool:
    return any(MARK in v for v in _strs(node))


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # f-string 不在其列：错误消息里提到文件名是说明文字，不是路径拼接。
            direct = [a for a in [*node.args, *node.keywords]
                      if isinstance(a, (ast.Constant, ast.BinOp))]
            if any(_suspect(a) for a in direct):
                lines.add(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _suspect(node):
                lines.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
            value = getattr(node, "value", None)
            if not isinstance(value, (ast.Constant, ast.JoinedStr)):
                continue
            text = "".join(_strs(value))
            if "\n" in text or len(text) > 160:
                continue
            if _suspect(value):
                lines.add(node.lineno)
    return sorted(lines)


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    bad: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.as_posix() in WHITELIST:
            continue
        try:
            found = violations(p)
        except SyntaxError:
            continue
        if found:
            bad[rel.as_posix()] = found
    return bad


def test_no_self_concatenated_universe_path():
    bad = scan()
    rows = [f"{f}  {lines}" for f, lines in sorted(bad.items())]
    assert not bad, "以下文件自拼名单路径，应改用 paths.universe_path()：\n" + "\n".join(rows)


def test_guard_catches_the_three_shapes(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "ROOT = Path('/x')\n"
        "A = ROOT / 'data' / 'all_a_stocks.json'\n"
        "B = Path('/home/u/proj/data/all_a_stocks.json')\n"
        "C = 'all_a_stocks.json'\n",
        encoding="utf-8",
    )
    assert violations(sample) == [3, 4, 5]


def test_guard_ignores_comments_and_docstrings(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""名单在 data/all_a_stocks.json 下（只是说明文字）。"""\n'
        "# all_a_stocks.json 也是\n"
        "META = {'note': 'data/all_a_stocks.json'}\n",
        encoding="utf-8",
    )
    assert violations(sample) == []


def test_guard_whitelists_the_authority_modules():
    for rel in WHITELIST:
        assert (ROOT / rel).exists(), rel
