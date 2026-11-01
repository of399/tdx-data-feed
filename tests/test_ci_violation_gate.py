"""增量阻断门禁的守卫测试。

覆盖：diff 解析 / A1 净增长 / A2 新增行 / A3 基线完整性 / 基线更新规则 / 真实基线快照自检。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci_violation_gate as gate  # noqa: E402

BASELINE = ROOT / "config" / "ruff_baseline.json"


def _v(file: str, row: int, code: str, col: int = 1) -> gate.Violation:
    return gate.Violation(file=file, row=row, col=col, code=code, message="msg")


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# ---------------- 1. diff 解析 ---------------- #
def test_parse_added_lines() -> None:
    # -U0 差异：无上下文行，hunk 头部直接给出新增行区间
    diff = (
        "diff --git a/scripts/x.py b/scripts/x.py\n"
        "--- a/scripts/x.py\n"
        "+++ b/scripts/x.py\n"
        "@@ -10,0 +11,3 @@ def f():\n"
        "+    added1\n"
        "+    added2\n"
        "+    added3\n"
        "@@ -30,0 +33,1 @@\n"
        "+    added4\n"
    )
    assert gate.parse_added_lines(diff)["scripts/x.py"] == {11, 12, 13, 33}


def test_parse_added_lines_ignores_deleted_file() -> None:
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-gone\n")
    assert gate.parse_added_lines(diff) == {}


# ---------------- 2. A1 净增长 ---------------- #
def test_a1_flags_increase() -> None:
    fails = gate.check_a1_net_growth({("a.py", "E402"): 2}, {("a.py", "E402"): 1})
    assert len(fails) == 1 and "A1" in fails[0].assertion


def test_a1_allows_equal_and_decrease() -> None:
    q = {("a.py", "E402"): 2}
    assert gate.check_a1_net_growth({("a.py", "E402"): 2}, q) == []
    assert gate.check_a1_net_growth({("a.py", "E402"): 1}, q) == []
    assert gate.check_a1_net_growth({}, q) == []


def test_a1_new_file_is_rejected() -> None:
    assert len(gate.check_a1_net_growth({("new.py", "F401"): 1}, {})) == 1


# ---------------- 3. A2 新增行 ---------------- #
def test_a2_flags_violation_on_added_line_in_clean_file() -> None:
    fails = gate.check_a2_added_lines([_v("new.py", 3, "F401")], {}, {"new.py": {3}})
    assert len(fails) == 1 and "A2" in fails[0].assertion


def test_a2_ignores_violation_on_untouched_line() -> None:
    viols = [_v("old.py", 100, "F401")]
    assert gate.check_a2_added_lines(viols, {}, {"old.py": {3, 4, 5}}) == []


def test_a2_quota_limits_dirty_file() -> None:
    viols = [_v("dirty.py", 10, "E402")]
    added = {"dirty.py": {10}}
    assert gate.check_a2_added_lines(viols, {("dirty.py", "E402"): 1}, added) == []
    assert len(gate.check_a2_added_lines(viols, {("dirty.py", "E402"): 0}, added)) == 1


# ---------------- 4. A3 基线完整性 ---------------- #
def test_a3_flags_baseline_below_measured() -> None:
    b = gate.Baseline({"total": 10, "by_file_rule": {}})
    fails = gate.check_a3_baseline_integrity(b, 12)
    assert len(fails) == 1 and "A3" in fails[0].assertion


def test_a3_flags_expired_growth_approval() -> None:
    b = gate.Baseline({"total": 10, "by_file_rule": {}, "growth_approved": {
        "reason": "x", "approved_at": "2026-01-01",
        "expires": (date.today() - timedelta(days=1)).isoformat()}})
    fails = gate.check_a3_baseline_integrity(b, 10)
    assert len(fails) == 1 and "到期" in fails[0].detail


def test_a3_accepts_valid_growth_approval() -> None:
    b = gate.Baseline({"total": 10, "by_file_rule": {}, "growth_approved": {
        "reason": "x", "approved_at": date.today().isoformat(),
        "expires": (date.today() + timedelta(days=10)).isoformat()}})
    assert gate.check_a3_baseline_integrity(b, 10) == []


# ---------------- 5. 基线更新规则 ---------------- #
def _update_args(base, sem_base, **kw):
    """cmd_update 的参数集（双维度）。"""
    d = {"baseline": str(base), "semantic_baseline": str(sem_base), "source": "all",
         "semantic_paths": None, "allow_growth": False, "reason": "", "ruff_cmd": None}
    d.update(kw)
    return _ns(**d)


def _patch_collect(monkeypatch, violations):
    monkeypatch.setattr(gate, "collect_violations",
                        lambda *a, **k: list(violations))


def test_update_refuses_growth_without_flag(tmp_path, monkeypatch) -> None:
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 1, "by_file_rule": {"a.py": {"E402": 1}}}).save(base)
    _patch_collect(monkeypatch, [_v("a.py", 1, "E402"), _v("a.py", 2, "E402")])
    assert gate.cmd_update(_update_args(base, sem)) == 1
    assert json.loads(base.read_text())["total"] == 1


def test_update_allows_shrink(tmp_path, monkeypatch) -> None:
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 2, "by_file_rule": {"a.py": {"E402": 2}}}).save(base)
    _patch_collect(monkeypatch, [_v("a.py", 1, "E402")])
    assert gate.cmd_update(_update_args(base, sem)) == 0
    data = json.loads(base.read_text())
    assert data["total"] == 1 and data["by_file_rule"]["a.py"]["E402"] == 1


def test_update_growth_writes_expiring_approval(tmp_path, monkeypatch) -> None:
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 1, "by_file_rule": {"a.py": {"E402": 1}}}).save(base)
    _patch_collect(monkeypatch, [_v("a.py", 1, "E402"), _v("a.py", 2, "E402")])
    assert gate.cmd_update(_update_args(base, sem, allow_growth=True,
                                        reason="紧急修复引入")) == 0
    data = json.loads(base.read_text())
    assert data["growth_approved"]["reason"] == "紧急修复引入"
    assert date.fromisoformat(data["growth_approved"]["expires"]) == (
        date.today() + timedelta(days=gate.GROWTH_APPROVAL_MAX_DAYS))


def test_update_growth_requires_reason(tmp_path, monkeypatch) -> None:
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 1, "by_file_rule": {"a.py": {"E402": 1}}}).save(base)
    _patch_collect(monkeypatch, [_v("a.py", 1, "E402"), _v("a.py", 2, "E402")])
    assert gate.cmd_update(_update_args(base, sem, allow_growth=True)) == 2


def test_dimensions_are_independent(tmp_path, monkeypatch) -> None:
    """语义维度可独立 bootstrap，不影响 ruff 维度的只降不升。"""
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 1, "by_file_rule": {"a.py": {"E402": 1}}}).save(base)
    _patch_collect(monkeypatch, [_v("a.py", 1, "E402"), _v("b.py", 3, "SEM002")])
    # ruff 维度持平（允许），语义维度首次创建（bootstrap）
    assert gate.cmd_update(_update_args(base, sem)) == 0
    assert json.loads(sem.read_text())["total"] == 1
    assert json.loads(base.read_text())["total"] == 1


def test_semantic_growth_is_refused_independently(tmp_path, monkeypatch) -> None:
    """语义维度出现净增长 → 同样拒绝（不放宽"只降不升"）。"""
    base = tmp_path / "b.json"
    sem = tmp_path / "s.json"
    gate.Baseline({"total": 0, "by_file_rule": {}}).save(base)
    gate.Baseline({"total": 1, "by_file_rule": {"b.py": {"SEM002": 1}}}).save(sem)
    _patch_collect(monkeypatch, [_v("b.py", 3, "SEM002"), _v("b.py", 4, "SEM002")])
    assert gate.cmd_update(_update_args(base, sem)) == 1
    assert json.loads(sem.read_text())["total"] == 1


# ---------------- 6. 真实基线快照 ---------------- #
def test_repo_baseline_is_wellformed() -> None:
    assert BASELINE.exists(), "基线快照缺失"
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data["version"] >= 1
    assert isinstance(data["by_file_rule"], dict) and data["by_file_rule"]
    for fname, rules in data["by_file_rule"].items():
        assert isinstance(rules, dict) and rules, fname
        for rule, cnt in rules.items():
            assert isinstance(cnt, int) and cnt >= 1, f"{fname}/{rule}"
    assert data["total"] == sum(sum(r.values()) for r in data["by_file_rule"].values())


def test_repo_baseline_not_below_measured() -> None:
    viols = gate.run_ruff()
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data["total"] >= len(viols), (
        f"基线 {data['total']} < 实测 {len(viols)}：存在未登记的新违规")
