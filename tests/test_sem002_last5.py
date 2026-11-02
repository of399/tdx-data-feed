"""P1-12 项 1：最后 5 处 SEM002 的配对功能测试（KI-SILENT-001 / KI-SILENT-002）。

结构层（全仓 SEM002=0、键名真实、Counter 声明）由 test_sem002_convergence /
test_error_counters / ci_semantic_guard 守着；这里逐处**真实触发**吞错路径，
证明计数确实递增、且控制流不变（该返回什么还返回什么）。

两处模块级 import 退化（schema_validation / parquet_reader）用 reload + 屏蔽
sys.modules 触发；计数键按守卫约定用 "<module>"。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

NEEDS_DATA = False


def test_sentinel_subprocess_failure_counted(monkeypatch):
    import check_sync_sentinel as css

    def boom(*a, **k):
        raise OSError("模拟 systemctl 不可用")

    monkeypatch.setattr(css.subprocess, "run", boom)
    before = css._ERROR_COUNT.get("_unit_success_recent", 0)
    assert css._unit_success_recent(24.0) is False, "兜底语义不变：查不到即不放行"
    assert css._ERROR_COUNT.get("_unit_success_recent", 0) == before + 1


def test_sentinel_bad_timestamp_counted(monkeypatch):
    import check_sync_sentinel as css

    class FakeProc:
        stdout = "Result=success\nExecMainExitTimestamp=not-a-date\n"

    monkeypatch.setattr(css.subprocess, "run", lambda *a, **k: FakeProc())
    before = css._ERROR_COUNT.get("_unit_success_recent", 0)
    assert css._unit_success_recent(24.0) is False, "时间戳解析失败仍不放行"
    assert css._ERROR_COUNT.get("_unit_success_recent", 0) == before + 1


def test_mark_sync_done_bad_manifest_counted(monkeypatch, tmp_path):
    import mark_sync_done as msd

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{这不是合法 JSON", encoding="utf-8")
    sentinel = tmp_path / "sync-done"
    monkeypatch.setattr(msd, "MANIFEST", manifest)
    monkeypatch.setattr(msd, "SENTINEL", sentinel)
    before = msd._ERROR_COUNT.get("main", 0)
    assert msd.main() == 0, "manifest 坏了仍写哨兵（last_date=None），控制流不变"
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    assert payload["last_date"] is None
    assert msd._ERROR_COUNT.get("main", 0) == before + 1


def test_schema_validation_import_fallback_counted(monkeypatch):
    import mcp.schema_validation as sv

    before = sv._ERROR_COUNT.get("<module>", 0)
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    try:
        importlib.reload(sv)
        assert sv._HAS_JSONSCHEMA is False, "退化语义保留"
        assert sv._ERROR_COUNT.get("<module>", 0) == before + 1
    finally:
        monkeypatch.undo()
        importlib.reload(sv)


def test_parquet_reader_import_fallback_counted(monkeypatch):
    import mcp.servers.parquet_reader as pr

    before = pr._ERROR_COUNT.get("<module>", 0)
    monkeypatch.setitem(sys.modules, "mcp.schema_validation", None)
    try:
        importlib.reload(pr)
        assert pr._ERROR_COUNT.get("<module>", 0) == before + 1
        assert callable(pr.validate_request), "文件直载兜底仍可用"
    finally:
        monkeypatch.undo()
        importlib.reload(pr)
