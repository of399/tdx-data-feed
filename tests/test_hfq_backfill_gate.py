"""回填器的三道门禁与幂等：零网络，baostock 用假客户端注入。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_hfq_history as bh  # noqa: E402


class _RS:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    def next(self):
        if self._i >= len(self._rows):
            return False
        self._i += 1
        return True

    def get_row_data(self):
        return self._rows[self._i - 1]


class FakeBS:
    """只实现回填器用到的两个调用：历史查询与代码是否存在。"""

    def __init__(self, raw, hfq, in_source=True):
        self.rows = {"1": hfq, "3": raw}
        self.in_source = in_source

    def query_history_k_data_plus(self, code, fields, start_date, end_date,
                                  frequency, adjustflag):
        return _RS(self.rows[adjustflag])

    def query_stock_basic(self, code=""):
        return _RS([["sh.600000"]] if self.in_source else [])


def _old_frame(dates, raw, hfq):
    return pd.DataFrame({
        "code": ["600000"] * len(dates),
        "date": [pd.Timestamp(d).date() for d in dates],
        "hfq_factor": [h / r for h, r in zip(hfq, raw, strict=True)],
        "raw_close": raw,
        "hfq_close": hfq,
    })


def _write(tmp_path, dates, raw, hfq):
    out = tmp_path / "hfq"
    out.mkdir(exist_ok=True)
    _old_frame(dates, raw, hfq).to_parquet(out / "600000.parquet", index=False)
    return out


def test_raw_overlap_error_rejects_with_zero_writes(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03"], [10.0, 10.0], [10.0, 10.0])
    before = (out / "600000.parquet").read_bytes()
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.2"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.2"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("reject:raw-overlap-err"), r
    assert (out / "600000.parquet").read_bytes() == before
    assert not (tmp_path / "bak" / "600000.parquet").exists()


def test_hfq_overlap_error_rebases_and_takes_new_segment(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03"], [10.0, 10.0], [10.0, 10.0])
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.5"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("rebase:"), r
    df = pd.read_parquet(out / "600000.parquet")
    assert float(df["hfq_close"].iloc[-1]) == 10.5
    assert (tmp_path / "bak" / "600000.parquet").exists()


def test_shorter_source_segment_never_drops_rows(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03", "2020-01-06"],
                 [10.0, 10.0, 10.0], [10.0, 10.0, 10.0])
    bs = FakeBS(raw=[["2020-01-06", "10.0"]], hfq=[["2020-01-06", "10.0"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("ok:"), r
    assert len(pd.read_parquet(out / "600000.parquet")) == 3


def test_empty_source_is_classified_by_basic_info(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    assert bh.backfill_one(FakeBS([], [], in_source=False), "600000", out,
                           "2026-10-30", tmp_path / "bak") == "not-in-source"
    assert bh.backfill_one(FakeBS([], [], in_source=True), "600000", out,
                           "2026-10-30", tmp_path / "bak") == "fail:empty-source"


def test_written_date_column_is_date32(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]])
    bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert str(pq.read_schema(out / "600000.parquet").field("date").type) == "date32[day]"


def test_plan_codes_skips_done_and_not_in_source(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    state = {"done": ["600000"], "rejected": {}, "not_in_source": ["600001"], "rebased": {}}
    assert bh.plan_codes(out, state) == []


def test_state_roundtrip_keeps_four_fields(tmp_path):
    p = tmp_path / "state.json"
    st = {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    st["done"].append("600000")
    bh.save_state(st, p)
    assert json.loads(p.read_text(encoding="utf-8"))["done"] == ["600000"]
    assert set(bh.load_state(p)) == {"done", "rejected", "not_in_source", "rebased"}


def test_mark_state_is_idempotent():
    st = {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    bh._mark(st, "done", "600000")
    bh._mark(st, "done", "600000")
    bh._mark(st, "rejected", "600001", "raw-overlap-err=1.0000%")
    assert st["done"] == ["600000"]
    assert st["rejected"] == {"600001": "raw-overlap-err=1.0000%"}
