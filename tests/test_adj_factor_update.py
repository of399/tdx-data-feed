"""复权因子增量合并：不联网，只测纯逻辑与本地文件写入。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tdxfeed.adj_factor import COLUMNS, bs_code, last_date, merge_rows, update_one, write_atomic


def _old_df():
    # 与现存文件同 schema：date 是 datetime.date → pyarrow date32[day]
    return pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), 1.0, 8.98, 8.98],
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
        ],
        columns=COLUMNS,
    )


def test_bs_code_mapping():
    assert bs_code("600000") == "sh.600000"
    assert bs_code("688001") == "sh.688001"
    assert bs_code("000001") == "sz.000001"
    assert bs_code("300750") == "sz.300750"
    assert bs_code("920001") == "bj.920001"
    assert bs_code("830799") == "bj.830799"


def test_merge_rows_appends_and_dedupes():
    qfq = [["2026-09-25", "9.10"], ["2026-09-28", "9.16"], ["2026-09-24", "9.00"]]
    raw = [["2026-09-25", "9.10"], ["2026-09-28", "9.16"], ["2026-09-24", "9.00"]]
    out = merge_rows(_old_df(), qfq, raw)
    assert out is not None
    assert [str(d) for d in out["date"]] == ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]
    assert len(out) == 4
    assert out.iloc[-1]["adj_factor"] == 1.0


def test_merge_rows_returns_none_without_overlap():
    assert merge_rows(_old_df(), [], []) is None
    assert merge_rows(_old_df(), [["2026-09-25", "9.10"]], []) is None


def test_merge_rows_drops_non_positive_raw():
    qfq = [["2026-09-25", "0"]]
    raw = [["2026-09-25", "0"]]
    assert merge_rows(_old_df(), qfq, raw) is None


def test_update_one_skips_when_already_today(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    assert update_one(object(), "600000", tmp_path, "2026-09-24") == "skip"


def test_update_one_writes_atomically(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append((code, start, end, flag))
        return [["2026-09-25", "9.10"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "ok:+1"
    assert calls[0][1] == "2026-09-25"
    assert calls[0][3] == "2"
    assert last_date(p) == "2026-09-25"
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
    assert not list(tmp_path.glob("*.parquet.tmp*"))


def test_write_atomic_leaves_no_parquet_tmp(tmp_path):
    p = tmp_path / "600000.parquet"
    write_atomic(p, _old_df())
    assert p.exists()
    assert [x.name for x in tmp_path.iterdir()] == ["600000.parquet"]


def test_write_atomic_preserves_date32(tmp_path):
    import pyarrow.parquet as pq

    p = tmp_path / "600000.parquet"
    write_atomic(p, _old_df())
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
