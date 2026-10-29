"""生产入口失败必须退出非零，否则 systemd / canary 看到的是绿的。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tdxfeed import incremental
from tdxfeed.precompute import indicators


def _patch_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)


def test_indicators_returns_1_when_any_fail(monkeypatch, tmp_path):
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [Path("a.parquet")])

    def boom(code, force=False):
        raise RuntimeError("read failed")

    monkeypatch.setattr(indicators, "process_one", boom)
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 1


def test_indicators_returns_0_when_all_ok(monkeypatch, tmp_path):
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [Path("a.parquet")])
    monkeypatch.setattr(indicators, "process_one", lambda code, force=False: tmp_path / "x.parquet")
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 0


def test_incremental_returns_1_when_any_fail(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])

    def boom(sym):
        raise RuntimeError("source down")

    monkeypatch.setattr(incremental, "incremental_update", boom)
    assert incremental.main() == 1


def test_incremental_returns_0_when_all_ok(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"status": "ok", "added": 1},
    )
    assert incremental.main() == 0


def test_incremental_counts_fetch_failed_as_failure(monkeypatch):
    """fetch_failed 是行情源全挂，不是「无需更新」——必须退 1。"""
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "fetch_failed", "rows": 0, "added": 0},
    )
    assert incremental.main() == 1


def test_incremental_up_to_date_is_not_failure(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "up_to_date", "rows": 10, "added": 0},
    )
    assert incremental.main() == 0
