"""行情表写盘只有一个入口：tdxfeed.parquet_io.write_table（date32 + 原子替换）。"""
from __future__ import annotations

import json
import re
from datetime import datetime
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


def test_cli_sync_goes_through_merge_write():
    """`cli sync` 只能走 merge_write：write_parquet 会截断全历史且就地覆盖。"""
    src = (ROOT / "tdxfeed/cli.py").read_text(encoding="utf-8")
    assert "merge_write(" in src
    assert "write_parquet(" not in src
    assert ".to_parquet(" not in src


def test_writers_keeps_no_parquet_bypass():
    """write_parquet 是下一个入口陷阱：writers.py 里不该再有 to_parquet。"""
    src = (ROOT / "tdxfeed/writers.py").read_text(encoding="utf-8")
    assert "to_parquet" not in src


def _seed_daily(daily: Path) -> Path:
    """造 10 行历史（2026-01-01..01-10，date32），close = 10.1..11.0。"""
    from tdxfeed.parquet_io import write_table

    daily.mkdir(parents=True, exist_ok=True)
    fp = daily / "sh600000.parquet"
    n = 10
    write_table(fp, pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": [10.0 + i for i in range(n)],
        "high": [10.5 + i for i in range(n)],
        "low": [9.5 + i for i in range(n)],
        "close": [10.1 + i for i in range(n)],
        "volume": [1e6] * n,
        "amount": [1e7] * n,
    }))
    return fp


class _StubSource:
    """三个源统一返回 5 根新 bars（2026-01-06..01-10，close = 26.1..30.1）。

    日期用 datetime，与 SinaSource.normalize_date 的返回值同型——缺陷里正是它
    被 to_parquet 写成字符串列。
    """

    def fetch_bars(self, symbol, start_date=None, end_date=None):
        return [[datetime(2026, 1, d), 20.0 + d, 20.5 + d, 19.5 + d, 20.1 + d,
                 2e6, 2e7] for d in range(6, 11)]


def test_cmd_sync_merges_into_existing_history(tmp_path, monkeypatch, capsys):
    """cmd_sync 必须合并写：5 根新 bars 不截断 10 行历史，重叠日期取新值。"""
    import tdxfeed.cli as cli

    fp = _seed_daily(tmp_path / "parquet" / "daily")
    monkeypatch.setenv("TDX_OUT", str(tmp_path))
    for name in ("SinaSource", "AkshareSource", "TencentSource"):
        monkeypatch.setattr(cli, name, _StubSource)

    cli.cmd_sync("sh600000")

    stdout = capsys.readouterr().out
    assert "sync ok" in stdout, stdout   # cmd_sync 吞异常只 print，失败得看得见

    df = pd.read_parquet(fp)
    assert len(df) == 10, df["date"].tolist()          # 不是 5（覆盖）、也不是 15（追加）
    assert _date_type(fp) == "date32[day]"

    got = df.assign(d=pd.to_datetime(df["date"])).set_index("d")
    assert got.index.min() == pd.Timestamp("2026-01-01")   # 回填的全历史没被截断
    assert got.loc["2026-01-08", "close"] == pytest.approx(28.1)   # 重叠日取新值

    day_fp = tmp_path / "sh" / "lday" / "sh600000.day"
    assert day_fp.stat().st_size == 10 * 32   # .day 与 parquet 同源重建

    # manifest 的 count 是 cmd_status 直接打印的「条数」，必须等于合并后的
    # 行数（10），不是本次拉取的根数（5），否则与文件实际行数分叉。
    manifest = json.loads((tmp_path / ".state" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sh600000"]["count"] == 10


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
