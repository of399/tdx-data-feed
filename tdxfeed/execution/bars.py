"""把各调用点的行情帧归一成内核认识的五列，只做形状转换，不做筛选。"""
from __future__ import annotations

import pandas as pd

OHLCV = ("open", "high", "low", "close", "volume")


def date_str(value) -> str:
    s = str(value).replace("-", "")[:8]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return str(value)[:10]


def as_bars(df: pd.DataFrame, *, suffix: str = "") -> pd.DataFrame:
    """返回固定列序、从 0 开始的副本。

    ``suffix`` 为空时使用裸列；非空时五列必须全部带该后缀，任一缺失抛 KeyError，
    不回退到裸列——否则一半复权一半不复权的帧会静默混进内核。
    """
    cols = ({name: f"{name}{suffix}" for name in OHLCV} if suffix
            else {name: name for name in OHLCV})
    missing = [src for src in cols.values() if src not in df.columns]
    if missing:
        raise KeyError(f"缺列 {missing}（suffix={suffix!r}）")
    out = pd.DataFrame({
        "date": df["date"].to_numpy(),
        **{name: df[src].to_numpy(dtype=float) for name, src in cols.items()},
    })
    return out.reset_index(drop=True)
