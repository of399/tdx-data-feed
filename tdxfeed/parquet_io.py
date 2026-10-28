"""行情表的读写入口：date 口径与写盘原子性只在这里定义。

写盘用 tmp → rename 而不是就地 to_parquet：存量 derived 的备份是
`cp -al` 硬链接快照，就地改写会穿透硬链接把备份一起改掉。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .paths import daily_path, derived_path, minute_path, read_path

# float 日期列的唯一可接受口径：同 _as_date32 的年份边界 1990–2100。
_FLOAT_DATE_LO, _FLOAT_DATE_HI = 19900101, 21001231


def _parse_yyyymmdd(s: pd.Series) -> pd.Series:
    """整数 YYYYMMDD → date；NA 位置先掩空再解析，保持 NaT 而不是格式错。"""
    text = s.astype("Int64").astype(str).where(s.notna())
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce").dt.date


def _check_float_yyyymmdd(fp: Path, s: pd.Series) -> None:
    """float 日期列只在「非空值全为区间内的 YYYYMMDD 整数」时放行，否则拒绝。

    Arrow int64 带 null 的表读出来就是 float64，全空列也是 vacuous pass，
    随后交给 _parse_yyyymmdd（它用 Int64 + where(notna) 保住空值）。
    """
    values = s.dropna().to_numpy(dtype="float64")
    integral = np.isfinite(values) & (values == np.floor(values))
    in_range = (values >= _FLOAT_DATE_LO) & (values <= _FLOAT_DATE_HI)
    ok = integral & in_range
    if not bool(ok.all()):
        sample = values[~ok][:3].tolist()
        raise ValueError(
            f"{fp} 的 date 列是 float，非空值不全是 {_FLOAT_DATE_LO}–{_FLOAT_DATE_HI} "
            f"内的 YYYYMMDD 整数（异常值示例 {sample}）：float 日期列无法确定口径，"
            "疑似 epoch 秒/纳秒或错列，拒绝按纳秒解释"
        )


def _as_date32(s: pd.Series) -> pd.Series:
    """转成 object-of-datetime.date，交给 from_pandas 推断 date32 并同步 metadata。"""
    if pd.api.types.is_float_dtype(s):
        raise ValueError(
            f"date 列 {s.name!r} 是浮点 dtype：float 会被当作 epoch 纳秒解析，"
            "把所有值写成 1970-01-01；请先转成 YYYYMMDD 整数或字符串"
        )
    if pd.api.types.is_datetime64_any_dtype(s):
        out = s.dt.date
    elif pd.api.types.is_integer_dtype(s):
        out = _parse_yyyymmdd(s)
    elif pd.api.types.is_object_dtype(s):
        nonnull = s.dropna()
        kind = pd.api.types.infer_dtype(nonnull, skipna=True) if len(nonnull) else "empty"
        if kind.startswith("mixed"):
            sample = sorted({type(v).__name__ for v in nonnull.head(50)})
            raise ValueError(
                f"date 列 {s.name!r} 含混合类型（{kind}）：整数与字符串/日期混用，"
                f"无法确定日期口径（示例值类型 {sample}）；请先统一为 YYYYMMDD 整数或 ISO 字符串"
            )
        if kind == "empty":
            out = pd.Series([None] * len(s), index=s.index, dtype=object)
        elif kind == "integer":
            out = _parse_yyyymmdd(s)
        elif kind == "floating":
            raise ValueError(
                f"date 列 {s.name!r} 是浮点 dtype：float 会被当作 epoch 纳秒解析，"
                "把所有值写成 1970-01-01；请先转成 YYYYMMDD 整数或字符串"
            )
        elif kind == "date":
            out = s                                  # 已是 datetime.date，不走 to_datetime
        else:                                        # string / datetime
            out = pd.to_datetime(s, errors="coerce").dt.date
    else:
        out = pd.to_datetime(s, errors="coerce").dt.date
    nulls_before = int(s.isna().sum())
    newly_null = int(pd.isna(out).sum()) - nulls_before
    if newly_null > 0:
        raise ValueError(
            f"date 列 {s.name!r} 有 {newly_null} 个值无法解析为日期；拒绝写盘："
            "静默变 NaT 会让读回时按 date 去重把整片行合并掉"
        )
    years = [v.year for v in out.dropna()]
    if years and not all(1990 <= y <= 2100 for y in years):
        bad = min(y for y in years if not 1990 <= y <= 2100)
        raise ValueError(
            f"date 列 {s.name!r} 出现越界年份 {bad}（要求 1990–2100）："
            "疑似 epoch 秒/纳秒或错列，拒绝写盘"
        )
    if out.isna().all():
        # 全 NaT 的 datetime64 列 .dt.date 仍保留 datetime64 dtype，from_pandas 会写成
        # timestamp[ns]；统一成 object-of-None，由 write_table 补成 date32。
        return pd.Series([None] * len(out), index=out.index, dtype=object)
    return out


def write_table(path: Path, df: pd.DataFrame, date_col: str = "date") -> Path:
    """写 parquet：date 列强制 date32[day]；先写 .tmp 再 rename。

    先转换 date 列再 from_pandas：set_column 只换数据不换 pandas metadata，
    会让 Int64 带 NA 的表读回报 TypeError；from_pandas 一次生成一致的两者。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if date_col in df.columns:
        df = df.copy()
        df[date_col] = _as_date32(df[date_col])
    table = pa.Table.from_pandas(df, preserve_index=False)
    if date_col in df.columns:
        idx = table.schema.get_field_index(date_col)
        if pa.types.is_null(table.schema.field(idx).type):
            # 全空行/零行时 from_pandas 推断成 Arrow null 而非 date32[day]。纯 null 列
            # 没有 pandas 扩展 dtype 与之冲突，set_column 在这里不会触发上一轮那个 TypeError。
            table = table.set_column(
                idx, pa.field(date_col, pa.date32()),
                pa.array([None] * len(df), type=pa.date32()),
            )
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    os.replace(tmp, path)
    return path


def read_daily(src: str | Path, columns=None) -> pd.DataFrame:
    """src 传代码（sh600519 / 600519）或 .parquet 路径；date→datetime64，升序去重。

    读侧按 dtype 分派而不是一律 to_datetime：存量表与 .v1.bak 备份里的 date 是
    int64 YYYYMMDD，直接 to_datetime 会按 epoch 纳秒读成 1970-01-01；Arrow 的
    int64 列一旦带 null，pandas 读出来就是 float64，同样不能交给 to_datetime。
    空日期至多 1 个：去重按 date 取值，多个 NaT 会被并成一行并静默丢掉 OHLCV。
    """
    fp = Path(src) if str(src).endswith(".parquet") else daily_path(src)
    df = pd.read_parquet(read_path(fp), columns=columns)
    if "date" in df.columns:
        raw = df["date"]
        if pd.api.types.is_float_dtype(raw):
            _check_float_yyyymmdd(fp, raw)
        if pd.api.types.is_float_dtype(raw) or pd.api.types.is_integer_dtype(raw):
            parsed = _parse_yyyymmdd(raw)
            lost = int(parsed.isna().sum()) - int(raw.isna().sum())
            if lost > 0:
                raise ValueError(
                    f"{fp} 的 date 列是 YYYYMMDD 整数但 {lost} 个值不是合法日期；"
                    "拒绝按 epoch 纳秒解释（去重会把整片行并成一行），请先确认表的日期口径"
                )
            df["date"] = pd.to_datetime(parsed)
        else:
            df["date"] = pd.to_datetime(raw)
        blank = int(df["date"].isna().sum())
        if blank > 1:
            raise ValueError(
                f"{fp} 的 date 列有 {blank} 个空日期：按 date 去重会把多行空日期并成一行、"
                "静默丢掉这些行的 OHLCV；请先补齐这些日期或删掉这些行"
            )
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
