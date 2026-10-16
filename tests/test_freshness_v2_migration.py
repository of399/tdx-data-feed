"""freshness.py 与 migrate_v1_to_v2.py 的 v2 迁移集成测试。"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================================
# Helpers
# ============================================================================
def _write_int_v1_parquet(fp: Path, dates_as_int: list[int]) -> None:
    """写一个 v1 schema parquet：date 列是 int64 YYYYMMDD（v1 历史格式）。"""
    df = pd.DataFrame({
        "date": dates_as_int,
        "open": [1.0] * len(dates_as_int),
        "high": [2.0] * len(dates_as_int),
        "low": [0.5] * len(dates_as_int),
        "close": [1.5] * len(dates_as_int),
        "volume": [1000.0] * len(dates_as_int),
        "amount": [100.0] * len(dates_as_int),
    })
    df.to_parquet(fp, index=False, engine="pyarrow")


def _write_date32_v2_parquet(fp: Path, dates: list[date]) -> None:
    """写一个 v2 schema parquet：date 列是 date32[day]。"""
    df = pd.DataFrame({
        "date": dates,
        "open": [1.0] * len(dates),
        "high": [2.0] * len(dates),
        "low": [0.5] * len(dates),
        "close": [1.5] * len(dates),
        "volume": [1000.0] * len(dates),
        "amount": [100.0] * len(dates),
    })
    arr = pa.array(dates, type=pa.date32())
    table = pa.Table.from_pandas(df, preserve_index=False)
    idx = table.schema.get_field_index("date")
    table = table.set_column(idx, "date", arr)
    pq.write_table(table, str(fp), compression="snappy")


@pytest.fixture
def tmp_daily_dir(tmp_path: Path, monkeypatch) -> Path:
    """为 freshness / migrate 测试创建临时 parquet 目录。"""
    daily = tmp_path / "parquet" / "daily"
    daily.mkdir(parents=True)
    from tdxfeed import freshness
    monkeypatch.setattr(freshness, "DEFAULT_PARQUET_DIR", daily)
    return daily


@pytest.fixture
def fresh_migrate():
    """每次重新加载 migrate_v1_to_v2.py（main 调 sys.exit，必须用 cmd_* 直接调）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_v1_to_v2", ROOT / "scripts" / "migrate_v1_to_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Args:
    """简单 namespace 替代 argparse.Namespace。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ============================================================================
# freshness.py 测试
# ============================================================================
def test_freshness_excludes_v1_bak(tmp_daily_dir: Path, monkeypatch):
    """freshness 必须排除 .v1.bak.parquet 备份，否则会被误判为过期。"""
    from tdxfeed import freshness

    today = date(2026, 10, 15)
    monkeypatch.setattr(freshness, "_today", lambda: today)
    monkeypatch.setattr(freshness, "CN_HOLIDAYS", set())

    # 4 个 active 全部为 v2 date32 末日期 = today
    for i in range(4):
        _write_date32_v2_parquet(tmp_daily_dir / f"sh{600000 + i:06d}.parquet",
                                 [today - timedelta(days=10), today])

    # 6 个 .v1.bak 末日期 = 5 天前（int YYYYMMDD 合法但过期）
    for i in range(6):
        old_int = int((today - timedelta(days=5)).strftime("%Y%m%d"))
        _write_int_v1_parquet(tmp_daily_dir / f"sh{600100 + i:06d}.v1.bak.parquet",
                              [20260101, old_int])

    # require_fresh 不应 sys.exit
    try:
        freshness.require_fresh(parquet_dir=tmp_daily_dir, max_age_days=2, sample_size=10)
    except SystemExit as e:
        pytest.fail(f"freshness 误判 .v1.bak 为过期并 sys.exit({e.code})")


def test_freshness_glob_excludes_bak(tmp_daily_dir: Path):
    """glob 内部必须过滤 .bak（与 predict.py:419 / check_schema.py:37-38 对齐）。"""
    today = date(2026, 10, 15)
    _write_date32_v2_parquet(tmp_daily_dir / "sh600000.parquet",
                             [today - timedelta(days=10), today])
    _write_int_v1_parquet(tmp_daily_dir / "sh600000.v1.bak.parquet", [20261010])

    from tdxfeed import freshness
    files = [p for p in freshness.DEFAULT_PARQUET_DIR.glob("*.parquet")
             if ".bak" not in p.name and freshness._is_a_share_stock(p.name)]
    assert len(files) == 1
    assert files[0].name == "sh600000.parquet"


def test_freshness_excludes_etf_and_bj(tmp_daily_dir: Path):
    """A 股过滤：ETF/LOF/基金（159/51/56 开头）+ 北交所（920/8 开头）应被剔除。"""
    from tdxfeed import freshness

    assert freshness._is_a_share_stock("sh600000.parquet") is True
    assert freshness._is_a_share_stock("600000.parquet") is True
    assert freshness._is_a_share_stock("sz000001.parquet") is True
    assert freshness._is_a_share_stock("sz300750.parquet") is True
    assert freshness._is_a_share_stock("sh688981.parquet") is True

    # 非 A 股个股
    assert freshness._is_a_share_stock("sz159748.parquet") is False   # LOF
    assert freshness._is_a_share_stock("sh513000.parquet") is False   # 上交所 ETF
    assert freshness._is_a_share_stock("sh560900.parquet") is False   # 基金
    assert freshness._is_a_share_stock("bj920026.parquet") is False   # 北交所
    assert freshness._is_a_share_stock("bj830xxx.parquet") is False   # 北交所
    assert freshness._is_a_share_stock("sh900901.parquet") is True    # 沪 B 股 仍属 A 股 universe


def test_freshness_passes_with_etf_excluded(tmp_daily_dir: Path, monkeypatch):
    """核心验证：A 股 + .v1.bak 双过滤后，即便 ETF/北交所陈旧也能通过。"""
    from tdxfeed import freshness

    today = date(2026, 10, 15)
    monkeypatch.setattr(freshness, "_today", lambda: today)
    monkeypatch.setattr(freshness, "CN_HOLIDAYS", set())

    # 30 个 A 股 active 全部新鲜
    for i in range(30):
        _write_date32_v2_parquet(tmp_daily_dir / f"sh{600000 + i:06d}.parquet",
                                 [today - timedelta(days=10), today])

    # 20 个 ETF/北交所 真陈旧（应被 A 股过滤剔除）
    for i in range(10):
        _write_date32_v2_parquet(tmp_daily_dir / f"sz{159000 + i:03d}.parquet",
                                 [date(2026, 9, 14), date(2026, 9, 14)])
    for i in range(10):
        _write_date32_v2_parquet(tmp_daily_dir / f"bj{920000 + i:03d}.parquet",
                                 [date(2026, 9, 14), date(2026, 9, 14)])

    # 应通过
    freshness.require_fresh(parquet_dir=tmp_daily_dir, max_age_days=2, sample_size=50)


def test_freshness_still_rejects_actually_stale(tmp_daily_dir: Path, monkeypatch):
    """freshness 仍要能识别真正陈旧的 active 文件。"""
    from tdxfeed import freshness

    today = date(2026, 10, 15)
    monkeypatch.setattr(freshness, "_today", lambda: today)
    monkeypatch.setattr(freshness, "CN_HOLIDAYS", set())

    # 10 个 active 末日期 30 天前（真陈旧）
    for i in range(10):
        old = today - timedelta(days=30)
        _write_date32_v2_parquet(tmp_daily_dir / f"sz{300000 + i:06d}.parquet",
                                 [old - timedelta(days=10), old])

    with pytest.raises(SystemExit) as exc_info:
        freshness.require_fresh(parquet_dir=tmp_daily_dir, max_age_days=2, sample_size=10)
    assert exc_info.value.code == 2


# ============================================================================
# migrate_v1_to_v2.py 测试
# ============================================================================
def test_migrate_converts_int_to_date32(fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """migrate --apply：int YYYYMMDD 旧文件应被转为 date32，备份为 .v1.bak。"""
    fp = tmp_daily_dir / "sh688818.parquet"
    _write_int_v1_parquet(fp, [20260101, 20260102, 20260105])

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    rc = fresh_migrate.cmd_migrate(_Args(apply=True, limit=0))
    assert rc == 0

    schema = pq.read_schema(fp)
    date_field = next(f for f in schema if f.name == "date")
    assert str(date_field.type) == "date32[day]"

    bak = tmp_daily_dir / "sh688818.v1.bak.parquet"
    assert bak.exists()
    bak_schema = pq.read_schema(bak)
    bak_date = next(f for f in bak_schema if f.name == "date")
    assert str(bak_date.type) in ("int64", "int32")  # 备份保持原样

    assert pq.read_metadata(fp).num_rows == pq.read_metadata(bak).num_rows == 3


def test_migrate_dry_run_no_write(fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """migrate（无 --apply）不应改盘。"""
    fp = tmp_daily_dir / "sh688818.parquet"
    _write_int_v1_parquet(fp, [20260101])
    original_mtime = fp.stat().st_mtime

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    fresh_migrate.cmd_migrate(_Args(apply=False, limit=0))

    schema = pq.read_schema(fp)
    assert "int64" in str(next(f for f in schema if f.name == "date").type)
    assert fp.stat().st_mtime == original_mtime
    assert not (tmp_daily_dir / "sh688818.v1.bak.parquet").exists()


def test_migrate_idempotent(fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """对已 v2 合规的文件再跑 migrate 不应产生 .v1.bak。"""
    today = date(2026, 10, 15)
    fp = tmp_daily_dir / "sh600000.parquet"
    _write_date32_v2_parquet(fp, [today])

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    fresh_migrate.cmd_migrate(_Args(apply=True, limit=0))
    assert not (tmp_daily_dir / "sh600000.v1.bak.parquet").exists()


def test_cleanup_safety_gate_blocks_when_active_not_compliant(
        fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """闸门 1：active 不全 v2 → cleanup 拒绝。"""
    today = date(2026, 10, 15)
    _write_int_v1_parquet(tmp_daily_dir / "sh688818.parquet", [20260101, int(today.strftime("%Y%m%d"))])
    bak = tmp_daily_dir / "sh688818.v1.bak.parquet"
    _write_int_v1_parquet(bak, [20260101, 20261010])
    # bak 设 100 天前 mtime
    old_ts = (datetime(2026, 10, 15) - timedelta(days=100)).timestamp()
    os.utime(bak, (old_ts, old_ts))

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    rc = fresh_migrate.cmd_cleanup(_Args(apply=True))
    assert rc == 2
    assert bak.exists()

    # 把 active 也归一为 v2，再试
    _write_date32_v2_parquet(tmp_daily_dir / "sh688818.parquet", [date(2026, 1, 1), today])
    rc = fresh_migrate.cmd_cleanup(_Args(apply=True))
    assert rc == 0
    assert not bak.exists()


def test_cleanup_safety_gate_blocks_when_too_young(fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """闸门 2：v1.bak 太新（< 30 天）→ cleanup 拒绝。"""
    today = date(2026, 10, 15)
    _write_date32_v2_parquet(tmp_daily_dir / "sh688818.parquet", [today])
    bak = tmp_daily_dir / "sh688818.v1.bak.parquet"
    _write_int_v1_parquet(bak, [20260101])
    os.utime(bak, None)  # mtime = now, age = 0d

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    rc = fresh_migrate.cmd_cleanup(_Args(apply=True))
    assert rc == 2
    assert bak.exists()


def test_cleanup_dry_run_lists_without_deleting(fresh_migrate, tmp_daily_dir: Path, monkeypatch):
    """cleanup（无 --apply）应列出但不删。"""
    today = date(2026, 10, 15)
    _write_date32_v2_parquet(tmp_daily_dir / "sh688818.parquet", [today])
    bak = tmp_daily_dir / "sh688818.v1.bak.parquet"
    _write_int_v1_parquet(bak, [20260101])
    old_ts = (datetime(today.year, today.month, today.day) - timedelta(days=100)).timestamp()
    os.utime(bak, (old_ts, old_ts))

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    rc = fresh_migrate.cmd_cleanup(_Args(apply=False))
    assert rc == 0
    assert bak.exists()  # dry-run 不删


def test_status_runs_and_returns_zero(fresh_migrate, tmp_daily_dir: Path, monkeypatch, capsys):
    """status 子命令应成功执行并打印关键统计。"""
    today = date(2026, 10, 15)
    for i in range(3):
        _write_date32_v2_parquet(tmp_daily_dir / f"sz{300000 + i:06d}.parquet", [today])
    _write_int_v1_parquet(tmp_daily_dir / "sh600000.v1.bak.parquet", [20261010])
    old_ts = (datetime(today.year, today.month, today.day) - timedelta(days=40)).timestamp()
    os.utime(tmp_daily_dir / "sh600000.v1.bak.parquet", (old_ts, old_ts))

    monkeypatch.setattr(fresh_migrate, "DAILY_DIR", tmp_daily_dir)
    rc = fresh_migrate.cmd_status(None)
    assert rc == 0

    captured = capsys.readouterr().out
    assert "active" in captured
    assert "v2 合规" in captured
    assert "v1.bak 备份" in captured
