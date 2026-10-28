"""业务代码不得写死数据表路径。

规则三条（AST，非正则，避免注释与文档字符串误伤）：
  R1 路径类调用的直接参数里出现可疑字面量；
  R2 任何 `a / b` 拼接里同时出现 "data" 与表段名；
  R3 单行字面量赋值/返回（≤160 字符）里出现可疑字面量。
白名单只有两个模块：它们就是权威层本身。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# 本模块只读仓库源码文本做 AST 扫描，不读 data/；样例字符串里的 data/ 字样
# 会被根 conftest 的「无数据跳过」分类误判，故显式退出。
# （放在 import 块之后：语句插在 import 之前会触发 E402。）
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]

FLAT = re.compile(r"data[/\\](?:parquet\b|adj_factor\b|hfq_factor\b|finance\b)")
SEGMENTS = {"parquet", "adj_factor", "hfq_factor", "finance", "finance.parquet", "xdxr.parquet"}
PATH_CALLS = {
    "Path", "read_parquet", "to_parquet", "read_csv", "to_csv", "open",
    "glob", "exists", "read_text", "write_text", "listdir", "mkdir",
    "iterdir", "rglob",
}
WHITELIST = {"tdxfeed/paths.py", "v5/scripts/_paths.py"}
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests"}


def _strs(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _suspect(node: ast.AST) -> bool:
    values = _strs(node)
    if any(FLAT.search(v) for v in values):
        return True
    return "data" in values and any(v in SEGMENTS for v in values)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in PATH_CALLS:
            direct = [a for a in [*node.args, *node.keywords]
                      if isinstance(a, (ast.Constant, ast.JoinedStr, ast.BinOp))]
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


def _report(bad: dict[str, list[int]]) -> str:
    rows = [f"{f}  {lines}" for f, lines in sorted(bad.items())[:20]]
    tail = "" if len(bad) <= 20 else f"\n... 另有 {len(bad) - 20} 个文件"
    return (f"{len(bad)} 文件 / {sum(len(v) for v in bad.values())} 行写死了数据表路径：\n"
            + "\n".join(rows) + tail)


def test_no_hardcoded_table_paths():
    bad = scan()
    assert not bad, _report(bad)


def test_guard_catches_the_three_rules(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "ROOT = Path('/x')\n"
        "A = ROOT / 'data' / 'parquet' / 'daily'\n"
        "B = '/home/u/proj/data/adj_factor'\n"
        "C = open('data/parquet/finance.parquet')\n",
        encoding="utf-8",
    )
    assert violations(sample) == [3, 4, 5]


def test_guard_ignores_comments_and_docstrings(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""数据在 data/parquet/daily 下（只是说明文字）。"""\n'
        "# data/adj_factor 也是\n"
        "META = {'note': 'data/parquet'}\n",
        encoding="utf-8",
    )
    assert violations(sample) == []


def test_scan_reports_violations_under_an_injected_root(tmp_path):
    (tmp_path / "mod.py").write_text("A = '/x/data/parquet/daily/y.parquet'\n", encoding="utf-8")
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "deep.py").write_text(
        "from pathlib import Path\nP = Path('data') / 'adj_factor' / '600519.parquet'\n",
        encoding="utf-8")
    (tmp_path / "ok.py").write_text("B = 1\n", encoding="utf-8")
    bad = scan(root=tmp_path)
    assert set(bad) == {"mod.py", "package/deep.py"}, bad
    assert bad["mod.py"] == [1]
    assert bad["package/deep.py"] == [2]


def test_guard_whitelists_the_authority_modules():
    for rel in WHITELIST:
        assert (ROOT / rel).exists(), rel
