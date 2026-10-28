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


def _as_date32(df: pd.DataFrame) -> pd.Series:
    s = df["date"]
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.date
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype("int64").astype(str), format="%Y%m%d").dt.date
    return pd.to_datetime(s, errors="coerce").dt.date


def write_table(path: Path, df: pd.DataFrame, date_col: str = "date") -> Path:
    """写 parquet：date 列强制 date32[day]；先写 .tmp 再 rename。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    if date_col in df.columns:
        idx = table.schema.get_field_index(date_col)
        table = table.set_column(idx, date_col, pa.array(_as_date32(df), type=pa.date32()))
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    os.replace(tmp, path)
    return path


def read_daily(src: str | Path, columns=None) -> pd.DataFrame:
    """src 传代码（sh600519 / 600519）或 .parquet 路径；date→datetime64，升序去重。"""
    fp = Path(src) if str(src).endswith(".parquet") else daily_path(src)
    df = pd.read_parquet(read_path(fp), columns=columns)
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
