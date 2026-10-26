"""封板分类器：P(收盘封板 | 触板)，只用触板时点可得特征。

设计约束
--------
- 标签 ``y_sealed_close`` 只在收盘后可知；特征不得包含封板后信息。
- 目标是 **阈值处的精确率**，不是 AUC：实盘只买被选中的少数事件。
- 时间切分 + 独立校准集 + 独立测试集；阈值在验证集选，测试集只评一次。
- 校准用 isotonic（可切 platt），输出概率必须能直接当“封板概率”用。

本模块不负责成交模拟；``execution_backtest`` 会消费这里的概率。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

__all__ = [
    "CATEGORICAL_FEATURES",
    "SEAL_FEATURES",
    "CalibratedSealModel",
    "build_estimator",
    "compare_models",
    "evaluate_scores",
    "fit_calibrated_model",
    "precision_at_k_per_day",
    "reliability_table",
    "select_threshold",
    "threshold_metrics",
    "time_split",
    "wilson_interval",
]

LABEL = "y_sealed_close"

# 顺序即报告顺序；全部来自触板前已收盘 K 线/日线或当日开盘。
SEAL_FEATURES = [
    "touch_bar",
    "open_gap",
    "pre_ret",
    "pre_high_to_limit",
    "pre_range_pct",
    "pre_vol_ratio",
    "pre_amount_ratio",
    "touch_open_gap",
    "cb_prev",
    "is_st",
    "is_new",
    "ratio",
    "log_prior_amount",
    "amount_ratio_5d",
    "amount_ratio_20d",
    "log_price",
    "ret_5d",
    "ret_20d",
    "vol_20d",
    "dist_high_20d",
    "dist_ma5",
    "dist_ma10",
    "dist_ma20",
    "big_up_20d",
    "max_ret_20d",
    "prior_day_ret",
    "sec_n",
    "sec_limitup_n",
    "sec_median_ret",
    "sec_leader_pct",
    "sec_rank_pct",
    "sec_breadth",
    "sec_score",
    "mkt_n",
    "mkt_limitup_n",
    "mkt_median_ret",
    "mkt_breadth",
]

CATEGORICAL_FEATURES = ["touch_bar"]


@dataclass
class CalibratedSealModel:
    """已校准的封板概率模型。"""

    estimator: Pipeline
    calibrator: IsotonicRegression | None
    features: list[str]
    kind: str
    threshold: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        raw = self.estimator.predict_proba(frame[self.features])[:, 1]
        if self.calibrator is not None:
            raw = self.calibrator.predict(raw)
        return np.clip(np.asarray(raw, dtype=float), 1e-6, 1.0 - 1e-6)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(frame) >= self.threshold).astype(int)


def build_estimator(kind: str = "logistic", *, random_state: int = 42) -> Pipeline:
    """构建未校准的分类器。"""
    if kind == "logistic":
        numeric = [c for c in SEAL_FEATURES if c not in CATEGORICAL_FEATURES]
        prep = ColumnTransformer(
            [
                ("num", Pipeline([
                    ("impute", SimpleImputer(strategy="median")),
                    ("scale", StandardScaler()),
                ]), numeric),
                ("cat", Pipeline([
                    ("impute", SimpleImputer(strategy="most_frequent")),
                    ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                ]), CATEGORICAL_FEATURES),
            ],
            remainder="drop",
        )
        clf = LogisticRegression(C=0.5, max_iter=2000, solver="lbfgs")
    elif kind == "gbm":
        prep = "passthrough"
        clf = HistGradientBoostingClassifier(
            max_iter=250,
            learning_rate=0.05,
            max_leaf_nodes=15,
            min_samples_leaf=120,
            l2_regularization=1.0,
            random_state=random_state,
        )
    else:
        raise ValueError(f"未知模型类型: {kind}")
    return Pipeline([("prep", prep), ("clf", clf)])


def fit_calibrated_model(
    train: pd.DataFrame,
    calib: pd.DataFrame,
    *,
    kind: str = "logistic",
    features: list[str] | None = None,
    method: str = "isotonic",
    random_state: int = 42,
) -> CalibratedSealModel:
    """在 train 上拟合，在独立 calib 上校准。"""
    feats = list(features or SEAL_FEATURES)
    est = build_estimator(kind, random_state=random_state)
    est.fit(train[feats], train[LABEL].astype(int))
    p_cal = est.predict_proba(calib[feats])[:, 1]
    if method == "isotonic":
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(p_cal, calib[LABEL].astype(int))
    elif method == "platt":
        calibrator = LogisticRegression(C=1e6, max_iter=1000)
        calibrator.fit(np.log(np.clip(p_cal, 1e-6, 1 - 1e-6)
                              / (1 - np.clip(p_cal, 1e-6, 1 - 1e-6)))[:, None],
                       calib[LABEL].astype(int))
    else:
        raise ValueError(f"未知校准方法: {method}")
    return CalibratedSealModel(
        estimator=est,
        calibrator=calibrator,
        features=feats,
        kind=kind,
        metadata={
            "method": method,
            "n_train": len(train),
            "n_calib": len(calib),
            "train_dates": [str(train["date"].min()), str(train["date"].max())],
            "calib_dates": [str(calib["date"].min()), str(calib["date"].max())],
        },
    )


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项精确率/胜率的 Wilson 置信区间。"""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)) / denom
    return (float(max(0.0, center - half)), float(min(1.0, center + half)))


def threshold_metrics(
    y: np.ndarray | pd.Series,
    p: np.ndarray | pd.Series,
    dates: np.ndarray | pd.Series,
    threshold: float,
) -> dict[str, Any]:
    """固定阈值下的分类与覆盖率指标。"""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    d = np.asarray(dates).astype(str)
    pred = p >= threshold
    n = int(pred.sum())
    n_days = int(pd.unique(d).size)
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    tn = int((~pred & (y == 0)).sum())
    precision = tp / n if n else float("nan")
    recall = tp / int(y.sum()) if y.sum() else float("nan")
    lo, hi = wilson_interval(tp, n)
    return {
        "threshold": float(threshold),
        "n": n,
        "n_selected": n,
        "n_days": n_days,
        "events_per_day": n / n_days if n_days else float("nan"),
        "precision": float(precision),
        "precision_ci_low": lo,
        "precision_ci_high": hi,
        "recall": float(recall),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "base_rate": float(y.mean()) if len(y) else float("nan"),
    }


def select_threshold(
    y: np.ndarray | pd.Series,
    p: np.ndarray | pd.Series,
    dates: np.ndarray | pd.Series,
    *,
    target_precision: float = 0.80,
    min_events_per_day: float = 1.0,
    min_events: int = 100,
) -> dict[str, Any]:
    """在验证集选阈值：满足精确率与样本量前提下，选覆盖最多事件的阈值。"""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    d = np.asarray(dates).astype(str)
    if len(y) == 0:
        raise ValueError("验证集为空")
    candidates = np.unique(np.concatenate([
        np.quantile(p, np.linspace(0.0, 0.995, 400)),
        np.linspace(0.30, 0.99, 140),
    ]))
    rows = [threshold_metrics(y, p, d, float(t)) for t in candidates]
    sample_ok = [r for r in rows if r["n"] >= min_events]
    if not sample_ok:
        sample_ok = [r for r in rows if r["events_per_day"] >= min_events_per_day]
    feasible = [r for r in sample_ok if r["precision"] >= target_precision]
    if feasible:
        best = max(feasible, key=lambda r: (r["n"], -r["threshold"]))
        best["gate_met"] = True
    elif sample_ok:
        best = max(sample_ok, key=lambda r: (r["precision"], r["n"]))
        best["gate_met"] = False
    else:
        best = max(rows, key=lambda r: r["precision"])
        best["gate_met"] = False
    return best


def precision_at_k_per_day(
    y: np.ndarray | pd.Series,
    p: np.ndarray | pd.Series,
    dates: np.ndarray | pd.Series,
    ks: tuple[int, ...] = (1, 2, 3, 5),
) -> dict[int, dict[str, Any]]:
    """每日按概率取前 K 个事件的精确率（不依赖固定阈值）。"""
    frame = pd.DataFrame({"y": np.asarray(y, dtype=int),
                          "p": np.asarray(p, dtype=float),
                          "date": np.asarray(dates).astype(str)})
    frame["rank"] = frame.groupby("date")["p"].rank(ascending=False, method="first")
    out: dict[int, dict[str, Any]] = {}
    for k in ks:
        sel = frame[frame["rank"] <= k]
        tp = int(sel["y"].sum())
        n = len(sel)
        lo, hi = wilson_interval(tp, n)
        out[k] = {
            "n": n,
            "events_per_day": n / frame["date"].nunique() if frame["date"].nunique() else float("nan"),
            "precision": float(sel["y"].mean()) if n else float("nan"),
            "precision_ci_low": lo,
            "precision_ci_high": hi,
        }
    return out


def reliability_table(
    y: np.ndarray | pd.Series,
    p: np.ndarray | pd.Series,
    *,
    bins: int = 10,
) -> list[dict[str, Any]]:
    """可靠性曲线：每个概率分箱的样本数、平均预测概率、实际封板率。"""
    frame = pd.DataFrame({"y": np.asarray(y, dtype=int),
                          "p": np.asarray(p, dtype=float)})
    frame["bin"] = pd.qcut(frame["p"].rank(method="first"), bins,
                           labels=False, duplicates="drop")
    out = []
    for b, g in frame.groupby("bin", sort=True):
        out.append({
            "bin": int(b),
            "n": len(g),
            "mean_pred": float(g["p"].mean()),
            "actual": float(g["y"].mean()),
        })
    return out


def evaluate_scores(
    y: np.ndarray | pd.Series,
    p: np.ndarray | pd.Series,
    dates: np.ndarray | pd.Series,
    *,
    threshold: float | None = None,
    ks: tuple[int, ...] = (1, 2, 3, 5),
    bins: int = 10,
) -> dict[str, Any]:
    """整体评分：概率质量 + 阈值指标 + 每日 Top-K + 可靠性。"""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    d = np.asarray(dates).astype(str)
    out: dict[str, Any] = {
        "n": len(y),
        "n_days": int(pd.unique(d).size),
        "base_rate": float(y.mean()) if len(y) else float("nan"),
        "brier": float(brier_score_loss(y, p)) if len(y) else float("nan"),
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6),
                                   labels=[0, 1])) if len(y) else float("nan"),
        "auc": float(roc_auc_score(y, p))
        if len(y) and y.min() != y.max() else float("nan"),
        "precision_at_k": precision_at_k_per_day(y, p, d, ks),
        "reliability": reliability_table(y, p, bins=bins),
    }
    if threshold is not None:
        out["threshold_metrics"] = threshold_metrics(y, p, d, threshold)
    return out


def time_split(
    frame: pd.DataFrame,
    *,
    train_frac: float = 0.50,
    calib_frac: float = 0.15,
    val_frac: float = 0.15,
) -> dict[str, pd.DataFrame]:
    """按交易日切分 train/calib/val/test，确保同一天不跨集合。"""
    dates = np.array(sorted(frame["date"].astype(str).unique()))
    n = len(dates)
    i1 = int(n * train_frac)
    i2 = i1 + int(n * calib_frac)
    i3 = i2 + int(n * val_frac)
    return {
        "train": frame[frame["date"].astype(str).isin(dates[:i1])],
        "calib": frame[frame["date"].astype(str).isin(dates[i1:i2])],
        "val": frame[frame["date"].astype(str).isin(dates[i2:i3])],
        "test": frame[frame["date"].astype(str).isin(dates[i3:])],
    }


def compare_models(
    train: pd.DataFrame,
    calib: pd.DataFrame,
    val: pd.DataFrame,
    *,
    kinds: tuple[str, ...] = ("logistic", "gbm"),
) -> dict[str, Any]:
    """在验证集比较候选模型；按 Brier 选择（越小越好）。"""
    results: dict[str, Any] = {}
    best_kind, best_brier = None, float("inf")
    for kind in kinds:
        model = fit_calibrated_model(train, calib, kind=kind)
        p = model.predict_proba(val)
        scores = evaluate_scores(val[LABEL], p, val["date"])
        results[kind] = {"model": model, "scores": scores}
        if scores["brier"] < best_brier:
            best_kind, best_brier = kind, scores["brier"]
    return {"results": results, "best_kind": best_kind, "best_brier": best_brier}
