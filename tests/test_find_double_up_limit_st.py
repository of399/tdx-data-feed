"""find_double_up_c 的 limit_up_count / limit 列须按 ST 与交易日（并轨）判阈值。

get_limit 的 docstring 明示：主板 ST 在 2026-07-06 并轨（前 5% / 后 10%），
历史区间必须传 on_date，否则并轨前的 ST 被误判成 10%。旧实现 get_limit(code)
既不传 name 也不传 on_date，ST 股的涨停数与 limit 列都按 10% 算，少计涨停。
本守卫用并轨前、日涨 5% 的合成 ST 股钉死：ST 应计涨停（5% 档），非 ST 不计（10% 档）。
limit_up_count 是描述性元数据列，不进模型特征、不改样本宇宙，修它无需重训。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "find_double_up_c_limit", ROOT / "scripts" / "find_double_up_c.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _st_rising_parquet(path: Path):
    """并轨前（2020 年）日涨 5% 的序列：ST 主板涨停档 5%，非 ST 10%。"""
    n = 30
    close = 10.0 * np.power(1.05, np.arange(n))
    df = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.full(n, 1000.0), "amount": np.full(n, 1.0e7),
    })
    df.to_parquet(path, index=False)


def test_st_limit_up_count_uses_5pct_before_merge(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "PARQUET", tmp_path)
    _st_rising_parquet(tmp_path / "sz000016.parquet")

    st_hits = mod.screen_one("sz000016", {"000016": "*ST康佳A"})
    assert st_hits, "合成翻倍帧应产生命中"
    # 日涨 5% 在并轨前 ST 档（5%）应计涨停；旧 10% 口径会一个都不计。
    assert all(h["limit_up_count"] > 0 for h in st_hits), (
        f"并轨前 ST 股 5% 涨停未被计入: {[h['limit_up_count'] for h in st_hits]}"
    )
    assert all(h["limit"] == 5 for h in st_hits), (
        f"并轨前 ST 的 limit 列应为 5: {[h['limit'] for h in st_hits]}"
    )

    ok_hits = mod.screen_one("sz000016", {"000016": "康佳A"})
    assert ok_hits and all(h["limit_up_count"] == 0 for h in ok_hits), (
        "非 ST 股 5% 涨幅不该计涨停（10% 档）"
    )
    assert all(h["limit"] == 10 for h in ok_hits)
