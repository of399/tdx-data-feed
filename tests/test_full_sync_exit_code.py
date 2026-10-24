"""夜间全量同步必须把失败如实反映成进程退出码。

`systemd/tdxfeed-sync.service` 用 `python -m tdxfeed.full_sync` 启动，且
`predict-daily` / `plan-e-journal` 只靠钟表串在它后面（`Wants=` 而非 `Requires=`）。
所以只要 full_sync 恒返回 0，下游就会拿着陈旧数据继续出票，且无人被通知。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxfeed import full_sync


def test_all_symbols_returned_no_bars_is_a_failure():
    """5221 只全部拿到空 bars：ok=0/fail=0/skip=5221 —— 旧口径算出 0% 失败率。"""
    assert full_sync.sync_exit_code({"ok": 0, "fail": 0, "skip": 5221}, {"ok": 0, "fail": 0}) == 1


def test_nothing_attempted_at_all_is_a_failure():
    assert full_sync.sync_exit_code({"ok": 0, "fail": 0, "skip": 0}, {"ok": 0, "fail": 0}) == 1


def test_most_symbols_failing_is_a_failure():
    assert full_sync.sync_exit_code({"ok": 100, "fail": 4900, "skip": 0}, {"ok": 14, "fail": 0}) == 1


def test_healthy_night_exits_zero():
    individual = {"ok": 5200, "fail": 1, "skip": 20}
    assert full_sync.sync_exit_code(individual, {"ok": 14, "fail": 0}) == 0


def test_index_channel_failure_alone_does_not_void_good_stock_data():
    """个股全写成功、指数通道全挂：数据主体可用，不该误报整夜失败。"""
    assert full_sync.sync_exit_code({"ok": 5221, "fail": 0, "skip": 0}, {"ok": 0, "fail": 14}) == 0


def test_main_returns_failure_code(monkeypatch, tmp_path):
    """退出码要真的从 main() 传出去，而不只是躺在一个没人调的函数里。"""
    import types

    monkeypatch.setattr(full_sync, "OUT_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(full_sync, "SLEEP", 0.0)
    monkeypatch.setattr(full_sync, "get_all_codes", lambda: ["600000", "000001"])
    monkeypatch.setattr(full_sync.SinaSource, "fetch_bars", lambda self, sym: [])
    monkeypatch.setattr(full_sync, "download_indices", lambda w, s, src: {"ok": 0, "fail": 0})
    monkeypatch.setitem(
        sys.modules, "tdxfeed.fin_parquet", types.SimpleNamespace(incremental=lambda: None)
    )

    assert full_sync.main() == 1
