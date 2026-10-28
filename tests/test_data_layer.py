"""路径访问器只定位不 fallback；写入口是 date32 + 原子写；v5 孪生与主包同解。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tdxfeed import parquet_io, paths

ROOT = Path(__file__).resolve().parents[1]


def test_table_dirs_share_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.parquet_root() == tmp_path / "parquet"
    assert paths.daily_dir() == tmp_path / "parquet" / "daily"
    assert paths.derived_dir() == tmp_path / "parquet" / "derived"
    assert paths.adj_factor_dir() == tmp_path / "adj_factor"
    assert paths.hfq_factor_dir() == tmp_path / "hfq_factor"
    assert paths.finance_parquet() == tmp_path / "parquet" / "finance.parquet"
    assert paths.finance_deep_dir() == tmp_path / "parquet" / "finance_deep"
    assert paths.xdxr_path() == tmp_path / "parquet" / "xdxr.parquet"


def test_minute_dir_accepts_both_spellings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.minute_dir("60") == paths.minute_dir("min60") == tmp_path / "parquet" / "min60"
    assert paths.minute_dir("1min") == paths.minute_dir("m1") == paths.minute_dir("min1")
    assert paths.minute_dir("MIN5") == paths.minute_dir("5")
    assert paths.minute_path("600000", "60").parent == paths.minute_dir("60")
    assert paths.minute_path("600000", "45") == tmp_path / "parquet" / "min45" / "sh600000.parquet"


def test_daily_files_excludes_migration_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.daily_dir()
    d.mkdir(parents=True)
    for name in ("sh600000.parquet", "sh600000.v1.bak.parquet", "sz000001.parquet",
                 "_audit.csv"):
        (d / name).write_bytes(b"")
    assert [p.name for p in paths.daily_files()] == ["sh600000.parquet", "sz000001.parquet"]


def test_adj_factor_files_lists_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.adj_factor_dir()
    d.mkdir(parents=True)
    (d / "000001.parquet").write_bytes(b"")
    (d / "600000.parquet").write_bytes(b"")
    assert [p.name for p in paths.adj_factor_files()] == ["000001.parquet", "600000.parquet"]


def _bars(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.1] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })


def test_write_table_forces_date32_and_leaves_no_tmp(tmp_path):
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    assert [p.name for p in tmp_path.iterdir()] == ["x.parquet"]


def test_write_table_replaces_inode_not_overwrites(tmp_path):
    """硬链接快照的前提：写盘必须换 inode，否则 cp -al 备份会被就地改写穿透。"""
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    before = os.stat(fp).st_ino
    parquet_io.write_table(fp, _bars(8))
    assert os.stat(fp).st_ino != before


def test_read_daily_sorts_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    df = _bars(4).iloc[::-1].reset_index(drop=True)          # 倒序
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)     # 重复日期
    (paths.daily_dir()).mkdir(parents=True)
    df.to_parquet(paths.daily_path("600000"), index=False)    # 故意写成 timestamp
    out = parquet_io.read_daily("600000")
    assert out["date"].is_monotonic_increasing
    assert len(out) == 4
    assert str(out["date"].dtype).startswith("datetime64")


def test_write_daily_uses_canonical_name(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    fp = parquet_io.write_daily(_bars(), "600000")
    assert fp.name == "sh600000.parquet"


def test_read_minute_keeps_string_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.minute_dir("60").mkdir(parents=True)
    df = pd.DataFrame({"datetime": ["2026-10-27 15:00:00", "2026-10-27 14:00:00"],
                       "close": [10.0, 10.1]})
    df.to_parquet(paths.minute_path("600000", "60"), index=False)
    raw = parquet_io.read_minute("600000", "60")
    assert raw["datetime"].iloc[0] == "2026-10-27 14:00:00"
    assert raw["datetime"].dtype == object
    parsed = parquet_io.read_minute("600000", "60", parse_time=True)
    assert str(parsed["datetime"].dtype).startswith("datetime64")


def test_missing_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        parquet_io.read_daily("600000")


def _load_v5_paths():
    spec = importlib.util.spec_from_file_location("v5_paths_under_test",
                                                  ROOT / "v5/scripts/_paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v5_twin_agrees_with_main_package(tmp_path, monkeypatch):
    v5 = _load_v5_paths()
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(v5, "DATA_ROOT", tmp_path)
    for code in ("600519", "000001", "920001", "sh600519", "bj920001", "300750"):
        assert v5.daily_path(code) == paths.daily_path(code), code
        assert v5.adj_factor_path(code) == paths.adj_factor_path(code), code
    for period in ("5", "15", "60", "min60"):
        assert v5.minute_dir(period) == paths.minute_dir(period), period
    assert v5.derived_path("sh600519") == paths.derived_path("sh600519")
    assert v5.DAILY_BAK_SUFFIX == paths.DAILY_BAK_SUFFIX


def test_v5_twin_does_not_import_main_package():
    src = (ROOT / "v5/scripts/_paths.py").read_text(encoding="utf-8")
    assert "import tdxfeed" not in src
    assert "from tdxfeed" not in src
