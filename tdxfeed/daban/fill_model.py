"""成交流模型：P(开板成交 | 触板信号)，只用于诊断与回测辅助。

为什么单独建模
--------------
打板的成交不是随机的：强板不给你成交，弱板才成交。若只用“成交样本”算收益，
会系统性高估策略。这里用触板时点特征预测 ``fill_w{w}``，检查成交概率是否
可预测，以及高成交概率信号是否恰好是低质量信号（逆向选择）。

注意：历史数据没有逐笔队列/封单额，模型只能学“板会不会开”，不能学“排队排不排
得到”。因此即使 AUC 很高，也不代表实盘能按该概率成交。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from tdxfeed.daban.seal_classifier import (
    SEAL_FEATURES,
    build_estimator,
    reliability_table,
    wilson_interval,
)

__all__ = [
    "FillModel",
    "evaluate_fill_model",
    "fill_label",
    "fit_fill_model",
]

FILL_FEATURES = list(SEAL_FEATURES)


def fill_label(wait: int = 1) -> str:
    return f"fill_w{int(wait)}"


@dataclass
class FillModel:
    estimator: Any
    calibrator: IsotonicRegression | LogisticRegression | None
    features: list[str]
    kind: str
    wait: int
    threshold: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        raw = self.estimator.predict_proba(frame[self.features])[:, 1]
        if self.calibrator is not None:
            raw = self.calibrator.predict(raw)
        return np.clip(np.asarray(raw, dtype=float), 1e-6, 1.0 - 1e-6)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(frame) >= self.threshold).astype(int)


def fit_fill_model(
    train: pd.DataFrame,
    calib: pd.DataFrame,
    *,
    wait: int = 1,
    kind: str = "logistic",
    method: str = "isotonic",
) -> FillModel:
    """在 train 上拟合成交概率，在 calib 上校准。"""
    label = fill_label(wait)
    if label not in train.columns:
        raise ValueError(f"缺少成交标签 {label}")
    est = build_estimator(kind)
    est.fit(train[FILL_FEATURES], train[label].astype(int))
    p_cal = est.predict_proba(calib[FILL_FEATURES])[:, 1]
    if method == "isotonic":
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(p_cal, calib[label].astype(int))
    elif method == "platt":
        z = np.log(np.clip(p_cal, 1e-6, 1 - 1e-6)
                   / (1 - np.clip(p_cal, 1e-6, 1 - 1e-6)))
        calibrator = LogisticRegression(C=1e6, max_iter=1000)
        calibrator.fit(z[:, None], calib[label].astype(int))
    else:
        raise ValueError(f"未知校准方法: {method}")
    return FillModel(
        estimator=est,
        calibrator=calibrator,
        features=list(FILL_FEATURES),
        kind=kind,
        wait=int(wait),
        metadata={
            "method": method,
            "label": label,
            "n_train": len(train),
            "n_calib": len(calib),
        },
    )


def evaluate_fill_model(
    model: FillModel,
    test: pd.DataFrame,
    *,
    wait: int | None = None,
) -> dict[str, Any]:
    """成交概率的 OOS 质量与每日高概率组的实际成交率。"""
    wait = int(wait or model.wait)
    label = fill_label(wait)
    y = test[label].astype(int).to_numpy()
    p = model.predict_proba(test)
    frame = pd.DataFrame({"date": test["date"].astype(str).to_numpy(),
                          "y": y, "p": p})
    out: dict[str, Any] = {
        "n": len(test),
        "n_days": int(frame["date"].nunique()),
        "base_rate": float(y.mean()) if len(y) else float("nan"),
        "brier": float(brier_score_loss(y, p)) if len(y) else float("nan"),
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1]))
        if len(y) else float("nan"),
        "auc": float(roc_auc_score(y, p)) if len(y) and y.min() != y.max() else float("nan"),
        "reliability": reliability_table(y, p),
        "top_k_fill_rate": {},
    }
    frame["rank"] = frame.groupby("date")["p"].rank(ascending=False, method="first")
    for k in (1, 2, 3, 5):
        sel = frame[frame["rank"] <= k]
        n = len(sel)
        hits = int(sel["y"].sum())
        lo, hi = wilson_interval(hits, n)
        out["top_k_fill_rate"][k] = {
            "n": n,
            "events_per_day": n / frame["date"].nunique() if frame["date"].nunique() else float("nan"),
            "fill_rate": float(sel["y"].mean()) if n else float("nan"),
            "ci_low": lo,
            "ci_high": hi,
        }
    return out
