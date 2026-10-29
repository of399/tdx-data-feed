"""canary 必须能发现 adj/derived 停更——只数文件个数是发现不了的。"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd

NEEDS_DATA = False  # 只用 tmp_path 造表，不读真实 data/

ROOT = Path(__file__).resolve().parents[1]


def _canary():
    spec = importlib.util.spec_from_file_location(
        "canary_tbl_fresh", ROOT / "scripts" / "canary_health_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, last: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime([last])}).to_parquet(path, index=False)


def _dirs(tmp_path):
    daily, adj, derived = tmp_path / "daily", tmp_path / "adj", tmp_path / "derived"
    _write(daily / "sh600000.parquet", "2026-10-28")
    _write(derived / "sh600000_indicators.parquet", "2026-10-28")
    _write(adj / "600000.parquet", "2026-10-28")
    return daily, adj, derived


def _wire(m, monkeypatch, daily, adj, derived):
    monkeypatch.setattr(m, "_today", lambda: date(2026, 10, 29))
    monkeypatch.setattr(m, "daily_dir", lambda: daily)
    monkeypatch.setattr(m, "adj_factor_dir", lambda: adj)
    monkeypatch.setattr(m, "derived_dir", lambda: derived)
    monkeypatch.setattr(m, "PARQUET_MIN_COUNT", 1)
    monkeypatch.setattr(m, "ADJ_MIN_COUNT", 1)


def test_data_files_ok_when_all_fresh(tmp_path, monkeypatch):
    m = _canary()
    _wire(m, monkeypatch, *_dirs(tmp_path))
    r = m.check_data_files()
    assert r["status"] == "ok", r


def test_data_files_warns_when_adj_stale(tmp_path, monkeypatch):
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    for i in range(5):
        _write(adj / f"60000{i}.parquet", "2026-09-24")
    _wire(m, monkeypatch, daily, adj, derived)
    r = m.check_data_files()
    assert r["status"] == "warn"
    assert "adj_factor" in r["message"]


def test_data_files_warns_when_derived_stale(tmp_path, monkeypatch):
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    for i in range(5):
        _write(derived / f"sh60000{i}_indicators.parquet", "2026-09-28")
    _wire(m, monkeypatch, daily, adj, derived)
    r = m.check_data_files()
    assert r["status"] == "warn"
    assert "derived" in r["message"]


def test_data_files_reports_both_count_and_freshness(tmp_path, monkeypatch):
    """计数不足与表陈旧是两个原因，不能互相覆盖——只剩一条就等于另一个静默了。"""
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    for i in range(5):
        _write(adj / f"60000{i}.parquet", "2026-09-24")
    _wire(m, monkeypatch, daily, adj, derived)
    monkeypatch.setattr(m, "PARQUET_MIN_COUNT", 999)
    r = m.check_data_files()
    assert r["status"] == "warn"
    assert "parquet/daily only" in r["message"], r
    assert "adj_factor 陈旧" in r["message"], r


def test_stale_threshold_skips_holidays():
    from tdxfeed.freshness import stale_threshold

    # 2026-10-09 之前最近的已收盘交易日是 09-30（国庆休市），再回退 1 个交易日 = 09-29
    assert stale_threshold(date(2026, 10, 9), allowed_trading_days=1) == date(2026, 9, 29)
    assert stale_threshold(date(2026, 10, 29), allowed_trading_days=3) == date(2026, 10, 23)


def test_sample_latest_date_returns_median(tmp_path):
    from tdxfeed.freshness import sample_latest_date

    files = []
    for i, d in enumerate(["2026-09-24", "2026-09-25", "2026-09-26"]):
        p = tmp_path / f"{i}.parquet"
        _write(p, d)
        files.append(p)
    assert sample_latest_date(files) == date(2026, 9, 25)
    assert sample_latest_date([]) is None
