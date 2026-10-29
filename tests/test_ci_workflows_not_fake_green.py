"""CI 里「跑完永远绿」的步骤必须拆掉：导入失败要红，扫描失败要红。

守卫 quick-sop 的 smoke 步骤（曾用 try/except 只 print）与 docker-publish 的
Trivy 扫描（曾 continue-on-error）。lint.yml 的完整规则集 continue-on-error
是刻意分层（阻断项另跑），不在此列。
"""
from __future__ import annotations

from pathlib import Path

import yaml

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]


def _steps(workflow: str) -> dict[str, dict]:
    data = yaml.safe_load(
        (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    )
    steps: dict[str, dict] = {}
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            steps[step.get("name", "")] = step
    return steps


def test_quick_sop_smoke_step_does_not_swallow_import_errors():
    run = _steps("quick-sop.yml")["业务模块可导入性 smoke test"]["run"]
    assert "except Exception" not in run
    assert "|| true" not in run


def test_quick_sop_runs_tests_not_only_collects():
    pytest_runs = [s.get("run", "") for s in _steps("quick-sop.yml").values()
                   if "pytest" in s.get("run", "")]
    assert pytest_runs, "quick-sop 里应有 pytest 步骤"
    for run in pytest_runs:
        assert "--collect-only" not in run


def test_docker_trivy_scan_is_blocking():
    step = _steps("docker-publish.yml")["Scan image (Trivy)"]
    assert step.get("continue-on-error") is not True
    assert str(step["with"].get("exit-code", "1")) == "1"
