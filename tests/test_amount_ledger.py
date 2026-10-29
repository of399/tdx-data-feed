"""估算成交额的旁路清单：记录、撤销、原子落盘，且不改 daily 表 schema。"""
import json
from datetime import date

import pandas as pd

from tdxfeed.amount_fix import (backfill_daily_dir, estimated_dates,
                                fill_missing_amount, fill_missing_amount_marked,
                                flush_estimated, ledger_path_for, load_estimated,
                                record_estimated)
from tdxfeed.writers import Writers

NEEDS_DATA = False


def _bars(rows):
    return [list(r) for r in rows]


def test_marked_matches_legacy_api():
    df = pd.DataFrame({
        "date": [20260105, 20260106, 20260107],
        "open": [10.0, 10.0, 10.0],
        "high": [11.0, 11.0, 11.0],
        "low": [9.0, 9.0, 9.0],
        "close": [10.0, 20.0, 30.0],
        "volume": [100.0, 0.0, 200.0],   # 第二行停牌，不该补
        "amount": [0.0, 0.0, 5.0],       # 第三行已有真实成交额
    })
    legacy, n_legacy = fill_missing_amount(df)
    marked, n_marked, est = fill_missing_amount_marked(df)
    assert n_legacy == n_marked == 1
    pd.testing.assert_frame_equal(legacy, marked)
    assert list(est) == [True, False, False]


def test_merge_write_records_estimate_then_real_amount_clears_it(tmp_path):
    ledger = ledger_path_for(tmp_path)
    w = Writers(str(tmp_path))
    w.merge_write("sh600000", _bars([
        ["2026-01-05", 10.0, 11.0, 9.0, 10.0, 100.0, 0.0],
        ["2026-01-06", 10.0, 11.0, 9.0, 10.0, 200.0, 210000.0],
    ]))

    daily = tmp_path / "parquet" / "daily" / "sh600000.parquet"
    df = pd.read_parquet(daily)
    assert list(df.columns) == ["date", "open", "high", "low", "close",
                               "volume", "amount"]
    est_row = df[df["date"] == date(2026, 1, 5)].iloc[0]
    assert float(est_row["amount"]) == 10.0 * 100.0 * 100.0
    assert estimated_dates("sh600000", ledger) == {20260105}

    flush_estimated(ledger)
    assert load_estimated(ledger) == {"sh600000": [20260105]}
    assert list((tmp_path / ".state").glob("*.tmp")) == []

    # 源后来给出真实成交额：该日期的估算标记撤销，表值被真实值替换
    w.merge_write("sh600000", _bars([
        ["2026-01-05", 10.0, 11.0, 9.0, 10.0, 100.0, 101000.0],
    ]))
    assert estimated_dates("sh600000", ledger) == set()
    flush_estimated(ledger)
    assert load_estimated(ledger) == {}
    df = pd.read_parquet(daily)
    real_row = df[df["date"] == date(2026, 1, 5)].iloc[0]
    assert float(real_row["amount"]) == 101000.0


def test_flush_keeps_symbols_written_by_another_process(tmp_path):
    ledger = ledger_path_for(tmp_path)
    record_estimated("sh600000", [20260106], ledger)
    # 缓冲建立之后另一进程落盘：flush 必须重读磁盘，不能整份覆盖
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"sz000001": [20260105]}), encoding="utf-8")

    flush_estimated(ledger)
    assert load_estimated(ledger) == {
        "sh600000": [20260106],
        "sz000001": [20260105],
    }


def test_ledger_flushes_on_process_exit(tmp_path):
    """缓冲靠 atexit 落盘：进程里只调 merge_write，退出后清单必须已在盘上。"""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    script = (
        "from tdxfeed.writers import Writers\n"
        f"Writers({str(tmp_path)!r}).merge_write('sh600000', "
        "[['20260105', 10.0, 11.0, 9.0, 10.0, 100.0, 0.0]])\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=str(repo), check=True)
    assert load_estimated(ledger_path_for(tmp_path)) == {"sh600000": [20260105]}


def test_backfill_records_estimated_rows(tmp_path):
    daily = tmp_path / "parquet" / "daily"
    daily.mkdir(parents=True)
    frame = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")],
        "open": [10.0, 10.0], "high": [11.0, 11.0],
        "low": [9.0, 9.0], "close": [10.0, 10.0],
        "volume": [100.0, 200.0], "amount": [0.0, 0.0],
    })
    Writers._write_parquet_date32(str(daily / "sh600000.parquet"), frame)

    stats = backfill_daily_dir(daily, workers=1, out_root=tmp_path,
                               update_day=True)
    assert stats["filled_rows"] == 2
    assert estimated_dates("sh600000", ledger_path_for(tmp_path)) == {
        20260105, 20260106}
    assert (tmp_path / "sh" / "lday" / "sh600000.day").exists()
