"""P0-1（KI-COMPETE-001）：同步完成哨兵的双通道判定 + 写哨兵。

`check_sync_sentinel.py` 两条通道任一满足即放行；都不满足则 exit 2（不出榜）。
`mark_sync_done.py` 在 full_sync 成功后写 `data/.state/sync-done`（原子落位）。
不依赖真实 systemd：用 monkeypatch 顶替 subprocess.run 与 SENTINEL/ROOT/atomic_write_text。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_sentinel(mod, tmp_path):
    s = tmp_path / "sync-done"
    import sys
    sys.path.insert(0, str(ROOT))
    return s


def test_check_passes_when_file_fresh(tmp_path, monkeypatch):
    mod = _load("check_sync_sentinel", "scripts/check_sync_sentinel.py")
    s = tmp_path / "sync-done"
    s.write_text(json.dumps({"ts": "x", "last_date": "2026-11-02"}))
    monkeypatch.setattr(mod, "SENTINEL", s)
    assert mod.main(["--max-age-hours", "12"]) == 0


def test_check_blocks_when_stale_and_unit_not_success(tmp_path, monkeypatch):
    mod = _load("check_sync_sentinel", "scripts/check_sync_sentinel.py")
    s = tmp_path / "sync-done"
    s.write_text("{}")
    old = time.time() - 99 * 3600
    import os
    os.utime(s, (old, old))
    monkeypatch.setattr(mod, "SENTINEL", s)
    # 通道 B 不可达：单元非 success
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, "Result=failed\nExecMainExitTimestamp=\n", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mod.main(["--max-age-hours", "12"]) == 2


def test_check_passes_via_unit_channel(tmp_path, monkeypatch):
    mod = _load("check_sync_sentinel", "scripts/check_sync_sentinel.py")
    s = tmp_path / "sync-done"
    s.write_text("{}")
    import os
    old = time.time() - 99 * 3600
    os.utime(s, (old, old))
    monkeypatch.setattr(mod, "SENTINEL", s)
    from datetime import datetime
    recent = datetime.now().isoformat(timespec="seconds")  # naive，与 check 里的 datetime.now() 同口径
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, f"Result=success\nExecMainExitTimestamp={recent}\n", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mod.main(["--max-age-hours", "12"]) == 0


def test_check_dry_run_never_exits_nonzero(tmp_path, monkeypatch):
    mod = _load("check_sync_sentinel", "scripts/check_sync_sentinel.py")
    s = tmp_path / "sync-done"
    s.write_text("{}")
    import os
    old = time.time() - 99 * 3600
    os.utime(s, (old, old))
    monkeypatch.setattr(mod, "SENTINEL", s)
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, "Result=failed\n", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mod.main(["--dry-run", "--max-age-hours", "12"]) == 0


def test_mark_writes_json_payload(tmp_path, monkeypatch):
    mod = _load("mark_sync_done", "scripts/mark_sync_done.py")
    s = tmp_path / "sync-done"
    captured: dict = {}
    def fake_atomic(path, text, **kw):
        captured["path"] = path
        captured["text"] = text
        return path
    monkeypatch.setattr(mod, "atomic_write_text", fake_atomic)
    monkeypatch.setattr(mod, "SENTINEL", s)
    monkeypatch.setattr(mod, "MANIFEST", tmp_path / "no-manifest.json")  # 不存在 => last_date None
    assert mod.main() == 0
    payload = json.loads(captured["text"])
    assert "ts" in payload
    assert payload["last_date"] is None
