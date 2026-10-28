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


def _bare_sibling_imports(path: Path) -> list[tuple[int, str]]:
    siblings = {p.stem for p in PKG.glob("*.py")}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and (node.module or "").split(".")[0] in siblings
    ]


def test_fin_parquet_imports_siblings_as_package_modules():
    assert (PKG / "corp.py").exists() and (PKG / "finance.py").exists()
    assert _bare_sibling_imports(PKG / "fin_parquet.py") == []
