"""canary 对全市场同步的新鲜度判定。

同步搬到系统作用域（`tdx-sync.timer`/`tdx-sync.service`）之后，canary 原来读的
`ActiveEnterTimestamp` 在 oneshot 单元被 GC 后一律为空——年龄检查整段跳过，
结果是「同步停了两周也显示 ok」。这里用 timer 的 `LastTriggerUSec` 当时间源，
并要求「从未触发」「过期」「上次结果非 success」三种都显式 warn。

只替换 subprocess 输出，不发网络。
"""
from __future__ import annotations

import importlib.util
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _canary():
    spec = importlib.util.spec_from_file_location(
        "canary_health_under_test", ROOT / "scripts" / "canary_health_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _fake_run(responses: dict[str, str]):
    calls: list[list[str]] = []

    def run(cmd, *a, **k):
        calls.append(list(cmd))
        if '--user' in cmd:
            raise AssertionError(f"同步已搬去系统管理器，别再加 --user：{cmd}")
        for marker, out in responses.items():
            if marker in ' '.join(cmd):
                return subprocess.CompletedProcess(cmd, 0, out, '')
        raise AssertionError(f"未预期的命令 {cmd}")

    return run, calls


def _stamp(age: timedelta) -> str:
    ts = datetime.now() - age
    return ts.strftime("%a %Y-%m-%d %H:%M:%S CST")


def test_fresh_sync_is_ok(monkeypatch):
    canary = _canary()
    run, calls = _fake_run({
        "tdx-sync.timer": f"LastTriggerUSec={_stamp(timedelta(hours=3))}\nActiveState=active\n",
        "tdx-sync.service": "Result=success\n",
    })
    monkeypatch.setattr(canary.subprocess, "run", run)
    result = canary.check_tdxfeed_sync()
    assert result["status"] == "ok", result
    assert any("tdx-sync.timer" in c for c in calls)


def test_stale_sync_warns_with_age(monkeypatch):
    canary = _canary()
    run, _ = _fake_run({
        "tdx-sync.timer": f"LastTriggerUSec={_stamp(timedelta(days=3))}\nActiveState=active\n",
        "tdx-sync.service": "Result=success\n",
    })
    monkeypatch.setattr(canary.subprocess, "run", run)
    result = canary.check_tdxfeed_sync()
    assert result["status"] == "warn", result
    assert "old" in result["message"]


def test_never_triggered_warns_instead_of_passing(monkeypatch):
    """空时间戳曾被当成「没有时间信息」静默跳过——那正是最该报警的形态。"""
    canary = _canary()
    run, _ = _fake_run({
        "tdx-sync.timer": "LastTriggerUSec=\nActiveState=active\n",
        "tdx-sync.service": "Result=success\n",
    })
    monkeypatch.setattr(canary.subprocess, "run", run)
    result = canary.check_tdxfeed_sync()
    assert result["status"] == "warn", result
    assert "never" in result["message"]


def test_last_run_failed_warns(monkeypatch):
    canary = _canary()
    run, _ = _fake_run({
        "tdx-sync.timer": f"LastTriggerUSec={_stamp(timedelta(hours=2))}\nActiveState=active\n",
        "tdx-sync.service": "Result=exit-code\n",
    })
    monkeypatch.setattr(canary.subprocess, "run", run)
    result = canary.check_tdxfeed_sync()
    assert result["status"] == "warn", result
    assert "exit-code" in result["message"]


# ---------- 数据链路的 warn 必须由非零退出变成告警 ----------

def _run_main(canary, monkeypatch, tmp_path, checks, *argv):
    import sys

    monkeypatch.setattr(canary, "LOG_FILE", tmp_path / "canary.log")
    monkeypatch.setattr(canary, "HISTORY_FILE", tmp_path / "canary_history.jsonl")
    monkeypatch.setattr(canary, "run_all_checks",
                        lambda: {"ts": "2026-10-28T13:00:00", "checks": checks})
    monkeypatch.setattr(sys, "argv", ["canary", *argv])
    with pytest.raises(SystemExit) as exc:
        canary.main()
    return exc.value.code


def _check(name, status, message="m"):
    return {"name": name, "status": status, "message": message, "details": {}}


def test_stale_sync_warn_pages(monkeypatch, tmp_path):
    """OnFailure 只看退出码：warn 若仍 exit 0，同步停摆就只留在 journal 里。"""
    canary = _canary()
    code = _run_main(canary, monkeypatch, tmp_path,
                     [_check("tdxfeed_sync", "warn", "sync too old")], "--quiet")
    assert code == 1


def test_timer_warn_pages(monkeypatch, tmp_path):
    canary = _canary()
    code = _run_main(canary, monkeypatch, tmp_path,
                     [_check("timers", "warn", "inactive timers")], "--quiet")
    assert code == 1


def test_non_pipeline_warn_does_not_page(monkeypatch, tmp_path):
    """GPU/磁盘这类长期 warn 每 5 分钟推一次就是刷屏；它们留在 journal 与 canary 历史里。"""
    canary = _canary()
    code = _run_main(canary, monkeypatch, tmp_path,
                     [_check("gpu", "warn", "nvidia-smi missing"),
                      _check("tdxfeed_sync", "ok")], "--quiet")
    assert code == 0
