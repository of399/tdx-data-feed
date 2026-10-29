"""复权因子增量合并：不联网，只测纯逻辑与本地文件写入。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tdxfeed.adj_factor import (
    COLUMNS,
    bs_code,
    exit_code,
    last_date,
    merge_rows,
    update_one,
    write_atomic,
)


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
    # 窗口从 last_date 当日起（含重叠日），不是次日起
    assert calls[0][1] == "2026-09-24"
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


def test_update_one_rebases_when_overlap_qfq_changed(tmp_path, monkeypatch):
    """重叠日 qfq_close 变了 = 除权重算了基准，旧段必须整表按新基准重拉。"""
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    series = {
        "2026-09-24": (["2026-09-24", "8.70"], ["2026-09-24", "9.00"]),
        "2026-09-23": (
            [["2026-09-23", "8.70"], ["2026-09-24", "8.70"], ["2026-09-25", "8.80"]],
            [["2026-09-23", "8.98"], ["2026-09-24", "9.00"], ["2026-09-25", "9.10"]],
        ),
    }

    def fake_query(bs, code, start, end, flag):
        qfq, raw = series[start]
        if start != "2026-09-23":
            # 增量窗口：重叠日 09-24 的 qfq 已由 9.00 变成 8.70
            qfq = [qfq, ["2026-09-25", "8.80"]]
            raw = [raw, ["2026-09-25", "9.10"]]
        return qfq if flag == "2" else raw

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("rebase:+")
    out = pd.read_parquet(p)
    assert len(out) == 3, "重拉是整表替换，不是叠加"
    row = out[out["date"].astype(str) == "2026-09-24"].iloc[0]
    assert row["qfq_close"] == 8.70, "旧段 09-24 也按新基准"
    assert abs(row["adj_factor"] - round(8.70 / 9.00, 6)) < 1e-9
    assert out[out["date"].astype(str) == "2026-09-23"].iloc[0]["qfq_close"] == 8.70


def test_update_one_rebases_on_mixed_internal_base(tmp_path, monkeypatch):
    """首轮只追加不回算留下的接缝：重叠日没变，但旧表后段因子比前段小 = 两套基准。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
            ["600000", date(2026, 9, 25), 0.96, 9.10, 9.48],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append(start)
        if start == "2026-09-24":
            return (
                [["2026-09-24", "8.64"], ["2026-09-25", "9.10"]]
                if flag == "2"
                else [["2026-09-24", "9.00"], ["2026-09-25", "9.48"]]
            )
        return [["2026-09-25", "9.10"]] if flag == "2" else [["2026-09-25", "9.48"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-28").startswith("rebase:+")
    assert "2026-09-24" in calls
    out = pd.read_parquet(p)
    assert abs(out["adj_factor"].iloc[0] - 8.64 / 9.00) < 1e-6
    assert abs(out["adj_factor"].iloc[1] - 9.10 / 9.48) < 1e-6


def test_rebase_query_starts_from_file_first_date(tmp_path, monkeypatch):
    """整表重拉起点是文件自身首行日期，不是写死的常量。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2024, 3, 15), 1.0, 7.00, 7.00],
            ["600000", date(2026, 9, 25), 0.96, 9.10, 9.48],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append(start)
        if start == "2024-03-15":
            return (
                [["2024-03-15", "6.72"], ["2026-09-25", "9.10"]]
                if flag == "2"
                else [["2024-03-15", "7.00"], ["2026-09-25", "9.48"]]
            )
        return [["2026-09-25", "9.10"]] if flag == "2" else [["2026-09-25", "9.48"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-28").startswith("rebase:+")
    assert "2024-03-15" in calls, "重拉起点应取旧表首行日期"
    assert "2020-01-01" not in calls, "不应再用写死的常量起点"


def test_update_one_refuses_shrunken_rebase(tmp_path, monkeypatch):
    """源只回了部分历史时整表替换会静默截断，必须放弃本轮且不动原文件。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 21), 1.0, 9.00, 9.00],
            ["600000", date(2026, 9, 22), 1.0, 9.02, 9.02],
            ["600000", date(2026, 9, 23), 1.0, 9.05, 9.05],
            ["600000", date(2026, 9, 24), 0.96, 9.10, 9.48],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    before = pd.read_parquet(p)
    import tdxfeed.adj_factor as m

    def fake_query(bs, code, start, end, flag):
        if start == "2026-09-21":            # 整表重拉：源只回了 2 行
            return (
                [["2026-09-21", "8.64"], ["2026-09-24", "9.10"]]
                if flag == "2"
                else [["2026-09-21", "9.00"], ["2026-09-24", "9.48"]]
            )
        return [["2026-09-24", "9.10"]] if flag == "2" else [["2026-09-24", "9.48"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-28") == "fail:shrink"
    after = pd.read_parquet(p)
    assert len(after) == len(before) == 4
    assert str(after["date"].iloc[-1]) == "2026-09-24"
    pd.testing.assert_frame_equal(after, before)
    assert not list(tmp_path.glob("*.tmp*")), "失败路径不该留临时文件"


def test_update_one_fails_when_only_one_series_empty(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    def fake_query(bs, code, start, end, flag):
        return [["2026-09-25", "9.10"]] if flag == "2" else []

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "fail:partial-empty"


def test_update_one_skips_when_both_series_empty(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: [])
    assert update_one(object(), "600000", tmp_path, "2026-09-27") == "skip"


def test_update_one_fails_when_window_has_trading_day_but_source_empty(tmp_path, monkeypatch):
    """窗口里有交易日却两条都空 = 源异常，记 skip 会让全市场静默退 0。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 10, 22), 1.0, 9.00, 9.00],
            ["600000", date(2026, 10, 23), 1.0, 9.10, 9.10],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: [])
    assert update_one(object(), "600000", tmp_path, "2026-10-27") == "fail:empty-window"


def test_update_one_skips_when_window_is_all_holidays(tmp_path, monkeypatch):
    """国庆休市 + 周末：(2026-09-30, 2026-10-05] 没有交易日，空响应是真 skip。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [["600000", date(2026, 9, 30), 1.0, 9.10, 9.10]],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: [])
    assert update_one(object(), "600000", tmp_path, "2026-10-05") == "skip"


def test_update_one_reports_bad_parquet(tmp_path):
    p = tmp_path / "600000.parquet"
    p.write_bytes(b"not a parquet")
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("fail:")


def test_exit_code():
    # --code / --limit 试跑：一只都不许失败
    assert exit_code(0, 0, 1) == 1
    assert exit_code(9, 0, 1) == 1
    # 一只都没跑（目录空、清单空）也算失败
    assert exit_code(0, 0, 0) == 1
    # 全量：容忍 max(20, 2%)
    assert exit_code(5210, 11, 0) == 0
    assert exit_code(5100, 0, 100) == 0
    assert exit_code(5100, 0, 121) == 1


def _fake_bs(monkeypatch, logins):
    """把 main() 里的 `import baostock as bs` 换成可计数的假模块。"""
    import sys
    import types

    class FakeBS:
        @staticmethod
        def login():
            logins.append(1)
            return types.SimpleNamespace(error_code="0", error_msg="success")

        @staticmethod
        def logout():
            pass

    monkeypatch.setitem(sys.modules, "baostock", FakeBS)


def test_main_relogins_once_when_session_lost(tmp_path, monkeypatch):
    """会话在长跑中会掉（10001001/10002007）：重连后重试同一只，而不是记失败。"""
    import tdxfeed.adj_factor as m

    logins: list[int] = []
    _fake_bs(monkeypatch, logins)
    monkeypatch.setattr(m, "adj_factor_dir", lambda: tmp_path)
    results = iter([
        "fail:RuntimeError:baostock 10001001: 用户未登录",
        "ok:+3",
    ])
    monkeypatch.setattr(m, "update_one", lambda *a, **k: next(results))

    assert m.main(["--code", "600000", "--today", "2026-10-28"]) == 0
    assert len(logins) == 2


def test_main_does_not_relogin_on_data_defects(tmp_path, monkeypatch):
    """shrink / partial-empty 是数据缺陷不是会话问题，重连救不了。"""
    import tdxfeed.adj_factor as m

    logins: list[int] = []
    _fake_bs(monkeypatch, logins)
    monkeypatch.setattr(m, "adj_factor_dir", lambda: tmp_path)
    monkeypatch.setattr(m, "update_one", lambda *a, **k: "fail:shrink")

    assert m.main(["--code", "600000", "--today", "2026-10-28"]) == 1
    assert len(logins) == 1
