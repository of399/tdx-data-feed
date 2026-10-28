"""行情表的 schema 契约：date 口径、列集、derived 与 daily 的关系。

- 列举规则（排除迁移备份）用临时目录验证，不依赖本机是否还留着 .v1.bak；
- 需要本机行情目录的用例单独标 needs_data，CI 精简集里跳过。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tdxfeed import paths

needs_data = pytest.mark.skipif(
    not paths.daily_dir().exists(), reason="需要本地行情目录（CI 精简集没有）"
)

DAILY_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]
ADJ_COLS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]
DERIVED_COLS = 29
# 与 `compute_one` 的 `len(df) < 60` 守卫一致：不足这么多行就算不出指标。
MIN_ROWS_FOR_INDICATORS = 60


def _sample(files: list[Path], k: int = 12) -> list[Path]:
    if len(files) <= k:
        return list(files)
    step = len(files) // k
    return [files[i * step] for i in range(k)]


def _schema(fp: Path) -> dict[str, str]:
    sch = pq.read_schema(fp)
    return {n: str(t) for n, t in zip(sch.names, sch.types)}


def test_daily_files_excludes_migration_backups(tmp_path, monkeypatch):
    """canonical 列举必须排除迁移备份，且只收 .parquet。"""
    for name in (
        "sh600000.parquet",
        "sz000001.parquet",
        "sh600000" + paths.DAILY_BAK_SUFFIX,
        "notes.csv",
    ):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(paths, "daily_dir", lambda: tmp_path)
    assert [p.name for p in paths.daily_files()] == [
        "sh600000.parquet",
        "sz000001.parquet",
    ]


@needs_data
def test_daily_schema_is_date32():
    files = paths.daily_files()
    assert files
    for fp in _sample(files):
        sch = _schema(fp)
        assert list(sch) == DAILY_COLS, fp
        assert sch["date"] == "date32[day]", fp


@needs_data
def test_adj_factor_schema_is_date32():
    files = paths.adj_factor_files()
    assert files
    for fp in _sample(files):
        sch = _schema(fp)
        assert list(sch) == ADJ_COLS, fp
        assert sch["date"] == "date32[day]", fp


@needs_data
def test_derived_schema_is_date32():
    derived = sorted(paths.derived_dir().glob("*_indicators.parquet"))
    assert derived
    for fp in _sample(derived):
        sch = _schema(fp)
        assert len(sch) == DERIVED_COLS, (fp.name, len(sch))
        assert str(sch["date"]) == "date32[day]", (fp.name, sch["date"])


@needs_data
def test_derived_has_no_orphans():
    """derived 里不能有 daily 已经没有的 code（反向领先是永久的错误）。"""
    daily_stems = {p.stem for p in paths.daily_files()}
    derived = sorted(paths.derived_dir().glob("*_indicators.parquet"))
    stems = {p.name.removesuffix("_indicators.parquet") for p in derived}
    assert stems - daily_stems == set(), sorted(stems - daily_stems)[:10]


@needs_data
def test_derived_missing_only_where_daily_is_newer():
    """允许缺的只有两类：daily 比最新 derived 还新（新股/新数据），或行数不够。

    第二类是设计结果：`compute_one` 对不足 60 行的标的返回空表，`process_one`
    随之返回 None，derived 本来就不会有。剩下的缺才是漏网（如 `--incremental`
    按 mtime 跳过留下的）。
    """
    derived = sorted(paths.derived_dir().glob("*_indicators.parquet"))
    assert derived
    newest = max(p.stat().st_mtime for p in derived)
    daily = {p.stem: p for p in paths.daily_files()}
    stems = {p.name.removesuffix("_indicators.parquet") for p in derived}
    stale = {
        c for c in set(daily) - stems
        if daily[c].stat().st_mtime <= newest
        and pq.read_metadata(daily[c]).num_rows >= MIN_ROWS_FOR_INDICATORS
    }
    assert stale == set(), sorted(stale)[:10]


@needs_data
def test_minute_datetime_is_string():
    files = sorted(paths.minute_dir("60").glob("*.parquet"))
    assert files
    for fp in _sample(files, 3):
        assert _schema(fp)["datetime"] == "string", fp.name
        ts = pd.read_parquet(fp, columns=["datetime"])["datetime"].iloc[0]
        assert len(str(ts)) == 19, fp.name
