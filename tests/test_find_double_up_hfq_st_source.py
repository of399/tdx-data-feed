"""find_double_up_hfq 的 ST 判定必须走 tdxfeed.names.is_st_name（P1-12 Phase 2a）。

旧写法 `"ST" in name.upper()` 与权威的差在排版空格：权威先去空格再判，
所以 "*S T康佳A" 旧写法漏判、权威判 ST。真实宇宙 5221 只 / hfq 样本 17794 行
翻转数均为 0（2026-11-02 探针），因此这是语义收编而非口径变更；
本守卫用合成名称把语义差钉住，防止内联写法回流。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "find_double_up_hfq_st", ROOT / "scripts" / "find_double_up_hfq.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _synthetic_frame(code, *, kind, strict=True):
    n = 30
    close = np.array([10.0] * 10 + [20.0] * 20)  # 第 10 根翻倍
    df = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "close": close,
        "amount": np.full(n, 1.0e7),
    })
    df.attrs["factor_cov"] = 1.0
    return df


def _screen(mod, tmp_path, monkeypatch, name: str) -> pd.DataFrame:
    names_file = tmp_path / "names.json"
    names_file.write_text(json.dumps({"000016": name}), encoding="utf-8")
    out = tmp_path / "detail.csv"
    exc = tmp_path / "excluded.csv"

    monkeypatch.setattr(mod, "daily_files", lambda: [tmp_path / "sz000016.parquet"])
    monkeypatch.setattr(mod, "frame", _synthetic_frame)
    monkeypatch.setattr(mod, "factor_jumps",
                        lambda code: pd.DataFrame(columns=["date", "suspect", "econ"]))
    monkeypatch.setattr(mod, "get_limit", lambda code: 0.095)

    assert mod.main([
        "--names", str(names_file), "--out", str(out), "--excluded-out", str(exc),
    ]) == 0
    df = pd.read_csv(out)
    assert len(df) > 0, "合成翻倍帧应产生命中"
    return df


@pytest.mark.parametrize("name,expected", [
    ("*S T康佳A", True),   # 排版空格拆开 ST：权威判 ST，旧内联写法漏判
    ("*ST康佳A", True),
    ("康佳A", False),
])
def test_is_st_column_follows_canonical_detector(tmp_path, monkeypatch, name, expected):
    mod = _load()
    df = _screen(mod, tmp_path, monkeypatch, name)
    assert bool(df["is_st"].iloc[0]) is expected, f"{name!r} 的 is_st 应为 {expected}"


def test_source_delegates_to_is_st_name():
    source = (ROOT / "scripts" / "find_double_up_hfq.py").read_text(encoding="utf-8")
    assert "is_st_name" in source, "ST 判定应委托 tdxfeed.names.is_st_name"
    assert 'is_st="ST" in name.upper()' not in source, "不得在调用点内联判 ST"
