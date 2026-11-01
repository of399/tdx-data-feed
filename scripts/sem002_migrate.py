#!/usr/bin/env python
"""KI-SILENT-001/002 批量收敛器：把"静默吞错"改成"吞错计入统计"。

改造规则（对每个未 raise、也没有计数动作的 except handler）
-------------------------------------------------------------
1. 在 handler 体**第一行**插入 `_ERROR_COUNT["<函数名>"] += 1`
   （第一行插入 = 无论后面走 return/continue/break 还是别的路径，都会计数）
2. handler 体只有 `pass` → 直接把 `pass` 换成计数语句（语义等价）
3. 文件级补齐：`from collections import Counter` + 模块级 `_ERROR_COUNT: Counter[str] = Counter()`

为什么用"每文件一个 Counter"而不是共享模块
--------------------------------------------
`scripts/` 下部分脚本没有把仓库根放进 `sys.path`，引入项目内模块会在某些
调用方式下 ImportError；`collections` 是标准库，零路径风险。
计数按**函数名**分桶，便于定位"哪个函数在悄悄吞错"。

用法
  venv/bin/python scripts/sem002_migrate.py --paths tdxfeed --dry-run
  venv/bin/python scripts/sem002_migrate.py --paths tdxfeed
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import ci_semantic_guard as sg  # noqa: E402

from tdxfeed.atomic import atomic_write_text  # noqa: E402

# 守卫自检：解析失败必须计数，不能静默吞掉
PARSE_FAILS = 0

COUNTER_NAME = "_ERROR_COUNT"
COUNTER_COMMENT = ("# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，"
                    "按函数名分桶，便于事后追溯失败率")
DEFAULT_SCOPE = ("tdxfeed", "scripts", "mcp", "skill_router", "quant")


@dataclass
class Edit:
    lineno: int          # 1-based，目标行
    text: str            # 插入/替换后的完整行（含缩进）
    replace: bool = False  # True=替换该行；False=在该行前插入


def _enclosing_func(tree: ast.AST, target: ast.AST) -> str:
    best = ""
    best_size = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is target:
                size = sum(1 for _ in ast.walk(node))
                if size > best_size:
                    best_size = size
                    best = node.name
    return best or "<module>"


def _indent_of(line: str) -> str:
    return " " * (len(line) - len(line.lstrip()))


def plan_file(path: Path) -> tuple[list[Edit], int]:
    """分析单文件，返回 (编辑计划, 待收敛处数)。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    global PARSE_FAILS
    try:
        tree = ast.parse(src)
    except SyntaxError:
        PARSE_FAILS += 1
        return [], 0
    lines = src.splitlines()
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    edits: list[Edit] = []
    count = 0
    for h in handlers:
        body = h.body
        if any(isinstance(n, ast.Raise)
               for n in ast.walk(ast.Module(body=body, type_ignores=[]))):
            continue
        if sg.is_count_action(body):
            continue
        count += 1
        label = _enclosing_func(tree, h)
        stmt = f"{COUNTER_NAME}[{label!r}] += 1"
        first = body[0]
        # 一行式：except X: pass
        if len(body) == 1 and isinstance(first, ast.Pass) and first.lineno == h.lineno:
            head = lines[first.lineno - 1].split(":", 1)[0]
            edits.append(Edit(first.lineno, f"{head}:", replace=True))
            edits.append(Edit(first.lineno, f"{_indent_of(lines[first.lineno - 1])}    {stmt}",
                              replace=False))
            continue
        indent = _indent_of(lines[first.lineno - 1])
        if len(body) == 1 and isinstance(first, ast.Pass):
            edits.append(Edit(first.lineno, f"{indent}{stmt}", replace=True))
        else:
            edits.append(Edit(first.lineno, f"{indent}{stmt}", replace=False))
    return edits, count


def module_bootstrap_edits(src: str) -> list[Edit]:
    """补齐 Counter 导入与模块级计数器。"""
    edits: list[Edit] = []
    lines = src.splitlines()
    tree0 = ast.parse(src)
    has_toplevel = any(
        isinstance(n, ast.ImportFrom) and n.module == "collections"
        and any(a.name == "Counter" for a in n.names)
        for n in tree0.body)
    if not has_toplevel:
        idx = 0
        found_future = False
        for i, line in enumerate(lines):
            if line.startswith("from __future__"):
                idx = i + 1
                found_future = True
                break
        if not found_future:
            for i, line in enumerate(lines):
                if line.startswith(("import ", "from ")):
                    idx = i
                    break

        edits.append(Edit(idx + 1, "from collections import Counter", replace=False))
    if f"{COUNTER_NAME}: Counter[str] = Counter()" not in src:
        tree = ast.parse(src)
        last_import = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import = max(last_import, node.end_lineno or node.lineno)
        if last_import == 0:
            # 没有顶层 import → 插到模块 docstring 之后（不能插到 docstring 前面）
            doc = ast.get_docstring(tree, clean=False)
            body = tree.body
            last_import = 0
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                last_import = body[0].end_lineno or body[0].lineno
            _ = doc
        edits.append(Edit(last_import + 1, COUNTER_COMMENT, replace=False))
        edits.append(Edit(last_import + 1, f"{COUNTER_NAME}: Counter[str] = Counter()", replace=False))
    return edits


def apply_edits(path: Path, edits: list[Edit]) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    by_line: dict[int, list[Edit]] = {}
    for e in edits:
        by_line.setdefault(e.lineno, []).append(e)
    out: list[str] = []
    for i, line in enumerate(lines, start=1):
        items = by_line.get(i)
        if items:
            for e in items:
                if e.replace or e.text:
                    out.append(e.text)
        out.append(line)
    atomic_write_text(path, "\n".join(out) + "\n", encoding="utf-8")


def collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = ROOT / pp
        if pp.is_dir():
            files.extend(sorted(pp.rglob("*.py")))
        elif pp.suffix == ".py":
            files.append(pp)
    skip = {"venv", ".git", "__pycache__", "node_modules", "_archive", "v5",
            "tests", "build", "dist"}
    return [f for f in files if not any(part in skip for part in f.parts)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SEM002 批量收敛器")
    ap.add_argument("--paths", nargs="*", default=list(DEFAULT_SCOPE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    files = collect_files(args.paths)
    total = 0
    touched = 0
    for f in files:
        edits, count = plan_file(f)
        if not count:
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        edits += module_bootstrap_edits(src)
        total += count
        touched += 1
        print(f"  {f.relative_to(ROOT)}  {count} 处")
        if not args.dry_run:
            apply_edits(f, edits)
    print(f"\n待收敛 {total} 处，涉及 {touched} 个文件"
          f"{'（dry-run，未写入）' if args.dry_run else '（已写入）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
