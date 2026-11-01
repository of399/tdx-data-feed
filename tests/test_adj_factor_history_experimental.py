"""adj_factor_history 是实验模块，不得被业务悄悄接进生产链路。

AGENTS 已标注：tdxfeed/adj_factor_history.py 未落地、零消费者、仅实验，
不构成因子权威（它写 data/adj_factor_ext，与在役 hfq_factor 是两套口径）。
本守卫钉死“零业务 import”：一旦有人把它接进 tdxfeed/scripts/mcp/quant/v5，
就必须显式来这里解除登记并说明口径，否则变红——防止实验表静默成为权威。
纯静态守卫，不删除模块、不改变行为。
"""
from __future__ import annotations

import ast
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_DIRS = ("tdxfeed", "scripts", "mcp", "quant", "v5")
MODULE = "adj_factor_history"


def _imports_experiment(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == MODULE for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == MODULE:
                return True
            if any(a.name == MODULE for a in node.names):
                return True
    return False


def find_consumers(root: Path) -> list[str]:
    hits: list[str] = []
    for base in BUSINESS_DIRS:
        for path in sorted((root / base).rglob("*.py")):
            if "_archive" in path.parts or path.name == f"{MODULE}.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            if _imports_experiment(tree):
                hits.append(str(path.relative_to(root)))
    return hits


def test_experiment_module_exists():
    assert (ROOT / "tdxfeed" / f"{MODULE}.py").exists()


def test_no_business_consumer_imports_it():
    consumers = find_consumers(ROOT)
    assert consumers == [], (
        f"实验模块 {MODULE} 被业务接入（须先解除登记并说明口径）: {consumers}"
    )


def test_guard_bites_on_planted_importer(tmp_path):
    """证明守卫真能抓到接入：在副本里种一个 import 它的模块。"""
    pkg = tmp_path / "tdxfeed"
    pkg.mkdir()
    (pkg / f"{MODULE}.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "consumer.py").write_text(
        "from tdxfeed import adj_factor_history\n", encoding="utf-8")
    assert find_consumers(tmp_path) == ["tdxfeed/consumer.py"]
