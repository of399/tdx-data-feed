"""路径访问器只定位不 fallback；写入口是 date32 + 原子写；v5 孪生与主包同解。"""
from __future__ import annotations

import ast
import importlib.util
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tdxfeed import parquet_io, paths

ROOT = Path(__file__).resolve().parents[1]


def test_table_dirs_share_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.parquet_root() == tmp_path / "parquet"
    assert paths.daily_dir() == tmp_path / "parquet" / "daily"
    assert paths.derived_dir() == tmp_path / "parquet" / "derived"
    assert paths.adj_factor_dir() == tmp_path / "adj_factor"
    assert paths.hfq_factor_dir() == tmp_path / "hfq_factor"
    assert paths.finance_parquet() == tmp_path / "parquet" / "finance.parquet"
    assert paths.finance_deep_dir() == tmp_path / "parquet" / "finance_deep"
    assert paths.xdxr_path() == tmp_path / "parquet" / "xdxr.parquet"


def test_minute_dir_accepts_both_spellings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.minute_dir("60") == paths.minute_dir("min60") == tmp_path / "parquet" / "min60"
    assert paths.minute_dir("1min") == paths.minute_dir("m1") == paths.minute_dir("min1")
    assert paths.minute_dir("MIN5") == paths.minute_dir("5")
    assert paths.minute_path("600000", "60").parent == paths.minute_dir("60")
    assert paths.minute_path("600000", "45") == tmp_path / "parquet" / "min45" / "sh600000.parquet"


def test_daily_files_excludes_migration_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.daily_dir()
    d.mkdir(parents=True)
    for name in ("sh600000.parquet", "sh600000.v1.bak.parquet", "sz000001.parquet",
                 "_audit.csv"):
        (d / name).write_bytes(b"")
    assert [p.name for p in paths.daily_files()] == ["sh600000.parquet", "sz000001.parquet"]


def test_daily_files_ignores_noncanonical_residue(tmp_path, monkeypatch):
    """中断的 amount 修补残留以 .parquet 结尾，不能因此被当成一只股票。"""
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.daily_dir()
    d.mkdir(parents=True)
    for name in ("sh600000.parquet", "sz000001.parquet",
                 "sh600600.amountfix.tmp.parquet",
                 "sh600601.v1.bak.parquet",
                 "sh600602.parquet.tmp"):
        (d / name).write_bytes(b"")
    assert [p.name for p in paths.daily_files()] == ["sh600000.parquet", "sz000001.parquet"]


def test_adj_factor_files_lists_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.adj_factor_dir()
    d.mkdir(parents=True)
    (d / "000001.parquet").write_bytes(b"")
    (d / "600000.parquet").write_bytes(b"")
    assert [p.name for p in paths.adj_factor_files()] == ["000001.parquet", "600000.parquet"]


def _bars(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.1] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })


def test_write_table_rejects_float_date(tmp_path):
    """float 日期被 pandas 当 epoch 纳秒，会静默全部写成 1970-01-01。"""
    df = pd.DataFrame({"date": [20260105.0, 20260106.0], "close": [10.1, 10.2]})
    with pytest.raises(ValueError, match="1970-01-01"):
        parquet_io.write_table(tmp_path / "x.parquet", df)


def test_write_table_honors_date_col(tmp_path):
    """date_col 指定别列时，date32 只作用在那一列，不能被 date 列顶替。"""
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": ["2026-01-05"], "td": ["2026-02-02"], "close": [10.0]})
    parquet_io.write_table(fp, df, date_col="td")
    out = pd.read_parquet(fp)
    assert out["td"].iloc[0] == date(2026, 2, 2)
    assert out["date"].iloc[0] == "2026-01-05"


def test_write_table_rejects_unparseable_dates(tmp_path):
    """无法解析的日期不能静默变 NaT 写盘（会在读回去重时被当成同一行）。"""
    df = pd.DataFrame({"date": ["2026-01-05", "not-a-date"], "close": [10.0, 10.1]})
    with pytest.raises(ValueError, match="无法解析"):
        parquet_io.write_table(tmp_path / "x.parquet", df)


def test_write_table_allows_preexisting_nat(tmp_path):
    """输入本来就为空的行允许保留为空。"""
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": ["2026-01-05", pd.NaT], "close": [10.0, 10.1]})
    parquet_io.write_table(fp, df)
    out = pd.read_parquet(fp)
    assert out["date"].iloc[0] == date(2026, 1, 5)
    assert pd.isna(out["date"].iloc[1])


def test_int64_date_roundtrip(tmp_path):
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": pd.Series([20260105, 20260106], dtype="int64"), "c": [1, 2]})
    parquet_io.write_table(fp, df)
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    out = pd.read_parquet(fp)
    assert out["date"].tolist() == [date(2026, 1, 5), date(2026, 1, 6)]


def test_nullable_int64_with_na_roundtrip(tmp_path):
    """Int64 带 NA 若只换数据不换 pandas metadata，读回时 schema 与 metadata 冲突。"""
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": pd.Series([20260105, None], dtype="Int64"), "c": [1, 2]})
    parquet_io.write_table(fp, df)
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    out = pd.read_parquet(fp)
    assert out["date"].iloc[0] == date(2026, 1, 5)
    assert pd.isna(out["date"].iloc[1])
    assert int(out["date"].isna().sum()) == 1


def test_object_dtype_integer_dates(tmp_path):
    """object 里的整数若走 default to_datetime，会被当 epoch 纳秒写成 1970-01-01。"""
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": pd.Series([20260105, 20260106], dtype=object), "c": [1, 2]})
    parquet_io.write_table(fp, df)
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    out = pd.read_parquet(fp)
    assert out["date"].tolist() == [date(2026, 1, 5), date(2026, 1, 6)]


def test_object_dtype_float_dates_rejected(tmp_path):
    df = pd.DataFrame({"date": pd.Series([20260105.0], dtype=object), "c": [1]})
    with pytest.raises(ValueError, match="1970-01-01"):
        parquet_io.write_table(tmp_path / "x.parquet", df)


def test_out_of_range_year_rejected(tmp_path):
    """年份兜底：1970 只能来自 epoch 解析或错列，不能写盘。"""
    df = pd.DataFrame({"date": pd.Series([19700101], dtype="int64"), "c": [1]})
    with pytest.raises(ValueError, match="1990"):
        parquet_io.write_table(tmp_path / "x.parquet", df)


def test_object_dtype_python_dates(tmp_path):
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": pd.Series([date(2026, 1, 5), date(2026, 1, 6)], dtype=object),
                       "c": [1, 2]})
    parquet_io.write_table(fp, df)
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    out = pd.read_parquet(fp)
    assert out["date"].tolist() == [date(2026, 1, 5), date(2026, 1, 6)]


@pytest.mark.parametrize("series", [
    [None, None],
    pd.Series(pd.to_datetime([None, None])),      # 全 NaT 的 datetime64 保留 dtype，易漏
], ids=["object", "datetime64-nat"])
def test_all_null_date_column_stays_date32(tmp_path, series):
    """全空列走 from_pandas 会被推断成 Arrow null（或 timestamp）类型，丢失日期口径。"""
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, pd.DataFrame({"date": series, "close": [1.0, 2.0]}))
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    assert pd.read_parquet(fp)["date"].tolist() == [None, None]


@pytest.mark.parametrize("dtype", ["object", "datetime64[ns]"], ids=["object", "datetime64"])
def test_zero_row_date_column_stays_date32(tmp_path, dtype):
    fp = tmp_path / "x.parquet"
    df = pd.DataFrame({"date": pd.Series([], dtype=dtype),
                       "close": pd.Series([], dtype="float64")})
    parquet_io.write_table(fp, df)
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    assert pd.read_parquet(fp)["date"].tolist() == []


@pytest.mark.parametrize("values", [
    [20260105, "2026-01-06"],
    [20260105, date(2026, 1, 6)],
    [946684800000000000, "2026-01-06"],
    [20260105, 20260105.5],
], ids=["int-str", "int-date", "epoch-str", "int-float"])
def test_object_dtype_mixed_dates_rejected(tmp_path, values):
    """混合类型是数据质量信号：猜「整数按 epoch 还是 YYYYMMDD」只能赌，必须拒绝。"""
    df = pd.DataFrame({"date": pd.Series(values, dtype=object), "c": [1, 2]})
    fp = tmp_path / "x.parquet"
    with pytest.raises(ValueError, match="混合|mixed"):
        parquet_io.write_table(fp, df)
    assert not fp.exists()


def test_write_table_forces_date32_and_leaves_no_tmp(tmp_path):
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    assert [p.name for p in tmp_path.iterdir()] == ["x.parquet"]


def test_write_table_replaces_inode_not_overwrites(tmp_path):
    """硬链接快照的前提：写盘必须换 inode，否则 cp -al 备份会被就地改写穿透。"""
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    before = os.stat(fp).st_ino
    parquet_io.write_table(fp, _bars(8))
    assert os.stat(fp).st_ino != before


def test_read_daily_sorts_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    df = _bars(4).iloc[::-1].reset_index(drop=True)          # 倒序
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)     # 重复日期
    (paths.daily_dir()).mkdir(parents=True)
    df.to_parquet(paths.daily_path("600000"), index=False)    # 故意写成 timestamp
    out = parquet_io.read_daily("600000")
    assert out["date"].is_monotonic_increasing
    assert len(out) == 4
    assert str(out["date"].dtype).startswith("datetime64")


def test_write_daily_uses_canonical_name(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    fp = parquet_io.write_daily(_bars(), "600000")
    assert fp.name == "sh600000.parquet"


def test_read_minute_keeps_string_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.minute_dir("60").mkdir(parents=True)
    df = pd.DataFrame({"datetime": ["2026-10-27 15:00:00", "2026-10-27 14:00:00"],
                       "close": [10.0, 10.1]})
    df.to_parquet(paths.minute_path("600000", "60"), index=False)
    raw = parquet_io.read_minute("600000", "60")
    assert raw["datetime"].iloc[0] == "2026-10-27 14:00:00"
    assert raw["datetime"].dtype == object
    parsed = parquet_io.read_minute("600000", "60", parse_time=True)
    assert str(parsed["datetime"].dtype).startswith("datetime64")


@pytest.mark.parametrize("raw", [
    pd.Series(pd.to_datetime(["2026-01-05", "2026-01-06"])),          # timestamp
    pd.Series([20260105, 20260106], dtype="int64"),                    # 存量 YYYYMMDD 整数
    pd.Series([date(2026, 1, 5), date(2026, 1, 6)], dtype=object),     # date32 读成 object
    pd.Series(["2026-01-05", "2026-01-06"]),                           # 字符串
], ids=["timestamp", "int64", "object-date", "string"])
def test_read_daily_normalizes_date_encodings(tmp_path, monkeypatch, raw):
    """int64 若直接 to_datetime 会被当 epoch 纳秒读成 1970；其余编码都要读到同一天。"""
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.daily_dir().mkdir(parents=True)
    pd.DataFrame({"date": raw, "close": [1.0, 2.0]}).to_parquet(
        paths.daily_path("600000"), index=False)
    out = parquet_io.read_daily("600000")
    assert out["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-01-06"]


def test_read_daily_reads_own_date32_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    parquet_io.write_daily(_bars(2), "600000")
    out = parquet_io.read_daily("600000")
    assert out["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-01-06"]


def test_read_daily_unparseable_integer_dates_raise(tmp_path, monkeypatch):
    """epoch 纳秒整数不是 YYYYMMDD：静默变 NaT 后按 date 去重会把整片行并成一行。"""
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.daily_dir().mkdir(parents=True)
    pd.DataFrame({"date": [1736467200000000000, 1736553600000000000],
                  "close": [1.0, 2.0]}).to_parquet(paths.daily_path("600000"), index=False)
    with pytest.raises(ValueError, match="YYYYMMDD"):
        parquet_io.read_daily("600000")


def _write_raw_daily(tmp_path, monkeypatch, frame) -> Path:
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.daily_dir().mkdir(parents=True)
    fp = paths.daily_path("600000")
    frame.to_parquet(fp, index=False)
    return fp


def test_read_daily_multiple_null_dates_raise(tmp_path, monkeypatch):
    """NaT 之间互相「相等」，去重会把多行空日期并成一行，连带丢掉那些行的 OHLCV。"""
    _write_raw_daily(tmp_path, monkeypatch, pd.DataFrame({
        "date": pd.to_datetime(["2026-01-05", "2026-01-06", None, None]),
        "close": [1.0, 2.0, 3.0, 4.0],
    }))
    with pytest.raises(ValueError, match="2 个空日期"):
        parquet_io.read_daily("600000")


def test_read_daily_single_null_date_survives_roundtrip(tmp_path, monkeypatch):
    """写侧允许单个既有 NaT（test_write_table_allows_preexisting_nat），读侧必须能往返。"""
    _write_raw_daily(tmp_path, monkeypatch, pd.DataFrame({
        "date": pd.to_datetime(["2026-01-05", None, "2026-01-06"]),
        "close": [1.0, 2.0, 3.0],
    }))
    out = parquet_io.read_daily("600000")
    assert len(out) == 3
    assert out["date"].dropna().dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-01-06"]
    assert pd.isna(out["date"].iloc[-1])


def test_read_daily_float_yyyymmdd_parses_by_format(tmp_path, monkeypatch):
    """float 直接 to_datetime 会被当 epoch 纳秒，整列读成 1970-01-01。"""
    _write_raw_daily(tmp_path, monkeypatch, pd.DataFrame({
        "date": pd.Series([20260105.0, 20260106.0], dtype="float64"),
        "close": [1.0, 2.0],
    }))
    out = parquet_io.read_daily("600000")
    assert out["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-01-06"]


def test_read_daily_arrow_int64_with_null_is_float_upstream(tmp_path, monkeypatch):
    """Arrow int64 列带 null，pandas 读出来必然是 float64；不能因此退化成 1970。"""
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.daily_dir().mkdir(parents=True)
    fp = paths.daily_path("600000")
    pq.write_table(pa.Table.from_pydict({
        "date": pa.array([20260105, None], type=pa.int64()),
        "close": pa.array([1.0, 2.0]),
    }), fp)
    assert str(pd.read_parquet(fp)["date"].dtype) == "float64"
    out = parquet_io.read_daily("600000")
    assert out["date"].dropna().dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05"]
    assert int(out["date"].isna().sum()) == 1


@pytest.mark.parametrize("values", [
    [20260105.5],
    [19700101.0],
    [float("nan"), float("inf"), 20260105.0],
], ids=["fraction", "out-of-range", "inf"])
def test_read_daily_float_outside_yyyymmdd_rejected(tmp_path, monkeypatch, values):
    """float 不落在 YYYYMMDD 整数区间就是口径不明，不猜、不按纳秒解释。"""
    _write_raw_daily(tmp_path, monkeypatch, pd.DataFrame({
        "date": pd.Series(values, dtype="float64"),
        "close": [1.0] * len(values),
    }))
    with pytest.raises(ValueError, match="无法确定口径"):
        parquet_io.read_daily("600000")


def test_read_daily_float_bad_calendar_day_rejected(tmp_path, monkeypatch):
    """整数且在区间内但不存在的日子（20261232）同样不能静默变 NaT 去重。"""
    _write_raw_daily(tmp_path, monkeypatch, pd.DataFrame({
        "date": pd.Series([20260105.0, 20261232.0], dtype="float64"),
        "close": [1.0, 2.0],
    }))
    with pytest.raises(ValueError, match="不是合法日期"):
        parquet_io.read_daily("600000")


def test_read_daily_accepts_column_subset(tmp_path, monkeypatch):
    """columns 里没有 date 时不做归一/排序/去重，原样返回。"""
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    parquet_io.write_daily(_bars(), "600000")
    out = parquet_io.read_daily("600000", columns=["close"])
    assert list(out.columns) == ["close"]
    assert len(out) == 6


def test_missing_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        parquet_io.read_daily("600000")


def _load_v5_paths():
    spec = importlib.util.spec_from_file_location("v5_paths_under_test",
                                                  ROOT / "v5/scripts/_paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v5_twin_agrees_with_main_package(tmp_path, monkeypatch):
    v5 = _load_v5_paths()
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(v5, "DATA_ROOT", tmp_path)
    for code in ("600519", "000001", "920001", "sh600519", "bj920001", "300750",
                 "sh000001", "sh000300", "sh510300", "sz000001"):
        assert v5.daily_path(code) == paths.daily_path(code), code
        assert v5.adj_factor_path(code) == paths.adj_factor_path(code), code
    assert v5.parquet_root(tmp_path) == paths.parquet_root(tmp_path)
    assert v5.daily_dir(tmp_path) == paths.daily_dir(tmp_path)
    for period in ("5", "15", "60", "min60"):
        assert v5.minute_dir(period) == paths.minute_dir(period), period
    assert v5.derived_path("sh600519") == paths.derived_path("sh600519")
    assert (v5.derived_path("sh600519_indicators")
            == paths.derived_path("sh600519_indicators"))
    assert v5.DAILY_BAK_SUFFIX == paths.DAILY_BAK_SUFFIX
    daily = paths.daily_dir()
    daily.mkdir(parents=True)
    for name in ("sh600000.parquet", "sh600000.v1.bak.parquet", "sz000001.parquet",
                 "sh600600.amountfix.tmp.parquet"):
        (daily / name).write_bytes(b"")
    adj = paths.adj_factor_dir()
    adj.mkdir(parents=True)
    for name in ("000001.parquet", "600000.parquet"):
        (adj / name).write_bytes(b"")
    assert ([p.name for p in v5.daily_files()]
            == [p.name for p in paths.daily_files()]
            == ["sh600000.parquet", "sz000001.parquet"])
    assert ([p.name for p in v5.adj_factor_files()]
            == [p.name for p in paths.adj_factor_files()]
            == ["000001.parquet", "600000.parquet"])


def test_v5_twin_does_not_import_main_package():
    """只认导入语句：docstring 里提到 tdxfeed 不算耦合。"""
    tree = ast.parse((ROOT / "v5/scripts/_paths.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert [m for m in imported if m == "tdxfeed" or m.startswith("tdxfeed.")] == []
