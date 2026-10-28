"""行情表的读写入口：date 口径与写盘原子性只在这里定义。

写盘用 tmp → rename 而不是就地 to_parquet：存量 derived 的备份是
`cp -al` 硬链接快照，就地改写会穿透硬链接把备份一起改掉。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .paths import daily_path, derived_path, minute_path, read_path


def _as_date32(s: pd.Series) -> pd.Series:
    if pd.api.types.is_float_dtype(s):
        raise ValueError(
            f"date 列 {s.name!r} 是浮点 dtype：float 会被当作 epoch 纳秒解析，"
            "把所有值写成 1970-01-01；请先转成 YYYYMMDD 整数或字符串"
        )
    if pd.api.types.is_datetime64_any_dtype(s):
        out = s.dt.date
    elif pd.api.types.is_integer_dtype(s):
        out = pd.to_datetime(s.astype("Int64").astype(str), format="%Y%m%d",
                             errors="coerce").dt.date
    else:
        out = pd.to_datetime(s, errors="coerce").dt.date
    nulls_before = int(s.isna().sum())
    newly_null = int(pd.isna(out).sum()) - nulls_before
    if newly_null > 0:
        raise ValueError(
            f"date 列 {s.name!r} 有 {newly_null} 个值无法解析为日期；拒绝写盘："
            "静默变 NaT 会让读回时按 date 去重把整片行合并掉"
        )
    return out


def write_table(path: Path, df: pd.DataFrame, date_col: str = "date") -> Path:
    """写 parquet：date 列强制 date32[day]；先写 .tmp 再 rename。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    if date_col in df.columns:
        idx = table.schema.get_field_index(date_col)
        table = table.set_column(idx, date_col,
                                 pa.array(_as_date32(df[date_col]), type=pa.date32()))
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    os.replace(tmp, path)
    return path


def read_daily(src: str | Path, columns=None) -> pd.DataFrame:
    """src 传代码（sh600519 / 600519）或 .parquet 路径；date→datetime64，升序去重。"""
    fp = Path(src) if str(src).endswith(".parquet") else daily_path(src)
    df = pd.read_parquet(read_path(fp), columns=columns)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date", kind="mergesort").drop_duplicates("date", keep="last")
    return df.reset_index(drop=True)


def write_daily(df: pd.DataFrame, code: str) -> Path:
    return write_table(daily_path(code), df)


def read_minute(code: str, period: str, parse_time: bool = False) -> pd.DataFrame:
    """分钟表时间列是 string（YYYY-MM-DD HH:MM:SS）；parse_time=True 才转 datetime64。"""
    df = pd.read_parquet(read_path(minute_path(code, period)))
    if parse_time:
        df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime", kind="mergesort").reset_index(drop=True)


def read_derived(code: str | Path) -> pd.DataFrame:
    fp = Path(code) if str(code).endswith(".parquet") else derived_path(code)
    return pd.read_parquet(read_path(fp))
