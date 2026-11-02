"""P0-2（KI-OPS-002）：onfailure_alert 必须能按 --scope 查系统级单元。

历史实现硬编码 `systemctl --user show` / `journalctl --user`，系统级单元（tdx-sync /
tdx-monitor / tdx-verification）失败查不到、载荷全是 ?。修复：collect_state / journal_tail_cmd
接受 scope 参数，system 时去掉 --user。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "onfailure_alert_under_test", ROOT / "scripts" / "onfailure_alert.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_user_scope_keeps_user_flag(monkeypatch):
    mod = _load()
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        return ""
    monkeypatch.setattr(mod, "_run", fake_run)
    mod.collect_state("predict-daily.service", scope="user")
    assert "--user" in seen["argv"], seen["argv"]


def test_system_scope_drops_user_flag(monkeypatch):
    mod = _load()
    calls: list = []
    def fake_run(argv, **kw):
        calls.append(list(argv))
        return ""
    monkeypatch.setattr(mod, "_run", fake_run)
    mod.collect_state("tdx-sync.service", scope="system")
    # show 与 journal 两条调用都不该带 --user（系统级）
    assert any("--user" not in c for c in calls), calls
    assert any("show" in c for c in calls)


def test_journal_tail_scope(monkeypatch):
    mod = _load()
    assert "--user" in mod.journal_tail_cmd("x", scope="user")
    assert "--user" not in mod.journal_tail_cmd("x", scope="system")


def test_main_routes_scope_to_system(monkeypatch):
    mod = _load()
    calls: list = []
    def fake_run(argv, **kw):
        calls.append(list(argv))
        return ""
    monkeypatch.setattr(mod, "_run", fake_run)
    # 投递走 urllib 而非 subprocess；mock 掉它，让 rc=0 只验证 scope 路由
    monkeypatch.setattr(mod, "post_alert", lambda *a, **k: (True, "ok"))
    rc = mod.main(["tdx-sync.service", "--scope", "system", "--url", "http://example"])
    assert rc == 0
    # show 调用不该带 --user（系统级）
    assert any(("show" in c and "--user" not in c) for c in calls), calls
