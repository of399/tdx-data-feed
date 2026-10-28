"""行情表写盘只有一个入口：tdxfeed.parquet_io.write_table（date32 + 原子替换）。"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]

ALLOWED = {"tdxfeed/parquet_io.py"}
PA_DATE32 = re.compile(r"\bpa\.date32\(")
PQ_WRITE = re.compile(r"\bpq\.write_table\(")
SKIP_DIRS = {".git", ".qoder", "_archive", "node_modules", "tests"}
PRODUCERS = [
    "scripts/download_universe.py",
    "scripts/migrate_v1_to_v2.py",
    "tdxfeed/precompute/indicators.py",
]


def _iter_sources():
    for p in ROOT.rglob("*.py"):
        parts = p.relative_to(ROOT).parts[:-1]
        if any(part in SKIP_DIRS or part.startswith("venv") for part in parts):
            continue
        yield p


def _bars(n: int = 80) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.1] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })


def _date_type(fp: Path) -> str:
    return str(pq.read_schema(fp).field("date").type)


def test_no_raw_parquet_writer_outside_parquet_io():
    offenders = []
    for p in _iter_sources():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if PA_DATE32.search(src) or PQ_WRITE.search(src):
            offenders.append(rel)
    assert offenders == []


@pytest.mark.parametrize("rel", PRODUCERS)
def test_producers_call_write_table(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "write_table(" in src, rel
    assert ".to_parquet(" not in src, rel


def test_writers_helper_is_date32_and_replaces_inode(tmp_path):
    from tdxfeed.writers import Writers

    fp = tmp_path / "sh600000.parquet"
    Writers._write_parquet_date32(str(fp), _bars())
    assert _date_type(fp) == "date32[day]"
    ino = fp.stat().st_ino
    Writers._write_parquet_date32(str(fp), _bars(60))
    assert fp.stat().st_ino != ino, "就地覆盖会穿透 cp -al 快照"
    assert [p.name for p in tmp_path.iterdir()] == ["sh600000.parquet"]


def test_incremental_write_v2_keeps_column_guard(tmp_path):
    from tdxfeed import incremental as inc

    fp = tmp_path / "sh600000.parquet"
    with pytest.raises(ValueError):
        inc._write_v2(fp, _bars().drop(columns=["amount"]))
    inc._write_v2(fp, _bars())
    assert _date_type(fp) == "date32[day]"
    assert [p.name for p in tmp_path.iterdir()] == ["sh600000.parquet"]


def test_process_one_writes_date32_derived(tmp_path, monkeypatch):
    from tdxfeed.precompute import indicators as ind

    daily = tmp_path / "parquet" / "daily"
    daily.mkdir(parents=True)
    derived = tmp_path / "parquet" / "derived"
    monkeypatch.setattr(ind, "DAILY_DIR", daily)
    monkeypatch.setattr(ind, "DERIVED_DIR", derived)
    _bars().to_parquet(daily / "sh600000.parquet", index=False)   # 故意写成 timestamp[ns]

    out = ind.process_one("sh600000")
    assert out is not None and out.name == "sh600000_indicators.parquet"
    sch = pq.read_schema(out)
    assert len(sch.names) == 29
    assert str(sch.field("date").type) == "date32[day]"
