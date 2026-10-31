"""amount 缺口修复测试。

背景：Sina 日 K 不返回成交额，full_sync 写入的新日期 amount=0；
用 close × volume(手) × 100 估算，且不得覆盖已有的真实成交额。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tdxfeed.amount_fix import backfill_daily_dir, fill_missing_amount
from tdxfeed.writers import Writers


def test_fill_missing_amount_estimates_only_zero_rows():
    df = pd.DataFrame({
        "date": [20260916, 20260917, 20260918],
        "close": [10.0, 20.0, 30.0],
        "volume": [100.0, 0.0, 50.0],
        "amount": [0.0, 0.0, 123.0],
    })
    out, n = fill_missing_amount(df)
    assert n == 1
    assert out.loc[0, "amount"] == pytest.approx(10.0 * 100 * 100)
    assert out.loc[1, "amount"] == 0.0
    assert out.loc[2, "amount"] == 123.0


def test_fill_missing_amount_does_not_touch_positive_values():
    df = pd.DataFrame({
        "date": [20260916],
        "close": [10.0],
        "volume": [100.0],
        "amount": [1.0],
    })
    out, n = fill_missing_amount(df)
    assert n == 0
    assert out.loc[0, "amount"] == 1.0


def test_fill_missing_amount_respects_start_date():
    df = pd.DataFrame({
        "date": [20260915, 20260916],
        "close": [10.0, 10.0],
        "volume": [100.0, 100.0],
        "amount": [0.0, 0.0],
    })
    out, n = fill_missing_amount(df, start_date=20260916)
    assert n == 1
    assert out.loc[0, "amount"] == 0.0
    assert out.loc[1, "amount"] == pytest.approx(10.0 * 100 * 100)


def test_merge_write_fills_amount_for_new_dates(tmp_path: Path):
    w = Writers(str(tmp_path))
    w.merge_write("sh600000", [
        [20260915, 10.0, 10.0, 10.0, 10.0, 100.0, 123456.0],
        [20260916, 10.0, 10.0, 10.0, 10.0, 100.0, 0.0],
    ])
    df = pd.read_parquet(tmp_path / "parquet/daily/sh600000.parquet")
    df = df.sort_values("date").reset_index(drop=True)
    assert str(df.loc[0, "date"])[:10] == "2026-09-15"
    assert df.loc[0, "amount"] == pytest.approx(123456.0)
    assert df.loc[1, "amount"] == pytest.approx(10.0 * 100 * 100)


def test_merge_write_keeps_real_amount_when_new_bar_is_zero(tmp_path: Path):
    w = Writers(str(tmp_path))
    w.merge_write("sh600000", [
        [20260915, 10.0, 10.0, 10.0, 10.0, 100.0, 123456.0],
    ])
    w.merge_write("sh600000", [
        [20260915, 10.0, 10.0, 10.0, 10.0, 100.0, 0.0],
    ])
    df = pd.read_parquet(tmp_path / "parquet/daily/sh600000.parquet")
    assert df.loc[0, "amount"] == pytest.approx(123456.0)


def test_interrupted_backfill_leaves_no_parquet_suffixed_residue(tmp_path: Path,
                                                                 monkeypatch):
    """进程死在 os.replace 之前时，残留不得以 .parquet 结尾——否则下一次会被当股票。"""
    daily = tmp_path / "parquet/daily"
    daily.mkdir(parents=True)
    p = daily / "sh600000.parquet"
    pd.DataFrame({
        "date": [20260916],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [100.0], "amount": [0.0],
    }).to_parquet(p, index=False)

    real_write = Writers._write_parquet_date32

    def interrupted_write(path, df):
        real_write(str(path), df)
        raise KeyboardInterrupt

    monkeypatch.setattr(Writers, "_write_parquet_date32", staticmethod(interrupted_write))
    with pytest.raises(KeyboardInterrupt):
        backfill_daily_dir(daily, update_day=False)

    residue = [q.name for q in daily.glob("*.parquet") if q.name != "sh600000.parquet"]
    assert residue == []


def test_backfill_daily_dir_updates_parquet(tmp_path: Path):
    daily = tmp_path / "parquet/daily"
    daily.mkdir(parents=True)
    p = daily / "sh600000.parquet"
    pd.DataFrame({
        "date": [20260915, 20260916],
        "open": [10.0, 10.0],
        "high": [10.0, 10.0],
        "low": [10.0, 10.0],
        "close": [10.0, 12.0],
        "volume": [100.0, 50.0],
        "amount": [123456.0, 0.0],
    }).to_parquet(p, index=False)

    stats = backfill_daily_dir(daily, update_day=False)
    out = pd.read_parquet(p).sort_values("date").reset_index(drop=True)
    assert stats["stocks"] == 1
    assert stats["filled_rows"] == 1
    assert out.loc[0, "amount"] == pytest.approx(123456.0)
    assert out.loc[1, "amount"] == pytest.approx(12.0 * 50.0 * 100.0)
