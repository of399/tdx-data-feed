"""已知问题台账的守卫测试。

保证：
  1. 台账文件存在且 YAML 可解析，结构符合约定（必填字段 / id 唯一 / 状态与严重度取值合法）
  2. status=FIXED 的条目必须绑定真实存在的 guard（R5）；ACCEPTED 必须有 exemption + due（R6）
  3. 每条 detect 规格的 kind 合法，且必要参数齐全
  4. 复扫工具能判定"仍存在 / 已消失"两种状态（detect 语义正确）
  5. 未登记模式能被 probe 识别为 NEW（保证"新增"路径可用，不只是"复现"路径）
  6. guard 指向的测试文件必须真实存在
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

LEDGER = ROOT / "config" / "known_issues.yaml"

REQUIRED = (
    "id", "title", "theme", "severity", "status", "first_seen", "last_seen",
    "seen_count", "sources", "locations", "detect", "impact", "guard",
    "owner", "due", "exemption", "fix_hint",
)
VALID_STATUS = {"OPEN", "ACCEPTED", "FIXED", "DEFERRED", "ARCHIVED"}
VALID_SEVERITY = {"P0", "P1", "P2"}
VALID_KINDS = {"grep", "absent", "lines_gt", "glob_exists", "glob_absent"}


@pytest.fixture(scope="module")
def ledger() -> dict:
    assert LEDGER.exists(), f"台账缺失：{LEDGER}"
    data = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("issues"), "台账为空或结构不对"
    return data


def test_ledger_schema(ledger: dict) -> None:
    ids = [i["id"] for i in ledger["issues"]]
    assert len(ids) == len(set(ids)), "id 重复"
    for iss in ledger["issues"]:
        missing = [f for f in REQUIRED if f not in iss]
        assert not missing, f"{iss.get('id')} 缺字段 {missing}"
        assert iss["severity"] in VALID_SEVERITY, iss["id"]
        assert iss["status"] in VALID_STATUS, iss["id"]
        assert iss["seen_count"] >= 1, iss["id"]
        assert iss["first_seen"] <= iss["last_seen"], iss["id"]


def test_fixed_entries_have_guard(ledger: dict) -> None:
    for iss in ledger["issues"]:
        if iss["status"] == "FIXED":
            assert iss["guard"] not in ("NONE", ""), f"{iss['id']} FIXED 但无 guard（R5）"
            assert (ROOT / iss["guard"]).exists(), f"{iss['id']} guard 文件不存在：{iss['guard']}"


def test_accepted_entries_have_exemption(ledger: dict) -> None:
    for iss in ledger["issues"]:
        if iss["status"] == "ACCEPTED":
            assert iss["exemption"] not in (None, "NONE"), f"{iss['id']} ACCEPTED 缺 exemption（R6）"
            assert iss["due"], f"{iss['id']} ACCEPTED 缺 due（R6）"


def test_guard_files_exist(ledger: dict) -> None:
    for iss in ledger["issues"]:
        if iss["guard"] not in ("NONE", ""):
            assert (ROOT / iss["guard"]).exists(), f"{iss['id']} guard 不存在：{iss['guard']}"


def test_detect_specs_wellformed(ledger: dict) -> None:
    for iss in ledger["issues"]:
        assert iss["detect"], f"{iss['id']} 无 detect（无法复扫）"
        for spec in iss["detect"]:
            kind = spec.get("kind")
            assert kind in VALID_KINDS, f"{iss['id']} detect.kind 非法：{kind}"
            if kind in ("grep", "absent"):
                assert spec.get("pattern"), f"{iss['id']} detect.pattern 缺失"
            if kind == "grep":
                assert spec.get("paths") or spec.get("path"), f"{iss['id']} detect.paths 缺失"
            if kind in ("absent", "lines_gt"):
                assert spec.get("path"), f"{iss['id']} detect.path 缺失"


def test_scanner_detects_present_and_absent(ledger: dict) -> None:
    """复扫工具必须能区分"仍存在"与"已消失"。"""
    from audit_ledger import Issue, scan_issue

    # 构造两个探针条目：一��必然命中、一条必然不命中
    present = Issue(raw={
        "id": "KI-PROBE-001", "title": "probe-present", "theme": "docs",
        "severity": "P2", "status": "OPEN", "first_seen": "2026-11-01",
        "last_seen": "2026-11-01", "seen_count": 1, "sources": [], "locations": [],
        "detect": [{"kind": "grep", "pattern": "known-issues", "path": "config/known_issues.yaml"}],
        "impact": "", "guard": "NONE", "owner": "t", "due": "2026-11-01",
        "exemption": "NONE", "fix_hint": "",
    })
    state, _ = scan_issue(present)
    assert state == "STILL_PRESENT", state

    absent = Issue(raw={
        **present.raw, "id": "KI-PROBE-002",
        "detect": [{"kind": "grep", "pattern": "__this_pattern_never_exists__",
                    "path": "config/known_issues.yaml"}],
    })
    state, _ = scan_issue(absent)
    assert state == "RESOLVED", state


def test_probe_flags_unregistered_pattern(ledger: dict) -> None:
    """未登记的模式必须被判定为 NEW（保证新增路径可用）。"""
    from audit_ledger import Issue, Ledger, _find_duplicates

    led = Ledger(meta=ledger.get("meta", {}),
                 issues=[Issue(raw=i) for i in ledger["issues"]])
    dupes = _find_duplicates(led, "", "__totally_unregistered_pattern_xyz__")
    assert dupes == [], "未登记模式不应命中已有条目"
