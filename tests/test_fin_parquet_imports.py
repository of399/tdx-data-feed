"""包内模块不得用裸名导入同包兄弟模块。

`tdxfeed/fin_parquet.py` 的 `incremental()` 曾写成 `from corp import ...`：
包模式（`python -m tdxfeed.full_sync`，systemd 的生产跑法）下 sys.path 里
没有 `tdxfeed/`，导入抛 ModuleNotFoundError；调用方 `full_sync` 用宽 except
吞掉它，退出码不变，finance/xdxr 两张表静默停更。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tdxfeed"

# 裸名导入会解析到「sys.path 上的任意同名模块」，包模式下同包文件不在其上。
SIBLINGS = {p.stem for p in PKG.glob("*.py")}


def _bare_sibling_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            root = (node.module or "").split(".")[0]
            if root in SIBLINGS:
                hits.append((node.lineno, root))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in SIBLINGS:
                    hits.append((node.lineno, root))
    return hits


def test_package_never_imports_siblings_by_bare_name():
    assert {"corp", "finance"} <= SIBLINGS, "守卫对象消失，用例会空跑"
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        hits = _bare_sibling_imports(path)
        if hits:
            offenders[path.relative_to(ROOT).as_posix()] = hits
    assert offenders == {}


def test_guard_catches_the_known_pattern(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from corp import fetch_xdxr\n"
        "import finance\n"
        "from tdxfeed.paths import parquet_root\n"
        "from .local import thing\n",
        encoding="utf-8",
    )
    assert _bare_sibling_imports(sample) == [(1, "corp"), (2, "finance")]
