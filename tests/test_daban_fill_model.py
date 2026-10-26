"""成交流模型：标签、校准与 OOS 诊断测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tdxfeed.daban.fill_model import (
    FILL_FEATURES,
    evaluate_fill_model,
    fill_label,
    fit_fill_model,
)


def _synthetic(n: int = 360, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        rng.normal(size=(n, len(FILL_FEATURES))), columns=FILL_FEATURES)
    frame["touch_bar"] = rng.integers(1, 5, size=n)
    logit = 1.4 * frame["pre_vol_ratio"] - 0.2
    prob = 1.0 / (1.0 + np.exp(-logit))
    frame["fill_w1"] = rng.binomial(1, prob)
    dates = pd.date_range("2025-02-01", periods=36).strftime("%Y%m%d")
    frame["date"] = np.repeat(dates, 10)
    return frame


def test_fill_label():
    assert fill_label(1) == "fill_w1"
    assert fill_label(3) == "fill_w3"


def test_fit_and_evaluate_fill_model():
    frame = _synthetic()
    train, calib, test = frame.iloc[:220], frame.iloc[220:290], frame.iloc[290:]

    assert train["fill_w1"].nunique() == 2
    assert test["fill_w1"].nunique() == 2

    model = fit_fill_model(train, calib, wait=1, kind="logistic")
    prob = model.predict_proba(test)
    pred = model.predict(test)

    assert prob.shape == (len(test),)
    assert np.all(np.isfinite(prob))
    assert np.all((prob > 0.0) & (prob < 1.0))
    assert set(np.unique(pred)).issubset({0, 1})
    assert model.metadata["n_train"] == len(train)
    assert model.metadata["n_calib"] == len(calib)

    out = evaluate_fill_model(model, test)
    assert out["n"] == len(test)
    assert out["n_days"] == test["date"].nunique()
    assert out["base_rate"] == pytest.approx(test["fill_w1"].mean())
    assert 0.0 <= out["brier"] <= 1.0
    assert set(out["top_k_fill_rate"]) == {1, 2, 3, 5}
    for stats in out["top_k_fill_rate"].values():
        assert stats["n"] > 0
        assert 0.0 <= stats["fill_rate"] <= 1.0
        assert stats["ci_low"] <= stats["fill_rate"] <= stats["ci_high"]


def test_fit_fill_model_requires_label():
    frame = _synthetic()
    train = frame.iloc[:220].drop(columns="fill_w1")
    with pytest.raises(ValueError, match="fill_w1"):
        fit_fill_model(train, frame.iloc[220:290], wait=1)
