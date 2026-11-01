"""find_double_up_c 的 ST 判定必须走规范 is_st_name，不再用 StockVault 文件名启发式。

旧实现靠 /home/jiuben/StockVault 的 md 文件名猜 ST（漏判 *ST康佳A，且依赖本机绝对路径）；
规范检测器 tdxfeed.names.is_st_name 按名称判，与 predict 等活入口同源。
本守卫钉死：源码用 is_st_name、不再引用 VAULT_DIR；screen_one 按名称判 ST。
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
        "find_double_up_c_st", ROOT / "scripts" / "find_double_up_c.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _doubling_parquet(path: Path):
    n = 30
    close = np.array([10.0] * 10 + [20.0] * 20)  # 第 10 根翻倍，构造一个命中
    df = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.full(n, 1000.0), "amount": np.full(n, 1.0e7),
    })
    df.to_parquet(path, index=False)


def test_source_uses_canonical_st_detector():
    source = (ROOT / "scripts" / "find_double_up_c.py").read_text(encoding="utf-8")
    assert "is_st_name" in source, "ST 判定应委托 tdxfeed.names.is_st_name"
    assert "VAULT_DIR" not in source, "不应再依赖 StockVault 文件名启发式（本机绝对路径）"


def test_screen_one_flags_st_by_name(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "PARQUET", tmp_path)
    _doubling_parquet(tmp_path / "sz000016.parquet")

    st_hits = mod.screen_one("sz000016", {"000016": "*ST康佳A"})
    assert st_hits, "合成翻倍帧应产生命中"
    assert all(h["is_st"] for h in st_hits), "*ST康佳A 应被判 ST"

    ok_hits = mod.screen_one("sz000016", {"000016": "康佳A"})
    assert ok_hits and not any(h["is_st"] for h in ok_hits), "非 ST 名称不应被判 ST"
