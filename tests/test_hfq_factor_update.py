"""后复权因子增量合并：不联网，只测纯逻辑与本地文件写入。

与 adj 的差异在这里钉住：后复权历史段不随除权平移，正常只追加；重叠 1 日只用来
检测源修订（hfq_close 变了才整表重拉）。没有 `_stale_base`——后复权因子内部随日期
变化（含下降）是正常形态，不能当基准漂移信号。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tdxfeed.hfq_factor import (
    BOOTSTRAP_START,
    COLUMNS,
    bs_code,
    exit_code,
    last_date,
    merge_rows,
    update_one,
    write_atomic,
)

# 本模块只用 tmp_path 造表，不读真实行情目录，无数据环境（CI）照常收集。
NEEDS_DATA = False


def _old_df():
    # 与重建后的文件同 schema：date 是 datetime.date → pyarrow date32[day]
    return pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), round(120.03577674 / 8.98, 6), 8.98, 120.03577674],
            ["600000", date(2026, 9, 24), round(120.303117 / 9.00, 6), 9.00, 120.303117],
        ],
        columns=COLUMNS,
    )


def test_columns_are_recomputable_schema():
    assert COLUMNS == ["code", "date", "hfq_factor", "raw_close", "hfq_close"]


def test_bs_code_mapping():
    assert bs_code("600000") == "sh.600000"
    assert bs_code("688001") == "sh.688001"
    assert bs_code("000001") == "sz.000001"
    assert bs_code("300750") == "sz.300750"
    assert bs_code("920001") == "bj.920001"
    assert bs_code("830799") == "bj.830799"


def test_merge_rows_appends_and_dedupes():
    hfq = [["2026-09-25", "121.0"], ["2026-09-28", "121.8"], ["2026-09-24", "120.303117"]]
    raw = [["2026-09-25", "9.05"], ["2026-09-28", "9.11"], ["2026-09-24", "9.00"]]
    out = merge_rows(_old_df(), hfq, raw)
    assert out is not None
    assert [str(d) for d in out["date"]] == [
        "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]
    assert len(out) == 4
    last = out.iloc[-1]
    assert last["hfq_close"] == 121.8
    assert last["raw_close"] == 9.11
    assert last["hfq_factor"] == round(121.8 / 9.11, 6)


def test_merge_rows_returns_none_without_overlap():
    assert merge_rows(_old_df(), [], []) is None
    assert merge_rows(_old_df(), [["2026-09-25", "121.0"]], []) is None


def test_merge_rows_drops_non_positive_raw():
    hfq = [["2026-09-25", "121.0"]]
    raw = [["2026-09-25", "0"]]
    assert merge_rows(_old_df(), hfq, raw) is None


def test_merge_rows_fills_code_when_old_is_empty():
    """整表重拉时 old 是空表，code 必须由调用方传进来，不能取 old.iloc[0]。"""
    hfq = [["2026-09-24", "120.303117"]]
    raw = [["2026-09-24", "9.00"]]
    out = merge_rows(_old_df().iloc[0:0], hfq, raw, code="600000")
    assert out is not None
    assert list(out["code"]) == ["600000"]


def test_merge_rows_refuses_legacy_qfq_schema():
    """旧表是 qfq_close 列（1/前复权口径），不能把新行拼进去。"""
    legacy = pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), 1.0, 8.98, 8.98],
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
        ],
        columns=["code", "date", "hfq_factor", "qfq_close", "hfq_close"],
    )
    hfq = [["2026-09-25", "121.0"]]
    raw = [["2026-09-25", "9.05"]]
    assert merge_rows(legacy, hfq, raw) is None


def test_update_one_skips_when_already_today(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应联网")))
    assert update_one(object(), "600000", tmp_path, "2026-09-24") == "skip"


def test_update_one_writes_atomically(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append((code, start, end, flag))
        return [["2026-09-25", "121.0"]] if flag == "1" else [["2026-09-25", "9.05"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "ok:+1"
    # 增量窗口从 last_date 当日起（含重叠日）；后复权查 1、不复权查 3
    assert calls[0][1] == "2026-09-24"
    assert calls[0][2] == "2026-09-25"
    assert calls[0][3] == "1"
    assert calls[1][3] == "3"
    assert last_date(p) == "2026-09-25"
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
    assert not list(tmp_path.glob("*.tmp*"))


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


def test_update_one_rebases_legacy_qfq_schema(tmp_path, monkeypatch):
    """旧表（qfq_close 口径）即使重叠日数值恰好相同，也必须整表重拉。

    无除权的票旧 hfq_close 就是 raw（因子 1.0），与新查询重叠日相同；
    只看数值会走追加，把 qfq_close 表和 raw_close 表拼在一起。
    """
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), 1.0, 8.98, 8.98],
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
        ],
        columns=["code", "date", "hfq_factor", "qfq_close", "hfq_close"],
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        if start == "2026-09-23":       # 整表重拉
            return (
                [["2026-09-23", "120.0"], ["2026-09-24", "120.303117"]]
                if flag == "1"
                else [["2026-09-23", "8.98"], ["2026-09-24", "9.00"]]
            )
        return [["2026-09-24", "9.00"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("rebase:+")
    out = pd.read_parquet(p)
    assert list(out.columns) == COLUMNS, "旧 qfq_close 列必须被新 schema 替换"
    assert out.iloc[-1]["hfq_close"] == 120.303117
    assert out.iloc[-1]["hfq_factor"] == round(120.303117 / 9.00, 6)


def test_update_one_rebases_when_overlap_hfq_changed(tmp_path, monkeypatch):
    """重叠日 hfq_close 变了 = 源重算了后复权序列，旧段必须整表重拉。"""
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        if start == "2026-09-23":       # 整表重拉：历史段给出修订后的口径
            return (
                [["2026-09-23", "120.80"], ["2026-09-24", "121.50"], ["2026-09-25", "122.0"]]
                if flag == "1"
                else [["2026-09-23", "8.98"], ["2026-09-24", "9.00"], ["2026-09-25", "9.05"]]
            )
        # 增量窗口：重叠日 09-24 的 hfq_close 已由 120.303117 变成 121.50
        return (
            [["2026-09-24", "121.50"], ["2026-09-25", "122.0"]]
            if flag == "1"
            else [["2026-09-24", "9.00"], ["2026-09-25", "9.05"]]
        )

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("rebase:+")
    out = pd.read_parquet(p)
    assert len(out) == 3, "重拉是整表替换，不是叠加"
    row = out[out["date"].astype(str) == "2026-09-24"].iloc[0]
    assert row["hfq_close"] == 121.50, "旧段 09-24 也按新口径"
    assert abs(row["hfq_factor"] - round(121.50 / 9.00, 6)) < 1e-9
    assert out[out["date"].astype(str) == "2026-09-23"].iloc[0]["hfq_close"] == 120.80


def test_update_one_bootstraps_missing_file(tmp_path, monkeypatch):
    """文件不存在：从 BOOTSTRAP_START 起拉全历史建表，返回 rebase:+N。

    旧脚本已废弃，缺失文件只能靠这里补；返回 empty 会让补文件的人回去用旧脚本，
    写出 adjustflag=3 当后复权的错表。
    """
    import pyarrow.parquet as pq

    import tdxfeed.hfq_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append((code, start, end, flag))
        if flag == "1":
            return [["2020-01-02", "121.95241008"], ["2026-09-24", "120.303117"],
                    ["2026-09-25", "121.0"]]
        return [["2020-01-02", "12.47"], ["2026-09-24", "9.00"],
                ["2026-09-25", "9.05"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "rebase:+3"
    assert BOOTSTRAP_START == "2020-01-01"
    # 补建只查一轮：起点是常量，不需要像整表重拉那样再按旧首行查一次
    assert calls == [
        ("sh.600000", "2020-01-01", "2026-09-25", "1"),
        ("sh.600000", "2020-01-01", "2026-09-25", "3"),
    ]
    p = tmp_path / "600000.parquet"
    out = pd.read_parquet(p)
    assert list(out.columns) == COLUMNS
    assert len(out) == 3, "缩表守卫在旧表为空时不能把新表判成缩表"
    assert str(out["date"].iloc[0]) == "2020-01-02"
    assert out.iloc[-1]["hfq_factor"] == round(121.0 / 9.05, 6)
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
    assert not list(tmp_path.glob("*.tmp*"))


def test_update_one_bootstraps_empty_table(tmp_path, monkeypatch):
    """文件在但空表（last_date 返回 None）同样走补建，不是返回 empty。"""
    import tdxfeed.hfq_factor as m

    p = tmp_path / "600000.parquet"
    pd.DataFrame(columns=COLUMNS).to_parquet(p, index=False)
    assert last_date(p) is None

    monkeypatch.setattr(
        m, "_query",
        lambda bs, code, start, end, flag: (
            [["2026-09-24", "120.303117"]] if flag == "1"
            else [["2026-09-24", "9.00"]]))
    assert update_one(object(), "600000", tmp_path, "2026-09-24") == "rebase:+1"
    assert len(pd.read_parquet(p)) == 1


def test_update_one_bootstrap_empty_source_is_empty(tmp_path, monkeypatch):
    """文件不存在且源也回空：记 empty（没东西可写），不是 fail。"""
    import tdxfeed.hfq_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: [])
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "empty"
    assert not (tmp_path / "600000.parquet").exists()


def test_rebase_query_starts_from_file_first_date(tmp_path, monkeypatch):
    """整表重拉起点是文件自身首行日期，不是写死的常量。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2020, 1, 2), 9.78, 12.47, 121.95241008],
            ["600000", date(2026, 9, 24), 13.367012, 9.00, 120.303117],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append(start)
        if start == "2020-01-02":
            return (
                [["2020-01-02", "121.95241008"], ["2026-09-24", "121.50"],
                 ["2026-09-25", "122.0"]]
                if flag == "1"
                else [["2020-01-02", "12.47"], ["2026-09-24", "9.00"],
                      ["2026-09-25", "9.05"]]
            )
        return (
            [["2026-09-24", "121.50"]] if flag == "1" else [["2026-09-24", "9.00"]]
        )

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("rebase:+")
    assert "2020-01-02" in calls, "重拉起点应取旧表首行日期"
    assert "2020-01-01" not in calls, "不应再用写死的常量起点"


def test_internal_factor_drift_is_not_a_rebase_signal(tmp_path, monkeypatch):
    """后复权因子内部下降是正常形态：只有重叠日 hfq_close 变了才整表重拉。

    这里旧表内部因子 13.5 → 13.367 明显下降，但重叠日 09-24 与新查询一致，
    因此必须走增量（ok:+1），不能像 adj 那样用 `_stale_base` 触发 rebase。
    """
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 22), 13.5, 9.00, 121.5],
            ["600000", date(2026, 9, 23), 13.4, 8.98, 120.332],
            ["600000", date(2026, 9, 24), 13.367012, 9.00, 120.303117],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        if start != "2026-09-24":
            raise AssertionError(f"不该整表重拉（起点 {start}）")
        return (
            [["2026-09-24", "120.303117"], ["2026-09-25", "121.0"]]
            if flag == "1"
            else [["2026-09-24", "9.00"], ["2026-09-25", "9.05"]]
        )

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "ok:+1"


def test_update_one_refuses_shrunken_rebase(tmp_path, monkeypatch):
    """源只回了部分历史时整表替换会静默截断，必须放弃本轮且不动原文件。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 21), 13.36, 9.00, 120.20],
            ["600000", date(2026, 9, 22), 13.37, 9.00, 120.33],
            ["600000", date(2026, 9, 23), 13.367, 8.98, 120.03577674],
            ["600000", date(2026, 9, 24), 13.367012, 9.00, 120.303117],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    before = pd.read_parquet(p)
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        if start == "2026-09-21":            # 整表重拉：源只回了 2 行
            return (
                [["2026-09-21", "121.50"], ["2026-09-24", "121.50"]]
                if flag == "1"
                else [["2026-09-21", "9.00"], ["2026-09-24", "9.00"]]
            )
        return [["2026-09-24", "121.50"]] if flag == "1" else [["2026-09-24", "9.00"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "fail:shrink"
    after = pd.read_parquet(p)
    assert len(after) == len(before) == 4
    assert str(after["date"].iloc[-1]) == "2026-09-24"
    pd.testing.assert_frame_equal(after, before)
    assert not list(tmp_path.glob("*.tmp*")), "失败路径不该留临时文件"


def test_update_one_fails_when_only_one_series_empty(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        return [["2026-09-25", "121.0"]] if flag == "1" else []

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "fail:partial-empty"


def test_update_one_fails_when_window_has_trading_day_but_source_empty(
        tmp_path, monkeypatch):
    """窗口里有交易日却两条都空 = 源异常，记 skip 会让全市场静默退 0。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 10, 22), 13.36, 9.00, 120.20],
            ["600000", date(2026, 10, 23), 13.37, 9.10, 121.67],
        ],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: [])
    assert update_one(object(), "600000", tmp_path, "2026-10-27") == "fail:empty-window"


def test_update_one_skips_when_window_is_all_holidays(tmp_path, monkeypatch):
    """国庆休市 + 周末：(2026-09-30, 2026-10-05] 没有交易日，空响应是真 skip。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [["600000", date(2026, 9, 30), 13.37, 9.10, 121.67]],
        columns=COLUMNS,
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

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


def test_needs_build_selects_missing_and_legacy(tmp_path):
    """--missing 只挑需要从零建的文件：不存在、空表、旧 qfq_close 口径。"""
    from tdxfeed.hfq_factor import needs_build

    assert needs_build(tmp_path / "nofile.parquet") is True
    _old_df().to_parquet(tmp_path / "new.parquet", index=False)
    assert needs_build(tmp_path / "new.parquet") is False
    pd.DataFrame(
        [["600000", date(2026, 9, 24), 1.0, 9.00, 9.00]],
        columns=["code", "date", "hfq_factor", "qfq_close", "hfq_close"],
    ).to_parquet(tmp_path / "legacy.parquet", index=False)
    assert needs_build(tmp_path / "legacy.parquet") is True
    _old_df().iloc[0:0].to_parquet(tmp_path / "empty.parquet", index=False)
    assert needs_build(tmp_path / "empty.parquet") is True


def test_legacy_schema_queries_full_history_directly(tmp_path, monkeypatch):
    """旧口径文件没有可用历史：第一次查询就取文件首行日期，别先查 1 天窗口再重拉。"""
    p = tmp_path / "600000.parquet"
    pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), 1.0, 8.98, 8.98],
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
        ],
        columns=["code", "date", "hfq_factor", "qfq_close", "hfq_close"],
    ).to_parquet(p, index=False)
    import tdxfeed.hfq_factor as m

    starts = []

    def fake_query(bs, code, start, end, flag):
        starts.append(start)
        if flag == "1":
            return [["2026-09-23", "120.0"], ["2026-09-24", "120.303117"]]
        return [["2026-09-23", "8.98"], ["2026-09-24", "9.00"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25").startswith("rebase:+")
    assert starts == ["2026-09-23", "2026-09-23"], starts


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
    import tdxfeed.hfq_factor as m

    logins: list[int] = []
    _fake_bs(monkeypatch, logins)
    monkeypatch.setattr(m, "hfq_factor_dir", lambda: tmp_path)
    results = iter([
        "fail:RuntimeError:baostock 10001001: 用户未登录",
        "ok:+3",
    ])
    monkeypatch.setattr(m, "update_one", lambda *a, **k: next(results))

    assert m.main(["--code", "600000", "--today", "2026-10-28"]) == 0
    assert len(logins) == 2


def test_main_does_not_relogin_on_data_defects(tmp_path, monkeypatch):
    """shrink / empty-window 是数据缺陷不是会话问题，重连救不了。"""
    import tdxfeed.hfq_factor as m

    logins: list[int] = []
    _fake_bs(monkeypatch, logins)
    monkeypatch.setattr(m, "hfq_factor_dir", lambda: tmp_path)
    monkeypatch.setattr(m, "update_one", lambda *a, **k: "fail:shrink")

    assert m.main(["--code", "600000", "--today", "2026-10-28"]) == 1
    assert len(logins) == 1
