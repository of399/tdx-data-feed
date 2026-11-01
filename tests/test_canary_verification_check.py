"""canary 对每日验证快照的兜底：timer 停了、验证红了、latest 指针漂了都要报警。

`tdx-verification` 是 system 作用域，`onfailure-alert@.service` 只部署在用户管理器，
系统侧写 `OnFailure=` 会找不到模板、静默无效。所以失败信号收在 canary：
快照年龄、all_ok、latest 是否跟到最新，三项都进 PAGING_CHECKS。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 全程 tmp_path 构造快照，不读真实 v5/audit。
NEEDS_DATA = False

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _canary():
    spec = importlib.util.spec_from_file_location(
        "canary_verification_under_test", ROOT / "scripts" / "canary_health_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _snapshot(directory: Path, name: str, *, age: timedelta,
              all_ok: bool = True) -> Path:
    path = directory / name
    ts = (NOW - age).isoformat()
    path.write_text(json.dumps({'ts': ts, 'tests': 132, 'all_ok': all_ok}),
                    encoding='utf-8')
    return path


def _latest(directory: Path, target: Path) -> None:
    link = directory / "latest.json"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.name)


def test_fresh_ok_snapshot_passes(tmp_path):
    canary = _canary()
    newest = _snapshot(tmp_path, "verification-20261101-020000.json",
                       age=timedelta(hours=1))
    _latest(tmp_path, newest)
    result = canary.check_verification(tmp_path, now=NOW)
    assert result['status'] == 'ok', result
    assert result['details']['age_hours'] == 1.0


def test_stale_snapshot_warns(tmp_path):
    """timer 停了不会抛异常：快照一直停在原地，只有年龄检查能发现。"""
    canary = _canary()
    newest = _snapshot(tmp_path, "verification-20261030-020000.json",
                       age=timedelta(hours=58))
    _latest(tmp_path, newest)
    result = canary.check_verification(tmp_path, now=NOW)
    assert result['status'] == 'warn', result
    assert 'too old' in result['message'] or '太旧' in result['message']


def test_failed_verification_warns(tmp_path):
    canary = _canary()
    newest = _snapshot(tmp_path, "verification-20261101-020000.json",
                       age=timedelta(hours=1), all_ok=False)
    _latest(tmp_path, newest)
    result = canary.check_verification(tmp_path, now=NOW)
    assert result['status'] == 'warn', result
    assert 'all_ok' in result['message']


def test_latest_must_point_at_the_newest_snapshot(tmp_path):
    """symlink 没跟着换 = 人读到的是旧结论，和 timer 停了一样危险。"""
    canary = _canary()
    old = _snapshot(tmp_path, "verification-20261031-020000.json",
                    age=timedelta(hours=25))
    _snapshot(tmp_path, "verification-20261101-020000.json", age=timedelta(hours=1))
    _latest(tmp_path, old)
    result = canary.check_verification(tmp_path, now=NOW)
    assert result['status'] == 'warn', result
    assert 'latest.json' in result['message']


def test_missing_latest_warns(tmp_path):
    canary = _canary()
    _snapshot(tmp_path, "verification-20261101-020000.json", age=timedelta(hours=1))
    result = canary.check_verification(tmp_path, now=NOW)
    assert result['status'] == 'warn', result
    assert 'latest.json' in result['message']


def test_missing_snapshot_dir_warns(tmp_path):
    canary = _canary()
    result = canary.check_verification(tmp_path / "nope", now=NOW)
    assert result['status'] == 'warn', result


def test_verification_warn_pages(monkeypatch, tmp_path):
    """只在 journal 里留一行等于没人知道；必须变成非零退出。"""
    canary = _canary()
    assert 'verification' in canary.PAGING_CHECKS
    monkeypatch.setattr(canary, "LOG_FILE", tmp_path / "canary.log")
    monkeypatch.setattr(canary, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(canary, "run_all_checks", lambda: {
        "ts": "2026-11-01T12:00:00",
        "checks": [{"name": "verification", "status": "warn",
                    "message": "stale", "details": {}}],
    })
    monkeypatch.setattr(sys, "argv", ["canary", "--quiet"])
    with pytest.raises(SystemExit) as exc:
        canary.main()
    assert exc.value.code == 1


def test_system_onfailure_must_have_a_system_scope_template():
    """系统单元不能引用用户管理器的模板：模板不在同一作用域，写错是静默失效。

    当前仓库没有 system 作用域的 `onfailure-alert@.service`，所以 tdx-verification
    不写 OnFailure，兜底是 canary 的 verification 检查。以后真加了系统模板，
    这里允许写 OnFailure，但模板镜像必须同时入库。
    """
    unit = (ROOT / "systemd" / "system" / "tdx-verification.service").read_text(encoding="utf-8")
    m = re.search(r"^OnFailure=(\S+)", unit, re.M)
    if m:
        template = m.group(1).split("@", 1)[0] + "@.service"
        assert (ROOT / "systemd" / "system" / template).is_file(), (
            f"{m.group(1)} 引用了 {template}，但 systemd/system/ 下没有系统作用域模板"
        )
    else:
        canary = _canary()
        assert "verification" in canary.PAGING_CHECKS, (
            "没有 OnFailure 时，verification 失败必须由 canary 的非零退出兜底"
        )
