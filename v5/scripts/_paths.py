"""v5 脚本的路径定位：与主包零耦合（不导入 tdxfeed），读同一个 TDX_OUT_ROOT。

与 tdxfeed/paths.py 的一致性由 tests/test_data_layer.py 的 parity 用例锁住。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))

DAILY_BAK_SUFFIX = ".v1.bak.parquet"


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


def parquet_root() -> Path:
    return DATA_ROOT / "parquet"


def daily_dir() -> Path:
    return parquet_root() / "daily"


def adj_factor_dir() -> Path:
    return DATA_ROOT / "adj_factor"


def derived_dir() -> Path:
    return parquet_root() / "derived"


def _minute_name(period: str) -> str:
    p = str(period).strip().lower()
    if p.startswith("min"):
        p = p[len("min"):]
    elif p.endswith("min"):
        p = p[:-len("min")]
    if p.startswith("m"):
        p = p[1:]
    return f"min{p}"


def minute_dir(period: str) -> Path:
    return parquet_root() / _minute_name(period)


def daily_path(code: str) -> Path:
    return daily_dir() / f"{exchange_prefix(code)}{_six(code)}.parquet"


def adj_factor_path(code: str) -> Path:
    return adj_factor_dir() / f"{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    return derived_dir() / f"{exchange_prefix(code)}{_six(code)}_indicators.parquet"


def daily_files() -> list[Path]:
    return sorted(p for p in daily_dir().glob("*.parquet")
                  if not p.name.endswith(DAILY_BAK_SUFFIX))


def adj_factor_files() -> list[Path]:
    return sorted(adj_factor_dir().glob("*.parquet"))


DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
