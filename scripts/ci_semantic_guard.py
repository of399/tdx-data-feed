#!/usr/bin/env python
"""M-4：三条 AST 语义守卫。

SEM001 原子写            写文件必须走"临时文件 + os.replace"，或纯追加；否则读者可能读到半截文件
SEM002 吞错必计数        except 分支不得静默吞错：必须 raise，或把失败**计入统计**（递增失败计数）
SEM003 外部调用必带 timeout  网络/子进程调用必须显式设置 timeout，否则可能永久阻塞

与 M-2（`scripts/ci_violation_gate.py`）的关系
  本模块只负责"发现语义问题"，输出统一为 `Finding(file,row,col,code,message)`；
  判定（基线额度 / 只降不升 / 新增行）复用 M-2 的门禁机制，语义规则用独立基线
  `config/semantic_baseline.json`（维度独立 → 首次引入不污染 ruff 维度的"只降不升"）。

用法
  venv/bin/python scripts/ci_semantic_guard.py scan [--paths scripts tdxfeed] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

RULES: dict[str, str] = {
    "SEM001": "原子写",
    "SEM002": "吞错必计数",
    "SEM003": "外部调用必带 timeout",
}

# 临时文件特征：写入临时路径是"先写临时再落位"的合法中间态
TMP_SUFFIXES = (".tmp", ".part", ".new", ".temp", ".writing", ".swp")
TMP_NAME_WORDS = ("tmp", "temp", "part", "stage", "staging", "pending")

# 计数器识别：自增目标的标识符里出现这些词即视为"计入统计"
COUNTER_WORDS = ("count", "cnt", "fail", "failed", "failure", "error", "err",
                 "skip", "skipped", "miss", "retry", "total", "num", "_n")
# 计数函数识别：函数名以这些词开头
COUNT_FN_RE = re.compile(
    r"^(count|inc|incr|increment|record|track|note|bump|stat|counter)\w*$", re.I)

# 外部调用：函数全名匹配即视为外部 I/O
HTTP_PREFIXES = ("requests.", "httpx.", "aiohttp.", "urllib.request.")
HTTP_ATTR_RE = re.compile(r"\.(get|post|put|delete|head|patch|options)$")
HTTP_RECV_HINTS = ("session", "client", "sess", "conn")
SUBPROCESS_FNS = ("run", "call", "check_call", "check_output", "Popen")

NOQA_RE = re.compile(r"noqa:\s*(SEM\d{3})(.*)$")


@dataclass
class Finding:
    """一条语义违规。"""

    file: str
    row: int
    col: int
    code: str
    message: str
    fn: str = ""

    def key(self) -> tuple[str, str]:
        return (self.file, self.code)

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "row": self.row, "col": self.col,
                "code": self.code, "message": self.message, "fn": self.fn}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    global UNPARSE_FAILS
    try:
        return ast.unparse(node)
    except Exception:
        UNPARSE_FAILS += 1
        return getattr(node, "id", "") or type(node).__name__


def last_component(func_text: str) -> str:
    """取 `a.b.c` 的最后一段（函数名）。"""
    if "." not in func_text:
        return func_text
    return func_text.rsplit(".", 1)[-1]


def is_tmp_like(text: str) -> bool:
    """路径表达式是否像临时文件。"""
    low = text.lower()
    if any(low.endswith(sfx) for sfx in TMP_SUFFIXES):
        return True
    return any(w in low for w in TMP_NAME_WORDS)


def suppressed(line_text: str, code: str) -> bool:
    """是否被带理由的 noqa 抑制。

    规则：`# noqa: SEM002` **必须带理由**（括号内容或 `-` 后的文字）才生效；
    无理由的 noqa 视为无效（防止一行注释把守卫整体关掉）。
    """
    m = NOQA_RE.search(line_text)
    if not m or m.group(1) != code:
        return False
    tail = m.group(2).strip()
    if not tail:
        return False
    if tail.startswith(":"):
        tail = tail[1:].strip()
        return bool(tail)
    if tail.startswith("("):
        return bool(tail.strip("() ").strip())
    return False


def counter_target_text(target: ast.AST) -> str:
    return unparse(target).lower()


def is_count_action(stmts: Iterable[ast.stmt]) -> bool:
    """语句列表里是否存在"计入统计"的动作。"""
    for st in stmts:
        for node in ast.walk(st):
            if isinstance(node, ast.AugAssign):
                if not isinstance(node.op, (ast.Add, ast.Sub)):
                    continue
                text = counter_target_text(node.target)
                if any(w in text for w in COUNTER_WORDS):
                    return True
            elif isinstance(node, ast.Call):
                fn = last_component(unparse(node.func))
                if COUNT_FN_RE.match(fn):
                    return True
                # 登记进失败清单也算计入统计（如 failures.append(...)）
                if fn in ("append", "add", "extend") and node.args:
                    recv = unparse(node.func.value).lower()
                    if any(w in recv for w in ("fail", "error", "err", "issue",
                                               "problem", "warn", "reject",
                                               "skip", "count", "stat", "metric")):
                        return True
    return False


# --------------------------------------------------------------------------- #
# 守卫 1 · 原子写（SEM001）
# --------------------------------------------------------------------------- #
@dataclass
class _Write:
    kind: str          # overwrite | append
    path_text: str
    row: int
    col: int
    call: str


@dataclass
class _FuncState:
    writes: list[_Write] = field(default_factory=list)
    reads: set[str] = field(default_factory=set)
    has_rename: bool = False


def _open_mode(node: ast.Call) -> str:
    """取 open() 的模式参数。"""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        return str(node.args[1].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return "r"


def _is_rename_call(node: ast.Call) -> bool:
    text = unparse(node.func)
    if text in ("os.replace", "os.rename", "shutil.move"):
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
        return unparse(node.func.value).startswith("Path")
    return False


def _walk_no_funcs(stmts: list[ast.stmt]) -> Iterable[ast.AST]:
    """遍历语句，但不下钻函数/类定义体（避免同一写入被重复计数）。"""
    stack: list[ast.AST] = list(stmts)
    while stack:
        st = stack.pop()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield st
        for child in ast.iter_child_nodes(st):
            stack.append(child)


def _collect_state(stmts: list[ast.stmt]) -> _FuncState:
    st = _FuncState()
    for node in _walk_no_funcs(stmts):
        if not isinstance(node, ast.Call):
            continue
        text = unparse(node.func)
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("write_text", "write_bytes"):
                st.writes.append(_Write("overwrite", unparse(node.func.value),
                                        node.lineno, node.col_offset, text))
            elif (attr in ("to_csv", "to_parquet", "to_json", "to_excel",
                           "to_pickle", "to_hdf")
                    and node.args
                    and not (isinstance(node.args[0], ast.Constant)
                             and node.args[0].value is None)):
                st.writes.append(_Write("overwrite", unparse(node.args[0]),
                                        node.lineno, node.col_offset, text))
        if (text in ("open", "builtins.open")
                or (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open")) and node.args:
            mode = _open_mode(node)
            path_text = unparse(node.args[0])
            if "w" in mode or "+" in mode:
                st.writes.append(_Write("overwrite", path_text, node.lineno,
                                        node.col_offset, "open(...)"))
            elif "a" in mode:
                st.writes.append(_Write("append", path_text, node.lineno,
                                        node.col_offset, "open(...)"))
            elif "r" in mode:
                st.reads.add(path_text)
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "read_text", "read_bytes", "read_csv", "read_json",
                "read_parquet", "readlines", "read_excel"):
            st.reads.add(unparse(node.func.value))
        if _is_rename_call(node):
            st.has_rename = True
    return st


def check_atomic_write(tree: ast.AST, source_lines: list[str]) -> list[Finding]:
    """SEM001：写文件必须原子（临时文件 + replace）或纯追加。"""
    findings: list[Finding] = []
    bodies: list[tuple[str, list[ast.stmt]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies.append((node.name, node.body))
    bodies.append(("<module>", getattr(tree, "body", [])))

    for fn_name, body in bodies:
        st = _collect_state(body)
        for w in st.writes:
            line = source_lines[w.row - 1] if 0 < w.row <= len(source_lines) else ""
            if suppressed(line, "SEM001"):
                continue
            if is_tmp_like(w.path_text):
                if not st.has_rename:
                    findings.append(Finding(
                        "", w.row, w.col, "SEM001",
                        f"临时文件写完未落位：{w.path_text} 写完未见 os.replace/replace 到最终路径",
                        fn_name))
                continue
            if w.kind == "append":
                # 纯追加是 POSIX O_APPEND 语义（短行写入不撕裂）；只有"先读后追加"
                # 的读-改-写才会丢行
                if w.path_text in st.reads:
                    findings.append(Finding(
                        "", w.row, w.col, "SEM001",
                        f"读-改-写无锁：{w.path_text} 先读后追加，并发下会丢行", fn_name))
                continue
            findings.append(Finding(
                "", w.row, w.col, "SEM001",
                f"非原子写：{w.path_text} 直接覆盖写入，崩溃/并发下读者可能读到半截文件"
                f"（应先写 .tmp 再 os.replace）", fn_name))
    return findings


# --------------------------------------------------------------------------- #
# 守卫 2 · 吞错必计数（SEM002）
# --------------------------------------------------------------------------- #
def _handler_shape(body: list[ast.stmt]) -> str:
    """判定吞错形态，用于给出可区分的提示。"""
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "pass"
    if body and all(isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str) for s in body):
        return "docstring"
    kinds = {type(s).__name__ for s in body}
    if "Continue" in kinds or "Break" in kinds:
        return "continue"
    logs = 0
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if isinstance(node, ast.Call):
            fn = last_component(unparse(node.func)).lower()
            if fn in ("exception", "error", "warning", "info", "debug",
                      "critical", "log", "logger"):
                logs += 1
    if logs:
        return "log"
    return "other"


def _enclosing_fn(tree: ast.AST, target: ast.AST) -> str:
    """找 target 所在的最内层函数名。"""
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
    return best


def check_swallow(tree: ast.AST, source_lines: list[str]) -> list[Finding]:
    """SEM002：except 分支不得静默吞错，必须 raise 或把失败计入统计。"""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        if any(isinstance(n, ast.Raise)
               for n in ast.walk(ast.Module(body=body, type_ignores=[]))):
            continue
        if is_count_action(body):
            continue
        line = source_lines[node.lineno - 1] if 0 < node.lineno <= len(source_lines) else ""
        if suppressed(line, "SEM002"):
            continue
        exc = unparse(node.type) if node.type else "BaseException"
        shape = _handler_shape(body)
        tip = {
            "pass": "except 体为 pass，错误完全不可追溯",
            "docstring": "except 体仅文档字符串，错误完全不可追溯",
            "continue": "except 体为 continue/break，失败被跳过且未计数",
            "log": "except 体仅记录日志，未递增任何失败计数（统计口径缺失）",
            "other": "except 体既未 raise 也未递增失败计数",
        }[shape]
        findings.append(Finding(
            "", node.lineno, node.col_offset, "SEM002",
            f"吞错未计数：{exc} → {tip}（需 raise 或递增失败计数，如 failed_count += 1）",
            _enclosing_fn(tree, node)))
    return findings


# --------------------------------------------------------------------------- #
# 守卫 3 · 外部调用必带 timeout（SEM003）
# --------------------------------------------------------------------------- #
def _target_token(t: ast.AST) -> str:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return unparse(t)
    return ""


def _collect_popen_targets(tree: ast.AST) -> set[str]:
    """收集由 subprocess.Popen 创建的对象名（含 self.attr）。"""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and last_component(unparse(node.value.func)) == "Popen"):
            for t in node.targets:
                tok = _target_token(t)
                if tok:
                    targets.add(tok)
        if (isinstance(node, ast.withitem) and isinstance(node.context_expr, ast.Call)
                and last_component(unparse(node.context_expr.func)) == "Popen"):
            tok = _target_token(node.optional_vars) if node.optional_vars else ""
            if tok:
                targets.add(tok)
    return targets


def _external_kind(func_text: str) -> str:
    """返回外部 I/O 类别，空串表示不是外部调用。"""
    last0 = last_component(func_text)
    if last0 in ("Request", "Session", "Client", "AsyncClient", "HTTPConnection",
                 "Pool", "HTTPAdapter", "Response"):
        return ""          # 只是构造对象，未发生 I/O
    if func_text.startswith(HTTP_PREFIXES):
        return "HTTP"
    if last_component(func_text) == "urlopen":
        return "HTTP"
    last = last_component(func_text)
    if last in SUBPROCESS_FNS and (func_text.startswith("subprocess.")
                                   or func_text.startswith("sp.")):
        return "SUBPROCESS"
    if func_text.startswith("socket.create_connection"):
        return "SOCKET"
    if HTTP_ATTR_RE.search(func_text):
        recv = func_text.rsplit(".", 1)[0].lower()
        if any(h in recv for h in HTTP_RECV_HINTS):
            return "HTTP"
    return ""


def _has_timeout_kw(node: ast.Call) -> bool:
    return any(kw.arg == "timeout" for kw in node.keywords)


def check_timeout(tree: ast.AST, source_lines: list[str]) -> list[Finding]:
    """SEM003：网络/子进程调用必须显式设置 timeout。"""
    findings: list[Finding] = []
    popen_targets = _collect_popen_targets(tree)
    settimeout_targets: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and last_component(unparse(node.func)) == "settimeout"
                and node.args):
            settimeout_targets.add(unparse(node.args[0]))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_text = unparse(node.func)
        line = source_lines[node.lineno - 1] if 0 < node.lineno <= len(source_lines) else ""
        kind = _external_kind(func_text)
        if kind:
            if last_component(func_text) == "Popen":
                pass          # Popen 本身无 timeout 参数，交给管道读规则
            elif not _has_timeout_kw(node):
                if suppressed(line, "SEM003"):
                    continue
                findings.append(Finding(
                    "", node.lineno, node.col_offset, "SEM003",
                    f"外部调用未设 timeout：{func_text}（{kind}）可能永久阻塞"
                    f"，须显式传 timeout=", _enclosing_fn(tree, node)))
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "readline", "read", "readlines", "communicate", "wait"):
            recv = unparse(node.func.value)
            base = recv.rsplit(".", 1)[0] if recv.endswith((".stdout", ".stderr")) else recv
            if (base in popen_targets and base not in settimeout_targets
                    and not _has_timeout_kw(node)
                    and not suppressed(line, "SEM003")):
                findings.append(Finding(
                    "", node.lineno, node.col_offset, "SEM003",
                    f"子进程管道读未设 timeout：{func_text} —— 父进程可能永久阻塞"
                    f"（MCP readline 场景），须 communicate(timeout=) 或独立读线程",
                    _enclosing_fn(tree, node)))
    return findings


# --------------------------------------------------------------------------- #
# 扫描入口
# --------------------------------------------------------------------------- #
CHECKS = (check_atomic_write, check_swallow, check_timeout)

# 守卫自身的失败计数（自检：这些 except 必须计入统计，否则守卫自己会被 SEM002 拦下）
UNPARSE_FAILS = 0
SCAN_SKIPPED = 0
RUFF_LIST_FAILS = 0
PATH_OUTSIDE_FAILS = 0


def scan_file(path: Path, root: Path = ROOT) -> list[Finding]:
    """扫描单个 Python 文件。"""
    global SCAN_SKIPPED
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, ValueError, OSError):
        SCAN_SKIPPED += 1
        return []
    lines = src.splitlines()
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    out: list[Finding] = []
    for check in CHECKS:
        for f in check(tree, lines):
            f.file = rel
            out.append(f)
    return out


def ruff_files(root: Path = ROOT) -> list[Path]:
    """与 ruff 扫描范围保持一致的文件清单。"""
    global RUFF_LIST_FAILS
    try:
        proc = subprocess.run([sys.executable, "-m", "ruff", "check", ".",
                               "--show-files"], capture_output=True, text=True,
                              cwd=str(root), timeout=120)
        if proc.returncode == 0 and proc.stdout.strip():
            return [Path(x) for x in proc.stdout.splitlines() if x.strip()]
    except Exception:
        RUFF_LIST_FAILS += 1
    skip = {"venv", ".git", "node_modules", "__pycache__"}
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in skip for part in p.parts):
            continue
        out.append(p)
    return out


EXCLUDE_DIRS = {"venv", ".git", "node_modules", "__pycache__", "build", "dist",
                "_archive", "v5", "tests"}


def _excluded(path: Path, root: Path) -> bool:
    global PATH_OUTSIDE_FAILS
    try:
        rel = path.relative_to(root)
    except ValueError:
        PATH_OUTSIDE_FAILS += 1
        return False
    return any(part in EXCLUDE_DIRS for part in rel.parts)


def scan_paths(paths: list[str] | None = None, root: Path = ROOT) -> list[Finding]:
    """扫描指定目录。

    默认作用域 = **在役代码**（与 ruff 全范围不同）：
    排除 `_archive/`（已归档）、`v5/`（已冻结的研究线）、`tests/`（测试夹具无并发读者）、
    及其余构建产物目录。
    语义守卫只对在役代码生效——归档代码不再产生新增债务，也不污染基线。
    """
    if not paths:
        files = [f for f in ruff_files(root) if not _excluded(f, root)]
    else:
        files = []
        for p in paths:
            pp = Path(p)
            if not pp.is_absolute():
                pp = root / p
            if pp.is_dir():
                files.extend(sorted(pp.rglob("*.py")))
            elif pp.suffix == ".py":
                files.append(pp)
    findings: list[Finding] = []
    for f in files:
        findings.extend(scan_file(f, root))
    return findings


def summarize(findings: list[Finding]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for f in findings:
        by_rule[f.code] = by_rule.get(f.code, 0) + 1
        by_file[f.file] = by_file.get(f.file, 0) + 1
    return {"total": len(findings), "by_rule": by_rule,
            "files": len(by_file), "by_file": by_file,
            "self_check": {"unparse_fails": UNPARSE_FAILS,
                           "scan_skipped": SCAN_SKIPPED,
                           "ruff_list_fails": RUFF_LIST_FAILS,
                           "path_outside_fails": PATH_OUTSIDE_FAILS}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M-4 语义守卫扫描")
    ap.add_argument("cmd", choices=["scan"], default="scan", nargs="?")
    ap.add_argument("--paths", nargs="*", help="要扫描的目录（默认与 ruff 同范围）")
    ap.add_argument("--rule", help="只看某条规则（如 SEM002）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--limit", type=int, default=15, help="每条规则示例数上限")
    args = ap.parse_args(argv)

    findings = scan_paths(args.paths)
    if args.rule:
        findings = [f for f in findings if f.code == args.rule]
    summary = summarize(findings)

    if args.json:
        print(json.dumps({"summary": summary,
                          "findings": [f.as_dict() for f in findings]},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("M-4 语义守卫扫描")
    print("=" * 72)
    for code, name in RULES.items():
        print(f"  {code} {name:<22} {summary['by_rule'].get(code, 0):>4} 条")
    print(f"  合计 {summary['total']} 条，涉及 {summary['files']} 个文件")
    sc = summary["self_check"]
    if any(sc.values()):
        print(f"  ⚠️ 守卫自检计数（异常被计入统计，未静默）：{sc}")
    print()
    for code in RULES:
        items = [f for f in findings if f.code == code]
        if not items:
            continue
        print(f"── {code} {RULES[code]}（{len(items)} 条，示例 {min(args.limit, len(items))} 条）")
        for f in items[:args.limit]:
            print(f"   {f.file}:{f.row}  {f.message[:96]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
