"""五张行情表的路径定位：只定位，不 fallback，缺文件就报错。

各表命名不一致是历史事实，不要在这里抹平：
  daily / min*   → 带市场前缀 sh600519.parquet
  adj_factor / hfq_factor → 纯 6 位 600519.parquet
  derived        → 带前缀 + _indicators 后缀 sh600519_indicators.parquet
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))


def _six(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return c.zfill(6)


def exchange_prefix(code: str) -> str:
    c = _six(code)
    if c.startswith(("920", "8", "4")):
        return "bj"
    return "sh" if c[0] in "69" else "sz"


DAILY_BAK_SUFFIX = ".v1.bak.parquet"


def _minute_name(period: str) -> str:
    p = str(period).strip().lower()
    if p.startswith("min"):
        p = p[len("min"):]
    elif p.endswith("min"):
        p = p[:-len("min")]
    if p.startswith("m"):
        p = p[1:]
    return f"min{p}"


def parquet_root() -> Path:
    return DATA_ROOT / "parquet"


def adj_factor_dir() -> Path:
    return DATA_ROOT / "adj_factor"


def hfq_factor_dir() -> Path:
    return DATA_ROOT / "hfq_factor"


def derived_dir() -> Path:
    return parquet_root() / "derived"


def minute_dir(period: str) -> Path:
    return parquet_root() / _minute_name(period)


def finance_parquet() -> Path:
    return parquet_root() / "finance.parquet"


def finance_deep_dir() -> Path:
    return parquet_root() / "finance_deep"


def xdxr_path() -> Path:
    return parquet_root() / "xdxr.parquet"


def daily_files() -> list[Path]:
    """active daily 文件，排除 .v1.bak 迁移备份；按文件名排序。"""
    return sorted(p for p in daily_dir().glob("*.parquet")
                  if not p.name.endswith(DAILY_BAK_SUFFIX))


def adj_factor_files() -> list[Path]:
    return sorted(adj_factor_dir().glob("*.parquet"))


def daily_dir() -> Path:
    return parquet_root() / "daily"


def daily_path(code: str) -> Path:
    return daily_dir() / f"{exchange_prefix(code)}{_six(code)}.parquet"


def minute_path(code: str, period: str) -> Path:
    return minute_dir(period) / f"{exchange_prefix(code)}{_six(code)}.parquet"


def adj_factor_path(code: str) -> Path:
    return adj_factor_dir() / f"{_six(code)}.parquet"


def hfq_factor_path(code: str) -> Path:
    return hfq_factor_dir() / f"{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    return derived_dir() / f"{exchange_prefix(code)}{_six(code)}_indicators.parquet"


def universe_path() -> Path:
    return DATA_ROOT / "all_a_stocks.json"


def read_path(path: Path) -> Path:
    """存在性守卫：把「路径算错」变成立刻可见的异常，而不是空表。"""
    if not Path(path).exists():
        raise FileNotFoundError(str(path))
    return Path(path)
