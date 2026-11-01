"""M-6 mypy 增量阻断门禁的测试。

关联台账条目：KI-RULE-005（mypy 只在 v5/ 上跑且不阻断，新增模块无类型门禁）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "ci_typecheck_gate_t", ROOT / "scripts" / "ci_typecheck_gate.py")
assert spec is not None and spec.loader is not None
ctg = importlib.util.module_from_spec(spec)
sys.modules["ci_typecheck_gate_t"] = ctg
spec.loader.exec_module(ctg)


def test_error_line_is_parsed() -> None:
    line = 'skill_router/runtime.py:906: error: "None" has no attribute "x"  [attr-defined]'
    m = ctg.ERR_RE.match(line)
    assert m is not None
    assert m.group("file") == "skill_router/runtime.py"
    assert m.group("code") == "attr-defined"
    assert "None" in m.group("msg")


def test_non_error_lines_ignored() -> None:
    assert ctg.ERR_RE.match("Found 70 errors in 12 files") is None
    assert ctg.ERR_RE.match("note: Common resolutions include:") is None


def test_quotas_aggregate_by_file_and_code() -> None:
    errors = [("a.py", "attr-defined", "x"), ("a.py", "attr-defined", "y"),
              ("b.py", "operator", "z")]
    q = ctg.quotas_from(errors)
    assert q == {"a.py": {"attr-defined": 2}, "b.py": {"operator": 1}}


def test_mypy_baseline_is_wellformed() -> None:
    data = json.loads(ctg.BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["version"] >= 1
    assert list(data["targets"]) == list(ctg.TARGETS)
    total = sum(sum(c.values()) for c in data["by_file_code"].values())
    assert total == data["total"], "基线 total 与 by_file_code 不自洽"


def test_mypy_baseline_not_below_measured() -> None:
    """基线不得低于实测（否则存在未登记的新类型错误）。"""
    errors = ctg.run_mypy()
    data = json.loads(ctg.BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["total"] >= len(errors), (
        f"mypy 基线 {data['total']} < 实测 {len(errors)}")
