#!/usr/bin/env python
"""SEM001 批量改造器：把非原子写改成"临时文件 + os.replace"。

改造规则
--------
| 原写法 | 改为 |
|---|---|
| `X.write_text(v, encoding=...)` | `atomic_write_text(X, v, encoding=...)` |
| `X.write_bytes(b)` | `atomic_write_bytes(X, b)` |
| `df.to_csv(path, ...)` | `atomic_write_frame(df, path, ...)` |
| `df.to_parquet(path, ...)` | `atomic_write_parquet(df, path, ...)` |
| `with open(p,"w") as fh: fh.write(v)` | `atomic_write_text(p, v, encoding=...)` |

导入注入
--------
- `tdxfeed/` 包内：直接 `from tdxfeed.atomic import ...`
- 其余目录：若已有仓库根入 `sys.path` 则直接加 import；否则补一段引导
  （`sys.path.insert(0, str(Path(__file__).resolve().parents[N]))`），与仓库既有写法一致。

用法
  venv/bin/python scripts/sem001_migrate.py --paths scripts --dry-run
  venv/bin/python scripts/sem001_migrate.py --paths scripts
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

from tdxfeed.atomic import atomic_write_text  # noqa: E402  # SEM001 原子写

WRITE_TEXT = {"write_text"}
WRITE_BYTES = {"write_bytes"}
TO_CSV = {"to_csv"}
TO_PARQUET = {"to_parquet"}


@dataclass
class Splice:
    """一处源码替换。"""

    lineno: int
    end_lineno: int
    col: int
    end_col: int
    new_text: str


# 守卫自检：解析失败必须计数（不能静默吞掉）
PARSE_FAILS = 0


def _seg(src_lines: list[str], node: ast.AST) -> str:
    return "\n".join(src_lines[node.lineno - 1: node.end_lineno])


def _args_text(node: ast.Call, src_lines: list[str]) -> str:
    """参数列表文本（保留原样，含关键字参数）。"""
    if not node.args and not node.keywords:
        return ""
    parts = [ast.get_source_segment("\n".join(src_lines), a) or "" for a in node.args]
    for kw in node.keywords:
        v = ast.get_source_segment("\n".join(src_lines), kw.value) or ""
        parts.append(f"{kw.arg}={v}" if kw.arg else f"**{v}")
    return ", ".join(parts)


def plan_file(path: Path) -> tuple[list[Splice], set[str]]:
    """返回 (替换计划, 需要的 helper 名集合)。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    global PARSE_FAILS
    try:
        tree = ast.parse(src)
    except SyntaxError:
        PARSE_FAILS += 1
        return [], set()
    flagged = {f.row for f in sg.check_atomic_write(tree, lines)}
    splices: list[Splice] = []
    needed: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno not in flagged:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        recv = ast.get_source_segment("\n".join(lines), func.value) or ""
        if attr in WRITE_TEXT:
            needed.add("atomic_write_text")
            splices.append(Splice(node.lineno, node.end_lineno, node.col_offset,
                                  node.end_col_offset,
                                  f"atomic_write_text({recv}, {_args_text(node, lines)})"))
        elif attr in WRITE_BYTES:
            needed.add("atomic_write_bytes")
            splices.append(Splice(node.lineno, node.end_lineno, node.col_offset,
                                  node.end_col_offset,
                                  f"atomic_write_bytes({recv}, {_args_text(node, lines)})"))
        elif attr in TO_CSV:
            needed.add("atomic_write_frame")
            splices.append(Splice(node.lineno, node.end_lineno, node.col_offset,
                                  node.end_col_offset,
                                  f"atomic_write_frame({recv}, {_args_text(node, lines)})"))
        elif attr in TO_PARQUET:
            needed.add("atomic_write_parquet")
            splices.append(Splice(node.lineno, node.end_lineno, node.col_offset,
                                  node.end_col_offset,
                                  f"atomic_write_parquet({recv}, {_args_text(node, lines)})"))

    # with open(p,"w") as fh: ...  →  写临时路径 + 块后 os.replace 落位
    # （块体原样保留，不做字符串拼接重构 → 风险最低）
    for node in ast.walk(tree):
        if not isinstance(node, ast.With) or node.lineno not in flagged:
            continue
        item = node.items[0]
        ctx = item.context_expr
        if not (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name)
                and ctx.func.id == "open"):
            continue
        if len(ctx.args) < 2 or not isinstance(ctx.args[1], ast.Constant):
            continue
        if "w" not in str(ctx.args[1].value):
            continue
        path_expr = ast.get_source_segment("\n".join(lines), ctx.args[0]) or ""
        if not path_expr:
            continue
        tmp_expr = f'f"{{{path_expr}}}.tmp"'
        needed.add("atomic_write_text")
        needed.add("os")   # 块后要 os.replace 落位
        # 1) open 的目标路径改成临时路径
        splices.append(Splice(ctx.args[0].lineno, ctx.args[0].end_lineno,
                               ctx.args[0].col_offset, ctx.args[0].end_col_offset,
                               tmp_expr))
        # 2) with 块之后（下一行）补 os.replace 落位
        indent = " " * node.col_offset
        splices.append(Splice(node.end_lineno + 1, node.end_lineno + 1, 0, 0,
                               f"{indent}os.replace({tmp_expr}, {path_expr})\n"))
    return splices, needed


def _apply_splices(src: str, splices: list[Splice]) -> str:
    """按位置倒序应用替换（避免行号漂移）。

    注意：CPython 的 AST `col_offset` 是 **UTF-8 字节偏移**，
    直接当字符偏移用会在含中文的行切错 → 整段按字节切片再解码。
    """
    data = src.encode("utf-8")
    lines = src.splitlines()
    offsets: list[int] = []
    acc = 0
    for ln in lines:
        offsets.append(acc)
        acc += len(ln.encode("utf-8")) + 1      # 字节偏移（+1 为换行）
    out = data
    for sp in sorted(splices, key=lambda s: (-s.lineno, -s.col)):
        start = offsets[sp.lineno - 1] + sp.col
        end = offsets[sp.end_lineno - 1] + sp.end_col
        out = out[:start] + sp.new_text.encode("utf-8") + out[end:]
    return out.decode("utf-8")


def _top_level_calls(tree: ast.AST) -> list[ast.Call]:
    """模块顶层（含 if/try 块内）的调用；不下钻函数与类体。"""
    out: list[ast.Call] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            walk(child)

    walk(tree)
    return out


def _is_path_insert(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Attribute)
            and call.func.attr == "insert"
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "path")


def _is_rootish(call: ast.Call) -> bool:
    """参数里是否出现 ROOT / parent / dirname / parents 等仓库根线索。"""
    for arg in list(call.args) + [k.value for k in call.keywords]:
        text = ast.unparse(arg) if arg is not None else ""
        if any(k in text for k in ("ROOT", "parent", "dirname", "parents", "abspath")):
            return True
    return False


def _has_root_on_path(src: str) -> bool:
    """是否已有"仓库根进 sys.path"的顶层写法（AST 判定，不看注释与函数内）。"""
    global PARSE_FAILS
    try:
        tree = ast.parse(src)
    except SyntaxError:
        PARSE_FAILS += 1
        return False
    return any(_is_path_insert(c) and _is_rootish(c) for c in _top_level_calls(tree))


def inject_imports(path: Path, needed: set[str]) -> tuple[list[str], list[str]]:
    """返回 (stdlib 段, 项目段) 两组待插入代码。

    - stdlib 段：`import sys` / `from pathlib import Path`（若缺）→ 必须落在 **标准库
      导入块内**，否则会触发 E402。
    - 项目段：仓库根引导 + `from tdxfeed.atomic import ...` → 必须落在
      **第一个项目导入之前**（或既有 sys.path 引导之后）。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    if not needed:
        return [], []
    global PARSE_FAILS
    try:
        rel = path.relative_to(ROOT)
        depth = len(rel.parts) - 1
    except ValueError:
        PARSE_FAILS += 1
        depth = 1   # 仓库外文件（测试临时目录）：按根下一层估算
    tree = ast.parse(src)
    stdlib_lines: list[str] = []
    proj_lines: list[str] = []

    # 只认**文件开头的连续导入块**：中段才导入的同名模块（如 plot_loss.py 里
    # 第 23 行才 from pathlib import Path）不能算"已可用"——引导代码在它之前。
    lead = _leading_imports(tree)
    has_sys = any(isinstance(n, ast.Import) and any(a.name == "sys" for a in n.names)
                  for n in lead)
    has_path = any(isinstance(n, ast.ImportFrom) and n.module == "pathlib"
                   and any(a.name == "Path" for a in n.names)
                   for n in lead)
    if not _has_root_on_path(src):
        if not has_sys:
            stdlib_lines.append("import sys")
        if not has_path:
            stdlib_lines.append("from pathlib import Path")
        proj_lines.append(
            f"sys.path.insert(0, str(Path(__file__).resolve().parents[{depth}]))"
            "  # 仓库根：让 tdxfeed.atomic 可导入")
    helper_names = sorted(n for n in needed if n != "os")
    if "os" in needed:
        has_os = any(isinstance(n, ast.Import) and any(a.name == "os" for a in n.names)
                     for n in lead)
        if not has_os:
            stdlib_lines.append("import os")
    if helper_names:
        names = ", ".join(helper_names)
        proj_lines.append(
            f"from tdxfeed.atomic import {names}  # noqa: E402  # SEM001 原子写")
    return stdlib_lines, proj_lines


PROJECT_ROOTS = ("tdxfeed", "quant", "mcp", "skills", "agents", "v5", "_common")


def _leading_imports(tree: ast.AST) -> list[ast.stmt]:
    """文件开头的**连续**导入块（遇到第一个非导入语句即停）。

    只认这一段：文件中段散落的 import（如函数内的延迟导入）不属于导入块，
    否则会把插入位置算到文件后半截。
    """
    out: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue      # 模块 docstring
        break
    return out


def _is_project_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").startswith(PROJECT_ROOTS)
    if isinstance(node, ast.Import):
        return any(a.name.startswith(PROJECT_ROOTS) for a in node.names)
    return False


def _stdlib_import_end(tree: ast.AST) -> int:
    """文件开头导入块里最后一个"标准库"导入的结束行（项目内导入不算）。"""
    end = 0
    for node in _leading_imports(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod != "__future__" and not mod.startswith(PROJECT_ROOTS):
                end = max(end, node.end_lineno or node.lineno)
        elif isinstance(node, ast.Import):
            if all(not a.name.startswith(PROJECT_ROOTS) for a in node.names):
                end = max(end, node.end_lineno or node.lineno)
    return end


def _project_insert_pos(tree: ast.AST) -> int:
    """项目段插入位置（行号，1-based，插在该行之前）。

    - 文件已有 sys.path 引导 → 插到最后一次引导之后
    - 否则 → 插到第一个项目导入之前（引导必须先于项目导入）
    """
    last_path = 0
    for call in _top_level_calls(tree):
        if _is_path_insert(call):
            last_path = max(last_path, call.end_lineno or call.lineno)
    if last_path:
        return last_path
    for node in _leading_imports(tree):
        if _is_project_import(node):
            return node.lineno - 1
    return _stdlib_import_end(tree) or 1


def _insert_position(src: str) -> int:
    """插入位置（行号，1-based）。

    必须在 **仓库根进 sys.path 之后** —— 否则 `from tdxfeed.atomic import ...`
    会在导入时直接 ImportError。故：
      - 已有 sys.path.insert → 插到最后一次 sys.path.insert 之后
      - 无 → 插到 docstring / __future__ 之后（并由 inject_imports 补引导）
    """
    tree = ast.parse(src)
    pos = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        pos = body[0].end_lineno or body[0].lineno
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            pos = max(pos, node.end_lineno or node.lineno)
    last_path_insert = 0
    for call in _top_level_calls(tree):
        if _is_path_insert(call):
            last_path_insert = max(last_path_insert, call.end_lineno or call.lineno)
    if last_path_insert:
        pos = max(pos, last_path_insert)
    # 已有 tdxfeed 导入 → 跟在那组后面（isort 顺序：_common 先、tdxfeed 后）
    last_tdx = 0
    for node in body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tdxfeed"):
            last_tdx = max(last_tdx, node.end_lineno or node.lineno)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("tdxfeed"):
                    last_tdx = max(last_tdx, node.end_lineno or node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
    if last_tdx:
        pos = max(pos, last_tdx)
    # stdlib 导入之后（保证 sys / Path 已可用）
    pos = max(pos, _stdlib_import_end(tree))
    return pos


def process_file(path: Path, dry_run: bool) -> tuple[int, set[str]]:
    splices, needed = plan_file(path)
    if not splices:
        return 0, set()
    src = path.read_text(encoding="utf-8", errors="replace")
    out = _apply_splices(src, splices)
    stdlib_extra, proj_extra = inject_imports(path, needed)
    if stdlib_extra or proj_extra:
        out_lines = out.splitlines()
        tree2 = ast.parse(out)
        if stdlib_extra:
            pos = _stdlib_import_end(tree2) or 1
            out_lines[pos:pos] = stdlib_extra
            tree2 = ast.parse("\n".join(out_lines))
        if proj_extra:
            pos = _project_insert_pos(tree2)
            out_lines[pos:pos] = [*proj_extra, ""]
        out = "\n".join(out_lines) + "\n"
    if not dry_run:
        atomic_write_text(path, out, encoding="utf-8")
    return len(splices), needed


def collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = ROOT / pp
        if pp.is_dir():
            files.extend(sorted(pp.rglob("*.py")))
        elif pp.suffix == ".py" and pp.exists():
            files.append(pp)
    skip = {"venv", ".git", "__pycache__", "node_modules", "_archive", "v5",
            "tests", "build", "dist"}
    return [f for f in files if not any(part in skip for part in f.parts)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SEM001 批量改造器")
    ap.add_argument("--paths", nargs="*", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    total = 0
    touched = 0
    for f in collect_files(args.paths):
        n, needed = process_file(f, args.dry_run)
        if n:
            total += n
            touched += 1
            print(f"  {f.relative_to(ROOT)}  {n} 处  {sorted(needed)}")
    print(f"\n待改造 {total} 处，涉及 {touched} 个文件"
          f"{'（dry-run，未写入）' if args.dry_run else '（已写入）'}")
    if PARSE_FAILS:
        print(f"⚠️ 有 {PARSE_FAILS} 个文件语法解析失败，已跳过（异常已计入统计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
