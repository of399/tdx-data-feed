"""canary 必须能发现 adj/derived 停更——只数文件个数是发现不了的。"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd

# 本模块只用 tmp_path 造表，不读真实行情目录，无数据环境（CI）照常收集。
NEEDS_DATA = False

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


def test_data_files_tolerates_one_trading_day_lag(tmp_path, monkeypatch):
    """adj/derived 结构性只能追到 T-1（baostock 当日 EOD 晚间才发布），T-2 才算停更。

    _today()=2026-10-29 → 最近已收盘交易日 10-28，再回退 1 个交易日 = 10-27
    （10-24/25 是周末）。末日期 == limit 为 ok；比 limit 早一个交易日（10-26）
    才 warn——这条钉住「容忍 1 天、不容忍 2 天」。
    """
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    _wire(m, monkeypatch, daily, adj, derived)

    # 10-27 == stale_threshold(_today(), allowed_trading_days=1)：T-1 滞后是正常态。
    _write(adj / "600000.parquet", "2026-10-27")
    _write(derived / "sh600000_indicators.parquet", "2026-10-27")
    r = m.check_data_files()
    assert r["status"] == "ok", r
    assert r["details"]["stale_before"] == "2026-10-27"

    # 10-26 比 limit 早一个交易日：后置任务连着两天没成功，必须报警。
    _write(adj / "600000.parquet", "2026-10-26")
    _write(derived / "sh600000_indicators.parquet", "2026-10-26")
    r = m.check_data_files()
    assert r["status"] == "warn", r
    assert "adj_factor 陈旧" in r["message"], r
    assert "derived 陈旧" in r["message"], r


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
    """中位数必须挡住离群末日期——真实 derived 表里就有 2009-12-15 这类落单文件。

    样本故意做成「均值 ≠ 中位数」：换成均值实现时 09-25 那条会红，不必靠下面
    那条「均值不等于中位数」单独兜底（它只钉住样本本身有区分力）。
    """
    from tdxfeed.freshness import sample_latest_date

    dates = ["2026-09-14", "2026-09-24", "2026-09-25", "2026-09-26", "2026-10-28"]
    files = []
    for i, d in enumerate(dates):
        p = tmp_path / f"{i}.parquet"
        _write(p, d)
        files.append(p)
    assert sample_latest_date(files, sample_size=5) == date(2026, 9, 25)

    # 同一批日期的均值不是 09-25（均值会被 10-28 / 09-14 拉偏），样本因此有区分力。
    days = sorted(date.fromisoformat(d).toordinal() for d in dates)
    mean_day = date.fromordinal(sum(days) // len(days))
    assert mean_day != date(2026, 9, 25), mean_day

    assert sample_latest_date([]) is None
