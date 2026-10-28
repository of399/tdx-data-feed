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
    """返回固定列序、从 0 开始的副本。缺列抛 KeyError，不静默补零。"""
    cols = {name: (f"{name}{suffix}" if f"{name}{suffix}" in df.columns else name)
            for name in OHLCV}
    missing = [name for name, src in cols.items() if src not in df.columns]
    if missing:
        raise KeyError(f"缺列 {missing}（suffix={suffix!r}）")
    out = pd.DataFrame({
        "date": df["date"].to_numpy(),
        **{name: df[src].to_numpy(dtype=float) for name, src in cols.items()},
    })
    return out.reset_index(drop=True)
