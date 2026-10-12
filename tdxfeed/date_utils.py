"""日期解析工具（项目单一数据源）。

背景：data/parquet/daily 的 date 列实际为 int64 的 YYYYMMDD（schema 漂移），
直接 ``pd.to_datetime`` 会被当成纳秒 -> 1970，导致按日期匹配全部失败。

所有脚本统一从此模块导入，避免多份实现口径不一致（历史上 parse_date 在 20+ 文件重复）。

提供三种入口：
- ``parse_date(value)``：标量 -> pd.Timestamp；pd.Series -> 归一化 Datetime 列。
- ``parse_date_series(s)``：等价于旧的 ``parse_datecol``（Series -> Series）。
- ``to_date_str(value)``：标量 -> ``'YYYY-MM-DD'`` 字符串（等价于旧的 bench 版 parse_date）。
"""
from __future__ import annotations

import pandas as pd

__all__ = ["parse_date", "parse_date_series", "to_date_str"]


def _parse_series(s: pd.Series) -> pd.Series:
    """Series -> 归一化 Datetime 列（plan_e 回测口径）。"""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.normalize()
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype("int64").astype("str"), format="%Y%m%d")
    return pd.to_datetime(s)


def parse_date(value) -> "pd.Series | pd.Timestamp":
    """解析单个值或 Series 为 pd.Timestamp / 归一化 Series。

    支持：pd.Timestamp、datetime.date/time、int/float/numpy 标量 YYYYMMDD、
    8 位字符串、以及 pd.Series（按列归一化）。无法解析返回 pd.NaT。
    """
    if isinstance(value, pd.Series):
        return _parse_series(value)
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    try:
        num = int(value)
        s = str(num)
        if len(s) == 8:
            return pd.Timestamp(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return pd.Timestamp(s) if s.strip() else pd.NaT
    except (TypeError, ValueError):
        if isinstance(value, str):
            s = value.strip()
            if len(s) == 8 and s.isdigit():
                return pd.Timestamp(int(s[:4]), int(s[4:6]), int(s[6:8]))
            return pd.Timestamp(s) if s else pd.NaT
        if hasattr(value, "year"):  # datetime.date / datetime.datetime
            return pd.Timestamp(value)
        return pd.NaT


def parse_date_series(s: pd.Series) -> pd.Series:
    """Series 版本（build_cls2000 的 ``parse_datecol`` 等价）。"""
    return _parse_series(s)


def to_date_str(value) -> str:
    """标量 -> 'YYYY-MM-DD' 字符串（bench_v567/bench_v7r 旧 parse_date 等价）。

    兼容 numpy 标量（int/float），无法解析时回退 ``str(value)[:10]``。
    """
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    try:
        num = int(value)
    except (TypeError, ValueError):
        return str(value)[:10]
    s = str(num)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
