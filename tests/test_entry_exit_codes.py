"""生产入口失败必须退出非零，否则 systemd / canary 看到的是绿的。"""
from __future__ import annotations

import sys
from pathlib import Path

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


def test_indicators_skip_is_not_failure(monkeypatch, tmp_path):
    """process_one 返回 None（已最新 / 数据不足）是 skip，不是失败。"""
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [Path("a.parquet")])
    monkeypatch.setattr(indicators, "process_one", lambda code, force=False: None)
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 0


def test_incremental_unknown_status_counts_as_failure(monkeypatch):
    """状态分流是白名单：不认识的状态按失败计，不许静默落到 skip。"""
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "write_failed", "rows": 0, "added": 0},
    )
    assert incremental.main() == 1


def test_incremental_main_dispatch_is_whitelisted():
    """源码级守卫：ok / up_to_date / fetch_failed 之外的 status 不得算成功。"""
    src = (Path(__file__).resolve().parents[1] / "tdxfeed" / "incremental.py").read_text(
        encoding="utf-8"
    )
    assert 'elif st == "up_to_date":' in src
    assert 'elif st == "fetch_failed":' in src


def test_entry_points_fail_on_empty_target_list(monkeypatch, tmp_path):
    """daily_files() 为空（数据目录整片缺失）不是「无事可做」，必须退 1。"""
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [])
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 1

    _patch_argv(monkeypatch, ["incremental"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [])
    assert incremental.main() == 1
