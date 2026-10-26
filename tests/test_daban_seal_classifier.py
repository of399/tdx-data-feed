"""封板分类器：切分、校准、阈值与精确率工具测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tdxfeed.daban.seal_classifier import (
    LABEL,
    SEAL_FEATURES,
    evaluate_scores,
    fit_calibrated_model,
    precision_at_k_per_day,
    select_threshold,
    threshold_metrics,
    time_split,
    wilson_interval,
)


def _synthetic(n: int = 420, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        rng.normal(size=(n, len(SEAL_FEATURES))), columns=SEAL_FEATURES)
    frame["touch_bar"] = rng.integers(1, 5, size=n)
    logit = 1.1 * frame["pre_high_to_limit"] + 0.6 * frame["cb_prev"] - 0.2
    prob = 1.0 / (1.0 + np.exp(-logit))
    frame[LABEL] = rng.binomial(1, prob)
    dates = pd.date_range("2025-01-01", periods=42).strftime("%Y%m%d")
    frame["date"] = np.repeat(dates, 10)
    return frame


def test_time_split_by_trading_day_without_overlap():
    dates = [f"2025-01-{i:02d}" for i in range(1, 11)]
    frame = pd.DataFrame({"date": np.repeat(dates, 2), "x": range(20)})
    parts = time_split(frame)

    sets = {k: set(v["date"]) for k, v in parts.items()}
    assert len(sets["train"]) == 5
    assert len(sets["calib"]) == 1
    assert len(sets["val"]) == 1
    assert len(sets["test"]) == 3
    assert set().union(*sets.values()) == set(dates)
    for a in parts:
        for b in parts:
            if a < b:
                assert sets[a].isdisjoint(sets[b])


def test_wilson_interval_bounds():
    lo, hi = wilson_interval(8, 10)
    assert 0.0 < lo < 0.8 < hi < 1.0
    assert wilson_interval(0, 0)[0] != wilson_interval(0, 0)[0]  # NaN

    lo0, hi0 = wilson_interval(0, 10)
    assert lo0 == pytest.approx(0.0)
    assert 0.0 < hi0 < 1.0


def test_threshold_metrics_precision_recall_and_rate():
    out = threshold_metrics(
        y=[1, 1, 0, 0],
        p=[0.9, 0.8, 0.7, 0.6],
        dates=["d1", "d1", "d2", "d2"],
        threshold=0.75,
    )
    assert out["n"] == 2
    assert out["tp"] == 2
    assert out["fp"] == 0
    assert out["precision"] == pytest.approx(1.0)
    assert out["recall"] == pytest.approx(1.0)
    assert out["events_per_day"] == pytest.approx(1.0)
    assert out["base_rate"] == pytest.approx(0.5)


def _per_day_dates(n_days: int, per_day: int) -> np.ndarray:
    return np.repeat(
        [f"2026-01-{i:02d}" for i in range(1, n_days + 1)], per_day)


def test_select_threshold_finds_feasible_high_precision_cut():
    rng = np.random.default_rng(7)
    n_days, per_day = 20, 10
    dates = _per_day_dates(n_days, per_day)
    y = np.tile([1] * 7 + [0] * 3, n_days)
    p = np.where(y == 1, rng.uniform(0.82, 0.97, len(y)),
                 rng.uniform(0.10, 0.55, len(y)))

    out = select_threshold(
        y, p, dates, target_precision=0.80,
        min_events_per_day=0.5, min_events=30)
    assert out["gate_met"] is True
    assert out["precision"] >= 0.80
    assert out["n"] >= 30


def test_select_threshold_flags_infeasible_cut():
    dates = _per_day_dates(20, 10)
    p = np.tile(np.linspace(0.9, 0.0, 10), 20)
    y = np.tile([0, 1] * 5, 20)  # 高概率组全是 0，任一截面精确率都不超过 0.5
    out = select_threshold(
        y, p, dates, target_precision=0.95,
        min_events_per_day=0.5, min_events=10)
    assert out["gate_met"] is False


def test_precision_at_k_per_day_ranks_within_each_day():
    y = np.array([1, 0, 0, 0, 0, 1])
    p = np.array([0.9, 0.8, 0.7, 0.9, 0.8, 0.7])
    dates = np.array(["d1"] * 3 + ["d2"] * 3)

    out = precision_at_k_per_day(y, p, dates, ks=(1, 2))
    assert out[1]["n"] == 2
    assert out[1]["precision"] == pytest.approx(0.5)
    assert out[2]["n"] == 4
    assert out[2]["precision"] == pytest.approx(0.25)


def test_fit_calibrated_model_and_evaluate_scores():
    frame = _synthetic()
    train, calib = frame.iloc[:250], frame.iloc[250:330]
    test = frame.iloc[330:]

    assert train[LABEL].nunique() == 2
    assert calib[LABEL].nunique() == 2
    assert test[LABEL].nunique() == 2

    model = fit_calibrated_model(train, calib, kind="logistic")
    prob = model.predict_proba(test)
    pred = model.predict(test)

    assert prob.shape == (len(test),)
    assert np.all(np.isfinite(prob))
    assert np.all((prob > 0.0) & (prob < 1.0))
    assert set(np.unique(pred)).issubset({0, 1})
    assert model.metadata["n_train"] == len(train)
    assert model.metadata["n_calib"] == len(calib)

    scores = evaluate_scores(test[LABEL], prob, test["date"], threshold=0.5)
    assert scores["n"] == len(test)
    assert 0.0 <= scores["brier"] <= 1.0
    assert set(scores["precision_at_k"]) == {1, 2, 3, 5}
    assert "threshold_metrics" in scores
    assert scores["threshold_metrics"]["n"] == int((prob >= 0.5).sum())

    with pytest.raises(ValueError):
        fit_calibrated_model(train, calib, kind="unknown")
