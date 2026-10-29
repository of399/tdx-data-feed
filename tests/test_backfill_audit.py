"""done 集对账：三类分桶与退出码（异常才非零）。"""
from __future__ import annotations

import json

from tdxfeed.backfill_audit import list_daily_symbols, reconcile

NEEDS_DATA = False


def _load_cli():
    import importlib.util
    from pathlib import Path

    fp = Path(__file__).resolve().parent.parent / "scripts" / "audit_backfill_done.py"
    spec = importlib.util.spec_from_file_location("audit_backfill_cli_under_test", fp)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_reconcile_flags_done_without_file():
    r = reconcile({"sh600000", "sh600001"}, {"sh600000"})
    assert r["done_no_file"] == ["sh600001"]
    assert r["anomalies"] == ["sh600001"]
    assert not r["ok"]


def test_reconcile_reports_file_without_done_as_backlog_not_anomaly():
    r = reconcile({"sh600000"}, {"sh600000", "sh600001", "sz000021"})
    assert r["file_no_done"] == ["sh600001", "sz000021"]
    assert r["ok"], "磁盘比 done 多出文件不等于状态说谎"


def test_reconcile_flags_done_still_shallow_per_audit():
    r = reconcile(
        {"sh600000", "sh600001", "sh600002"},
        {"sh600000", "sh600001", "sh600002"},
        {"sh600000": "ok", "sh600001": "extend", "sh600002": "listed_after_target"},
    )
    assert r["done_shallow"] == ["sh600001"]
    assert r["pending_backfill"] == []
    assert r["done_not_needed"] == ["sh600000", "sh600002"]
    assert r["anomalies"] == ["sh600001"]


def test_reconcile_without_audit_skips_depth_buckets():
    r = reconcile({"sh600000"}, {"sh600000"})
    assert r["ok"]
    assert r["pending_backfill"] == [] and r["done_shallow"] == []


def test_list_daily_symbols_skips_migration_backups(tmp_path):
    (tmp_path / "sh600000.parquet").write_bytes(b"")
    (tmp_path / "sh600001.v1.bak.parquet").write_bytes(b"")
    assert list_daily_symbols(tmp_path) == {"sh600000"}


def test_cli_exit_codes_and_json(tmp_path, capsys):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "sh600000.parquet").write_bytes(b"")
    resume = tmp_path / ".state" / "history_backfill.json"
    resume.parent.mkdir(parents=True)
    resume.write_text(json.dumps({"done": ["sh600000", "sh600001"]}),
                      encoding="utf-8")
    audit = tmp_path / "history_depth_1.json"
    audit.write_text(json.dumps({"rows": [
        {"symbol": "sh600000", "state": "extend"},
    ]}), encoding="utf-8")

    mod = _load_cli()
    assert mod.main(["--resume", str(resume), "--daily", str(daily),
                     "--audit", str(audit), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["done_no_file"] == ["sh600001"]
    assert report["done_shallow"] == ["sh600000"]

    # 补完之后审计再判一次：state 变成 ok，只剩 done 里那条无文件的陈旧记录被清掉
    resume.write_text(json.dumps({"done": ["sh600000"]}), encoding="utf-8")
    audit.write_text(json.dumps({"rows": [
        {"symbol": "sh600000", "state": "ok"},
    ]}), encoding="utf-8")
    assert mod.main(["--resume", str(resume), "--daily", str(daily),
                     "--audit", str(audit)]) == 0
    assert "对账通过" in capsys.readouterr().out


def test_cli_without_audit_artifact_says_depth_unchecked(tmp_path, capsys):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "sh600000.parquet").write_bytes(b"")
    resume = tmp_path / "history_backfill.json"
    resume.write_text(json.dumps({"done": ["sh600000"]}), encoding="utf-8")

    mod = _load_cli()
    assert mod.main(["--resume", str(resume), "--daily", str(daily),
                     "--audit", str(tmp_path / "nope.json")]) == 0
    assert "未检查" in capsys.readouterr().out
