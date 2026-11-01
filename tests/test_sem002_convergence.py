"""KI-SILENT-001 / KI-SILENT-002 批量收敛的批次回归测试。

关联台账条目：KI-SILENT-001（盲吞错 except Exception）、KI-SILENT-002（except 分支直接 pass）。

三个层次：
  1. 结构断言：全仓在役代码 **0 条 SEM002**（349 → 0 的收敛成果不再回退）
  2. 一致性断言：每个被改造的文件都声明了模块级 Counter（不会出现"计数变量未定义"）
  3. 功能断言：真实触发一次吞错路径，验证计数**确实递增**（不是摆设）
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ci_semantic_guard as sg  # noqa: E402

SCOPES = ("tdxfeed", "scripts", "mcp", "skill_router", "quant")
CONVERGED_DIRS = ("tdxfeed", "scripts", "mcp", "skill_router", "quant")


def _all_converged_files() -> list[Path]:
    files: list[Path] = []
    for d in CONVERGED_DIRS:
        files.extend(sorted((ROOT / d).rglob("*.py")))
    files.append(ROOT / "conftest.py")
    return [f for f in files if "__pycache__" not in f.parts]


def test_no_sem002_left_in_production_code() -> None:
    """在役代码不得再有"吞错未计数"。"""
    findings = sg.scan_paths(list(SCOPES))
    sem2 = [f for f in findings if f.code == "SEM002"]
    assert not sem2, "仍有吞错未计入统计：\n" + "\n".join(
        f"  {f.file}:{f.row} {f.message[:80]}" for f in sem2[:10])


def test_every_counter_file_has_module_level_counter_import() -> None:
    """用了 _ERROR_COUNT 的文件必须在模块级导入 Counter（否则运行即 NameError）。"""
    bad: list[str] = []
    for f in _all_converged_files():
        src = f.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            bad.append(f"{f.relative_to(ROOT)}: 语法错误")
            continue
        # 以 AST 判定"是否真的声明了模块级计数器"（避免把字符串字面量误判）
        declares = any(
            isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_ERROR_COUNT" for t in n.targets)
            for n in tree.body)
        if not declares:
            continue
        ok = any(isinstance(n, ast.ImportFrom) and n.module == "collections"
                 and any(a.name == "Counter" for a in n.names) for n in tree.body)
        if not ok:
            bad.append(f"{f.relative_to(ROOT)}: 缺模块级 Counter 导入")
    assert not bad, "\n".join(bad)


def test_counter_labels_match_actual_functions() -> None:
    """计数键是真实存在的函数名（避免拼错导致统计永远为 0）。"""
    bad: list[str] = []
    for f in _all_converged_files():
        src = f.read_text(encoding="utf-8", errors="replace")
        if "_ERROR_COUNT" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        funcs = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        funcs.add("<module>")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "_ERROR_COUNT"
                    and isinstance(node.slice, ast.Constant)):
                label = node.slice.value
                if isinstance(label, str) and label not in funcs:
                    bad.append(f"{f.relative_to(ROOT)}: 计数键 {label!r} 不对应任何函数")
    assert not bad, "\n".join(bad[:10])


def test_swallowed_error_is_actually_counted() -> None:
    """功能断言：真实触发一次吞错 → 计数递增（证明不是摆设）。"""
    from tdxfeed import symbols

    before = symbols._ERROR_COUNT.get("get_core_stocks", 0)
    original = symbols.ak.index_stock_cons_csindex

    def boom(*a, **k):
        raise RuntimeError("模拟接口失败")

    symbols.ak.index_stock_cons_csindex = boom
    try:
        result = symbols.get_core_stocks()
    finally:
        symbols.ak.index_stock_cons_csindex = original
    assert result, "失败路径应回退到内置样例，而不是崩溃"
    assert symbols._ERROR_COUNT.get("get_core_stocks", 0) == before + 1, (
        "吞错未被计入统计 —— 计数机制失效")


def test_convergence_progress_is_recorded() -> None:
    """收敛规模被台账与基线记录（防止数字漂移无人知晓）。"""
    import json
    base = json.loads((ROOT / "config" / "semantic_baseline.json").read_text(
        encoding="utf-8"))
    assert base["by_rule"].get("SEM002", 0) == 0, (
        f"语义基线里仍有 SEM002 额度 {base['by_rule'].get('SEM002')} 条")
