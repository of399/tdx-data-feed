"""写路径的原子性：失败不留半截文件、并发不互踩。"""
import json
import os
import struct

import pytest

from tdxfeed.backfill_history import acquire_run_lock, load_done, mark_done
from tdxfeed.state import State
from tdxfeed.writers import Writers

NEEDS_DATA = False

BARS = [
    ["20260105", 10.0, 11.0, 9.0, 10.0, 100.0, 100000.0],
    ["20260106", 10.0, 11.0, 9.0, 10.0, 200.0, 200000.0],
]


def _tmp_residue(root):
    return [str(p) for p in root.rglob("*.tmp")]


def test_state_save_keeps_old_file_when_dump_fails(tmp_path, monkeypatch):
    path = tmp_path / ".state" / "manifest.json"
    st = State(str(path))
    st.update("sh600000", "20260105", 3)
    before = path.read_text(encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("tdxfeed.state.json.dump", boom)
    st.data["sz000001"] = {"last_date": "20260105", "count": 1}
    with pytest.raises(RuntimeError):
        st.save()

    assert path.read_text(encoding="utf-8") == before
    assert _tmp_residue(tmp_path) == []


def test_write_day_leaves_previous_file_intact_on_failure(tmp_path, monkeypatch):
    w = Writers(str(tmp_path))
    p = w.write_day("sh600000", BARS)
    before = open(p, "rb").read()
    assert _tmp_residue(tmp_path) == []

    real_pack = struct.pack
    calls = {"n": 0}

    def flaky(fmt, *args):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return real_pack(fmt, *args)

    monkeypatch.setattr("tdxfeed.writers.struct.pack", flaky)
    with pytest.raises(ValueError):
        w.write_day("sh600000", BARS)

    assert open(p, "rb").read() == before
    assert _tmp_residue(tmp_path) == []


def test_write_day_safe_still_saturates_volume(tmp_path):
    p = Writers(str(tmp_path)).write_day_safe(
        "sh600000", [["20260105", 1.0, 1.0, 1.0, 1.0, 10**12, 1.0]])
    rec = open(p, "rb").read(32)
    assert struct.unpack("<iiiiifii", rec)[6] == 2147483647


def test_mark_done_keeps_previous_done_on_failure(tmp_path, monkeypatch):
    path = str(tmp_path / "state" / "history_backfill.json")
    mark_done(path, "sh600000")
    before = open(path, encoding="utf-8").read()

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("tdxfeed.backfill_history.json.dump", boom)
    with pytest.raises(RuntimeError):
        mark_done(path, "sz000001")

    assert open(path, encoding="utf-8").read() == before
    assert load_done(path) == {"sh600000"}
    assert _tmp_residue(tmp_path) == []


def test_run_lock_is_exclusive_and_released_on_close(tmp_path):
    path = str(tmp_path / "run.lock")
    first = acquire_run_lock(path)
    assert first is not None
    assert acquire_run_lock(path) is None
    first.close()
    second = acquire_run_lock(path)
    assert second is not None
    second.close()
