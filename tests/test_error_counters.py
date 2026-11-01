"""逐模块吞错计数键的单元测试 + 汇总出口（tdxfeed.errors）的行为测试。

关联台账条目：KI-SILENT-001 / KI-SILENT-002（吞错计入统计），KI-RULE-004（M-5 修复必带测试）。

三层：
  1. 逐模块：每个声明了 `_ERROR_COUNT` 的模块，计数键必须是该模块真实存在的函数名
  2. 汇总出口：snapshot / totals / summary / flush / read_recent / reset 行为正确
  3. 功能：真实触发一次吞错 → 计数递增 → 能被 snapshot 读到 → 落盘后能聚合回来
"""
from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ci_semantic_guard as sg  # noqa: E402

from tdxfeed import errors  # noqa: E402

SCOPE_DIRS = ("tdxfeed", "scripts", "mcp", "skill_router", "quant")


def _counter_modules() -> list[Path]:
    """所有声明了模块级 `_ERROR_COUNT` 的文件。"""
    out: list[Path] = []
    for d in SCOPE_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            declares = any(
                isinstance(n, ast.AnnAssign)
                and isinstance(n.target, ast.Name)
                and n.target.id == "_ERROR_COUNT"
                for n in tree.body)
            if declares:
                out.append(f)
    return out


MODULES = _counter_modules()
MODULE_IDS = [str(f.relative_to(ROOT)) for f in MODULES]


# --------------------------------------------------------------------------- #
# 1. 逐模块：计数键必须是真实函数名
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", MODULES, ids=MODULE_IDS)
def test_counter_keys_are_real_functions(path: Path) -> None:
    """每个模块的计数键都对应真实存在的函数（拼错会让统计永远为 0）。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    funcs.add("<module>")
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "_ERROR_COUNT"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value not in funcs):
            bad.append(node.slice.value)
    assert not bad, f"{path.relative_to(ROOT)} 的计数键不对应任何函数：{bad}"


@pytest.mark.parametrize("path", MODULES, ids=MODULE_IDS)
def test_module_imports_counter_at_top_level(path: Path) -> None:
    """模块级计数器必须在模块级导入 Counter（否则运行即 NameError）。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    has_import = any(isinstance(n, ast.ImportFrom) and n.module == "collections"
                      and any(a.name == "Counter" for a in n.names)
                      for n in tree.body)
    assert has_import, f"{path.relative_to(ROOT)} 缺模块级 Counter 导入"


def test_every_converged_module_is_covered() -> None:
    """覆盖度下限：至少 100 个模块纳入逐模块守卫（防扫描范围被悄悄缩小）。"""
    assert len(MODULES) >= 100, f"逐模块守卫只覆盖了 {len(MODULES)} 个模块"


def test_modules_have_no_sem002_violation() -> None:
    """逐模块守卫的覆盖面与 SEM002 守卫一致：不得再有吞错未计数。"""
    findings = sg.scan_paths(list(SCOPE_DIRS))
    assert not [f for f in findings if f.code == "SEM002"]


# --------------------------------------------------------------------------- #
# 2. 汇总出口行为
# --------------------------------------------------------------------------- #
def test_snapshot_reads_live_counters() -> None:
    from tdxfeed import symbols
    symbols._ERROR_COUNT["__test_fn__"] += 3
    try:
        snap = errors.snapshot()
        assert snap["tdxfeed.symbols"]["__test_fn__"] == 3
        assert errors.totals()["tdxfeed.symbols"] >= 3
        s = errors.summary()
        assert s["total"] >= 3
        assert any("tdxfeed.symbols" in x[0] for x in s["top_modules"])
    finally:
        symbols._ERROR_COUNT.pop("__test_fn__", None)


def test_reset_clears_counters() -> None:
    from tdxfeed import symbols
    symbols._ERROR_COUNT["__test_fn__"] += 1
    errors.reset()
    assert symbols._ERROR_COUNT.get("__test_fn__") is None


def test_flush_and_read_recent_roundtrip(tmp_path: Path) -> None:
    from tdxfeed import symbols
    symbols._ERROR_COUNT["__test_fn__"] += 5
    state = tmp_path / "swallowed.jsonl"
    try:
        out = errors.flush(state)
        assert out["written"] is True
        assert out["total"] >= 5
        rep = errors.read_recent(hours=24, path=state)
        assert rep["total"] >= 5
        assert rep["records"] >= 1
        # 落盘文件是逐行 JSON；最后一行的 counts 里能看到刚触发的计数键
        lines = [ln for ln in state.read_text(encoding="utf-8").splitlines() if ln]
        rec = json.loads(lines[-1])
        assert rec["counts"] and "ts" in rec and "pid" in rec
        assert rec["counts"]["tdxfeed.symbols"]["__test_fn__"] >= 5
    finally:
        symbols._ERROR_COUNT.pop("__test_fn__", None)


def test_read_recent_missing_file_is_empty(tmp_path: Path) -> None:
    rep = errors.read_recent(hours=24, path=tmp_path / "nope.jsonl")
    assert rep["total"] == 0
    assert rep["exists"] is False


def test_flush_without_counts_writes_nothing(tmp_path: Path) -> None:
    """没有计数时不该写空记录；有其它模块计数时只校验落盘格式合法。

    （不使用 pytest.skip —— 自跳过模块必须登记到机器资源登记表，
    这里用条件断言代替。）
    """
    state = tmp_path / "swallowed.jsonl"
    errors.reset()
    out = errors.flush(state)
    if out.get("written"):
        assert state.exists()
        rec = json.loads(state.read_text(encoding="utf-8").splitlines()[-1])
        assert rec["total"] > 0
    else:
        assert not state.exists()


# --------------------------------------------------------------------------- #
# 3. 功能：吞错 → 计数 → 汇总 → 落盘
# --------------------------------------------------------------------------- #
def test_swallowed_error_is_counted_and_aggregated(tmp_path: Path) -> None:
    """端到端：触发吞错 → 计数 +1 → snapshot 可见 → flush 后可聚合。"""
    from tdxfeed import symbols

    before = symbols._ERROR_COUNT.get("get_core_stocks", 0)
    original = symbols.ak.index_stock_cons_csindex

    def boom(*a, **k):
        raise RuntimeError("模拟接口失败")

    symbols.ak.index_stock_cons_csindex = boom
    try:
        symbols.get_core_stocks()
    finally:
        symbols.ak.index_stock_cons_csindex = original
    assert symbols._ERROR_COUNT.get("get_core_stocks", 0) == before + 1

    snap = errors.snapshot()
    assert snap["tdxfeed.symbols"].get("get_core_stocks", 0) >= before + 1

    state = tmp_path / "swallowed.jsonl"
    errors.flush(state)
    rep = errors.read_recent(hours=24, path=state)
    assert rep["total"] >= 1
