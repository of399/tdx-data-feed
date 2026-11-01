"""M-5（修复必须附带测试）机制本身的测试，含反例。

关联台账条目：KI-RULE-004（修复可以不带测试，无强制闭环）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "audit_fix_tests_t", ROOT / "scripts" / "audit_fix_tests.py")
assert spec is not None and spec.loader is not None
aft = importlib.util.module_from_spec(spec)
sys.modules["audit_fix_tests_t"] = aft
spec.loader.exec_module(aft)


# --------------------------------------------------------------------------- #
# M5-R3：改了生产代码必须同时改测试
# --------------------------------------------------------------------------- #
def test_prod_change_without_tests_is_rejected() -> None:
    problems = aft.diff_problems(["scripts/foo.py"], base="HEAD~1", staged=True)
    assert any(p.rule == "M5-R3" for p in problems)


def test_prod_change_with_tests_is_accepted() -> None:
    problems = aft.diff_problems(
        ["scripts/foo.py", "tests/test_foo.py"], base="HEAD~1", staged=True)
    assert not any(p.rule == "M5-R3" for p in problems)


def test_docs_only_change_is_exempt() -> None:
    problems = aft.diff_problems(["docs/WORKFLOW.md"], base="HEAD~1", staged=True)
    assert not problems


def test_ledger_only_change_is_exempt() -> None:
    problems = aft.diff_problems(["config/known_issues.yaml"], base="HEAD~1",
                                 staged=True)
    assert not problems


def test_test_only_change_is_exempt() -> None:
    problems = aft.diff_problems(["tests/test_x.py"], base="HEAD~1", staged=True)
    assert not problems


# --------------------------------------------------------------------------- #
# M5-R1 / M5-R2：台账 FIXED 条目必须绑定可验证测试
# --------------------------------------------------------------------------- #
def test_ledger_fixed_entries_bind_tests() -> None:
    problems = aft.ledger_problems()
    assert not problems, f"台账 FIXED 条目未满足 M-5：{[p.line() for p in problems]}"


def test_missing_test_field_is_detected(monkeypatch) -> None:
    """造一条 FIXED 但没绑测试的条目 → 必须报 M5-R1。"""
    import yaml

    data = yaml.safe_load((ROOT / "config" / "known_issues.yaml").read_text(
        encoding="utf-8"))
    data["issues"].append({
        "id": "KI-TEST-999", "title": "x", "theme": "rule", "severity": "P2",
        "status": "FIXED", "first_seen": "2026-11-01", "last_seen": "2026-11-01",
        "seen_count": 1, "sources": [], "locations": [], "detect": [],
        "impact": "", "guard": "scripts/ci_semantic_guard.py", "owner": "x",
        "due": "2026-11-01", "exemption": "NONE", "fix_hint": "x",
    })
    tmp = ROOT / "config" / "_known_issues_tmp.yaml"
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(aft, "LEDGER_PATH", tmp)
    try:
        problems = aft.ledger_problems()
        assert any(p.rule == "M5-R1" and "KI-TEST-999" in p.detail for p in problems)
    finally:
        tmp.unlink(missing_ok=True)


def test_missing_marker_is_detected(monkeypatch) -> None:
    """测试文件里没有条目 ID → M5-R2。"""
    import yaml

    data = yaml.safe_load((ROOT / "config" / "known_issues.yaml").read_text(
        encoding="utf-8"))
    for iss in data["issues"]:
        if iss["id"] == "KI-RULE-002":
            iss["test"] = "tests/test_atomic_write_helper.py"
    tmp = ROOT / "config" / "_known_issues_tmp.yaml"
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(aft, "LEDGER_PATH", tmp)
    try:
        problems = aft.ledger_problems()
        assert any(p.rule == "M5-R2" and "KI-RULE-002" in p.detail for p in problems)
    finally:
        tmp.unlink(missing_ok=True)
