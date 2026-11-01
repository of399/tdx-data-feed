"""SEM002 批量收敛器 scripts/sem002_migrate.py 的单元测试（含反例）。

关联台账条目：KI-SILENT-001 / KI-SILENT-002（吞错未计数）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_semantic_guard as sg  # noqa: E402
import sem002_migrate as m  # noqa: E402

VIOLATING = '''import json


def load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        pass


def loop(paths):
    for p in paths:
        try:
            open(p)
        except OSError:
            continue
'''

COMPLIANT = '''def load(path):
    try:
        return path.read_text()
    except OSError:
        raise
'''


def _write(tmp_path: Path, src: str) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(src, encoding="utf-8")
    return f


def test_plan_detects_violating_handlers(tmp_path: Path) -> None:
    f = _write(tmp_path, VIOLATING)
    edits, count = m.plan_file(f)
    assert count == 2
    assert edits


def test_plan_ignores_compliant_handlers(tmp_path: Path) -> None:
    f = _write(tmp_path, COMPLIANT)
    _edits, count = m.plan_file(f)
    assert count == 0


def test_migrate_makes_file_sem002_clean(tmp_path: Path) -> None:
    """改造后：语法正确 + 守卫不再报 SEM002 + 计数确实在代码里。"""
    f = _write(tmp_path, VIOLATING)
    edits, count = m.plan_file(f)
    assert count == 2
    edits += m.module_bootstrap_edits(f.read_text(encoding="utf-8"))
    m.apply_edits(f, edits)

    src = f.read_text(encoding="utf-8")
    ast.parse(src)                      # 语法必须正确
    tree = ast.parse(src)
    findings = sg.check_swallow(tree, src.splitlines())
    assert not findings, f"改造后仍有 SEM002：{[x.message for x in findings]}"
    assert "_ERROR_COUNT" in src
    assert "from collections import Counter" in src


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """已改造的文件再跑一次不应产生新编辑。"""
    f = _write(tmp_path, VIOLATING)
    edits, _ = m.plan_file(f)
    edits += m.module_bootstrap_edits(f.read_text(encoding="utf-8"))
    m.apply_edits(f, edits)
    edits2, count2 = m.plan_file(f)
    assert count2 == 0
    assert not edits2


def test_counter_label_matches_function(tmp_path: Path) -> None:
    f = _write(tmp_path, VIOLATING)
    edits, _ = m.plan_file(f)
    labels = [e.text for e in edits if "_ERROR_COUNT" in e.text]
    assert any("'load'" in x for x in labels)
    assert any("'loop'" in x for x in labels)


def test_module_bootstrap_not_duplicated(tmp_path: Path) -> None:
    f = _write(tmp_path, VIOLATING)
    edits = m.module_bootstrap_edits(f.read_text(encoding="utf-8"))
    assert sum(1 for e in edits
               if e.text.strip() == "from collections import Counter") == 1
    assert sum(1 for e in edits
               if e.text.strip() == "_ERROR_COUNT: Counter[str] = Counter()") == 1
