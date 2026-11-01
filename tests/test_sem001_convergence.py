"""SEM001（原子写）收敛的批次回归测试 + 改造器单测。

关联台账条目：KI-ATOMIC-001/002/003/004（原子写），KI-MCP-001（外部调用超时）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ci_semantic_guard as sg  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "sem001_migrate_t", ROOT / "scripts" / "sem001_migrate.py")
assert spec is not None and spec.loader is not None
mig = importlib.util.module_from_spec(spec)
sys.modules["sem001_migrate_t"] = mig
spec.loader.exec_module(mig)

SEM_BASELINE = ROOT / "config" / "semantic_baseline.json"
# 与门禁一致的作用域：以 ruff 的文件清单为准（自动排除 _archive / v5 / tests 等）
SCOPES: list[str] = []


# --------------------------------------------------------------------------- #
# 1. 收敛结果
# --------------------------------------------------------------------------- #
def test_no_sem001_left_in_production_code() -> None:
    findings = sg.scan_paths(list(SCOPES))
    sem1 = [f for f in findings if f.code == "SEM001"]
    assert not sem1, "\n".join(f"  {f.file}:{f.row} {f.message[:70]}" for f in sem1[:10])


def test_no_sem003_left_in_production_code() -> None:
    findings = sg.scan_paths(list(SCOPES))
    sem3 = [f for f in findings if f.code == "SEM003"]
    assert not sem3, "\n".join(f"  {f.file}:{f.row} {f.message[:70]}" for f in sem3[:10])


def test_semantic_baseline_is_fully_cleared() -> None:
    """语义维度基线必须为 0（否则门禁仍承认存量）。"""
    data = json.loads(SEM_BASELINE.read_text(encoding="utf-8"))
    assert data["total"] == 0, f"语义基线仍有 {data['total']} 条额度"
    assert not data["by_file_rule"]


def test_atomic_writes_use_helper() -> None:
    """改造后的文件必须真的走 tdxfeed.atomic（而不是只把守卫绕过去）。"""
    hits = 0
    for d in ("tdxfeed", "scripts", "mcp", "skill_router", "quant", "training"):
        for f in (ROOT / d).rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            if "atomic_write_" in src:
                hits += 1
                assert "from tdxfeed.atomic import" in src, (
                    f"{f.relative_to(ROOT)} 用了 helper 却没导入")
    assert hits >= 50, f"只有 {hits} 个文件用了原子写助手，改造可能没生效"


def test_subprocess_calls_have_timeout() -> None:
    """生产代码里的 subprocess.run 必须带 timeout。"""
    bad: list[str] = []
    for d in ("tdxfeed", "scripts", "mcp", "skill_router", "quant", "training"):
        for f in (ROOT / d).rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("run", "call", "check_output")
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and not any(kw.arg == "timeout" for kw in node.keywords)):
                    bad.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    assert not bad, "缺 timeout 的 subprocess 调用：\n" + "\n".join(bad[:10])


# --------------------------------------------------------------------------- #
# 2. 改造器单测
# --------------------------------------------------------------------------- #
VIOLATING = '''import json
from pathlib import Path


def dump(path):
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")


def dump_frame(df, path):
    df.to_csv(path, index=False, encoding="utf-8-sig")
'''

COMPLIANT = '''from pathlib import Path

from tdxfeed.atomic import atomic_write_text


def dump(path):
    atomic_write_text(path, "x")
'''


def _write(tmp_path: Path, src: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(src, encoding="utf-8")
    return f


def test_planner_detects_writes(tmp_path: Path) -> None:
    splices, needed = mig.plan_file(_write(tmp_path, VIOLATING))
    assert len(splices) == 2
    assert needed == {"atomic_write_text", "atomic_write_frame"}


def test_planner_ignores_atomic_code(tmp_path: Path) -> None:
    splices, needed = mig.plan_file(_write(tmp_path, COMPLIANT))
    assert not splices
    assert not needed


def test_migrate_makes_file_sem001_clean(tmp_path: Path) -> None:
    f = _write(tmp_path, VIOLATING)
    n, _needed = mig.process_file(f, dry_run=False)
    assert n == 2
    out = f.read_text(encoding="utf-8")
    tree = ast.parse(out)
    findings = sg.check_atomic_write(tree, out.splitlines())
    assert not findings, [x.message for x in findings]


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    f = _write(tmp_path, VIOLATING)
    mig.process_file(f, dry_run=False)
    splices2, needed2 = mig.plan_file(f)
    assert not splices2, "改造后再跑仍有待改写点 → 不幂等"
    assert not needed2


def test_splice_handles_utf8_byte_offsets(tmp_path: Path) -> None:
    """AST 的 col_offset 是 UTF-8 字节偏移 —— 含中文的行不能被切错。"""
    src = ('from pathlib import Path\n\n\n'
           'def w(p):\n'
           '    p.write_text("中文内容", encoding="utf-8")\n')
    f = _write(tmp_path, src)
    splices, _needed = mig.plan_file(f)
    assert splices, "应识别出 write_text"
    out = mig._apply_splices(src, splices)
    ast.parse(out)                      # 语法必须正确
    assert "中文内容" in out             # 内容不能丢
    assert "atomic_write_text" in out
