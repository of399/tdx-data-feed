"""注册表三元组：现有 10 条零变化；raw 口径不碰因子表；hfq 缺因子告警不 raise。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

OLD_ENTRIES = {
    "prod": "d_signal_5d_model.txt",
    "v9": "d_signal_5d_model_v9.txt",
    "v10_dart": "d_signal_5d_model_v10.txt",
    "vpnl": "d_signal_5d_model_vpnl.txt",
    "vpnl_asym": "d_signal_5d_model_vpnl_asym.txt",
    "vpnl_huber": "d_signal_5d_model_vpnl_huber.txt",
    "vpnl_huber_ind": "d_signal_5d_model_vpnl_huber_industry.txt",
    "vpnl_huber_mkt": "d_signal_5d_model_vpnl_huber_market.txt",
    "vpnl_huber_stage1": "d_signal_5d_model_vpnl_huber_stage1.txt",
    "daban": "d_signal_5d_model_daban.txt",
}


def test_registry_is_three_tuple_and_old_entries_are_raw():
    for name, fname in OLD_ENTRIES.items():
        path, kind, basis = predict.MODEL_REGISTRY[name]
        assert Path(path).name == fname, name
        assert kind in ("cls", "reg")
        assert basis == "raw", name
    for name in ("daban_hfq", "vpnl_huber_stage1_hfq"):
        assert predict.MODEL_REGISTRY[name][2] == "hfq"


def test_defaults_are_untouched():
    assert predict.DEFAULT_MODEL == "daban"
    assert predict.FALLBACK_MODEL == "vpnl_huber_stage1"
    assert predict.POSITION_MODE_MODEL == {"small": "daban", "full": "vpnl_huber_stage1"}


def _with_attrs(df: pd.DataFrame, **kw) -> pd.DataFrame:
    out = df.copy()
    out.attrs.update(kw)
    return out


def _wire_symbol(monkeypatch, tmp_path):
    """合成日线 + 三个环境隔离：日期必须近（MAX_STALE_DAYS=10）、
    特征模式锁 legacy（候选因子是 polars 专属）、ADJ_DIR 指向不存在目录。"""
    d = tmp_path / "daily"; d.mkdir()
    n = 60
    closes = np.linspace(10.0, 12.0, n)
    end = pd.Timestamp.now().normalize()
    df = pd.DataFrame({
        "date": pd.date_range(end=end, periods=n).date,
        "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
        "volume": np.full(n, 1000.0), "amount": np.full(n, 1.0e7),
    })
    p = d / "sh600000.parquet"
    df.to_parquet(p, index=False)
    monkeypatch.setattr(predict, "PARQUET_DIR", d)
    monkeypatch.setattr(predict, "ADJ_DIR", tmp_path / "no_adj")
    monkeypatch.setattr(predict, "ACTIVE_FEATURE_MODE", "legacy")
    monkeypatch.setattr(predict, "_enrich_market_features", lambda r, d_: r)
    monkeypatch.setattr(predict, "_enrich_industry_features", lambda r, d_: r)
    return df


def test_raw_basis_never_touches_the_factor_table(monkeypatch, tmp_path):
    _wire_symbol(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("raw 口径不该构建 hfq 帧")

    monkeypatch.setattr(predict.adjusted, "frame", _boom)
    assert predict.compute_features("600000", "raw") is not None


def test_hfq_basis_with_low_coverage_returns_none_instead_of_raising(monkeypatch, tmp_path):
    df = _wire_symbol(monkeypatch, tmp_path)
    monkeypatch.setattr(predict.adjusted, "frame",
                        lambda code, kind, strict: _with_attrs(df, factor_cov=0.5))
    assert predict.compute_features("600000", "hfq") is None
