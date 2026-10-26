#!/usr/bin/env python
"""Stage 1：封板分类器 OOS 验证（硬门槛：precision≥80%，≥1 event/day）。

流程
----
1. 读取 Stage 0 事件表，限定可执行池：非一字、非 ST、非新股。
2. 按交易日 50/15/15/20 切分 train/calib/val/test。
3. 逻辑回归为主模型（预指定），GBM 仅作敏感性对照；均在独立 calib 上 isotonic 校准。
4. 在 val 上选阈值（满足 precision 与样本量前提下覆盖最多事件）。
5. 在 test 上只评一次，输出 Wilson 区间、每日 Top-K、可靠性曲线、分月稳定性。
6. 保存模型与 test 概率，供 Stage 2 成交感知回测使用。

用法::

    venv/bin/python scripts/daban_seal_research.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.seal_classifier import (  # noqa: E402
    LABEL,
    SEAL_FEATURES,
    evaluate_scores,
    fit_calibrated_model,
    select_threshold,
    time_split,
)

EVENTS = ROOT / "data/daban/event_table_min60_20261022.parquet"
OUT_JSON = ROOT / "data/daban/seal_classifier_20261022.json"
OUT_MODEL = ROOT / "data/daban/seal_model_20261022.joblib"
OUT_SCORES = ROOT / "data/daban/seal_scores_20261022.parquet"


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def load_events(path: Path) -> pd.DataFrame:
    d = pd.read_parquet(path)
    d["date"] = d["date"].astype(str)
    d["code"] = d["code"].astype(str).str.zfill(6)
    d = d[(d["open_limit"] == 0) & (d["is_st"] == 0) & (d["is_new"] == 0)].copy()
    d = d[d[LABEL].notna()].copy()
    missing = [c for c in SEAL_FEATURES if c not in d.columns]
    if missing:
        raise ValueError(f"事件表缺少特征: {missing}")
    return d


def monthly_precision(selected: pd.DataFrame) -> list[dict]:
    if selected.empty:
        return []
    selected = selected.copy()
    selected["ym"] = selected["date"].str[:6]
    out = []
    for ym, g in selected.groupby("ym", sort=True):
        out.append({
            "month": f"{ym[:4]}-{ym[4:]}",
            "n": len(g),
            "precision": float(g[LABEL].mean()),
        })
    return out


def subset_precision(selected: pd.DataFrame, col: str) -> list[dict]:
    if selected.empty:
        return []
    out = []
    for val, g in selected.groupby(col, sort=True):
        out.append({
            col: str(val),
            "n": len(g),
            "precision": float(g[LABEL].mean()),
        })
    return out


def run(events: pd.DataFrame) -> tuple[dict, pd.DataFrame, object]:
    splits = time_split(events)
    for name, part in splits.items():
        print(f"[info] {name:5s}: {len(part):6,} 事件 / {part['date'].nunique():3d} 天 "
              f"({part['date'].min()}~{part['date'].max()}) 封板率 "
              f"{part[LABEL].mean() * 100:.1f}%")
    train, calib, val, test = splits["train"], splits["calib"], splits["val"], splits["test"]

    models = {}
    val_scores = {}
    for kind in ("logistic", "gbm"):
        model = fit_calibrated_model(train, calib, kind=kind)
        p_val = model.predict_proba(val)
        val_scores[kind] = evaluate_scores(val[LABEL], p_val, val["date"])
        models[kind] = model
        print(f"[fit] {kind:8s} val Brier={val_scores[kind]['brier']:.4f} "
              f"AUC={val_scores[kind]['auc']:.4f}")

    # 预指定逻辑回归为主模型；GBM 只做敏感性对照，避免用验证集挑模型再挑阈值。
    primary = "logistic"
    model = models[primary]
    p_val = model.predict_proba(val)
    threshold_info = select_threshold(
        val[LABEL], p_val, val["date"],
        target_precision=0.80, min_events_per_day=1.0, min_events=100,
    )
    threshold = float(threshold_info["threshold"])
    model.threshold = threshold
    print(f"[thr] {threshold:.4f} val precision={threshold_info['precision']:.3f} "
          f"n={threshold_info['n']} events/day={threshold_info['events_per_day']:.2f} "
          f"gate_met={threshold_info['gate_met']}")

    test_scores = {}
    selected = {}
    for kind in ("logistic", "gbm"):
        m = models[kind]
        p_test = m.predict_proba(test)
        use_thr = threshold if kind == primary else None
        if use_thr is None:
            other = select_threshold(
                val[LABEL], models[kind].predict_proba(val), val["date"],
                target_precision=0.80, min_events_per_day=1.0, min_events=100,
            )
            use_thr = float(other["threshold"])
            m.threshold = use_thr
        scores = evaluate_scores(test[LABEL], p_test, test["date"], threshold=use_thr)
        scores["model_threshold"] = use_thr
        test_scores[kind] = scores
        sel = test[p_test >= use_thr].copy()
        sel["p"] = p_test[p_test >= use_thr]
        selected[kind] = sel
        tm = scores["threshold_metrics"]
        print(f"[test] {kind:8s} thr={use_thr:.4f} n={tm['n']} "
              f"precision={tm['precision']:.3f} "
              f"[{tm['precision_ci_low']:.3f},{tm['precision_ci_high']:.3f}] "
              f"events/day={tm['events_per_day']:.2f}")

    tm = test_scores[primary]["threshold_metrics"]
    gate_passed = bool(
        tm["precision"] >= 0.80
        and tm["events_per_day"] >= 1.0
        and tm["n"] >= 100
    )
    reasons = []
    if tm["precision"] < 0.80:
        reasons.append(f"precision {tm['precision']:.3f} < 0.80")
    if tm["events_per_day"] < 1.0:
        reasons.append(f"events/day {tm['events_per_day']:.2f} < 1.0")
    if tm["n"] < 100:
        reasons.append(f"n {tm['n']} < 100（样本不足）")

    result = {
        "stage": "seal_classifier",
        "event_table": str(EVENTS),
        "universe": {
            "filter": "touched & non-open-limit & non-ST & non-new",
            "n_events": len(events),
            "n_days": int(events["date"].nunique()),
            "n_codes": int(events["code"].nunique()),
            "date_min": str(events["date"].min()),
            "date_max": str(events["date"].max()),
            "base_rate": float(events[LABEL].mean()),
        },
        "split": {
            name: {
                "n": len(part),
                "n_days": int(part["date"].nunique()),
                "date_min": str(part["date"].min()),
                "date_max": str(part["date"].max()),
                "base_rate": float(part[LABEL].mean()),
            }
            for name, part in splits.items()
        },
        "validation_scores": val_scores,
        "primary_model": primary,
        "threshold_selection": threshold_info,
        "threshold": threshold,
        "test_scores": test_scores,
        "test_selected_monthly": monthly_precision(selected[primary]),
        "test_selected_by_touch_bar": subset_precision(selected[primary], "touch_bar"),
        "gate": {
            "target_precision": 0.80,
            "min_events_per_day": 1.0,
            "min_events": 100,
            "passed": gate_passed,
            "reasons": reasons,
        },
        "caveats": [
            "min60 只有 4 根 K，无法识别 10:00 前首封",
            "历史无封单额、L2 队列、实时板块涨停家数",
            "行业映射为当前版本，存在历史归属/幸存者偏差",
            "逻辑回归预指定；GBM 仅敏感性对照",
        ],
    }

    score_cols = ["code", "date", "touch_bar", "first_touch", LABEL,
                  "fill_w1", "entry_w1", "fill_gap_w1",
                  "ret_fill_open_gross_w1", "ret_fill_close_gross_w1",
                  "next_open", "next_close", "next2_open", "next_lim_dn",
                  "cb_prev", "sec_score", "mkt_breadth"]
    test_scores_df = test[score_cols].copy()
    test_scores_df["seal_prob"] = model.predict_proba(test)
    test_scores_df["split"] = "test"
    val_scores_df = val[score_cols].copy()
    val_scores_df["seal_prob"] = p_val
    val_scores_df["split"] = "val"
    score_df = pd.concat([val_scores_df, test_scores_df], ignore_index=True)
    return result, score_df, model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-model", type=Path, default=OUT_MODEL)
    ap.add_argument("--out-scores", type=Path, default=OUT_SCORES)
    args = ap.parse_args()

    if not args.events.exists():
        print(f"[error] 缺少 {args.events}，先跑 scripts/daban_event_table.py")
        return 1
    events = load_events(args.events)
    print(f"[info] 可执行触板 {len(events):,} / {events['code'].nunique():,} 只 / "
          f"{events['date'].nunique()} 天")
    result, score_df, model = run(events)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    joblib.dump(model, args.out_model)
    score_df.to_parquet(args.out_scores, index=False)
    print(f"[ok] {args.out_json}")
    print(f"[ok] {args.out_model}")
    print(f"[ok] {args.out_scores}  {len(score_df):,} 行")
    gate = result["gate"]
    print(f"[gate] Stage 1 {'PASS' if gate['passed'] else 'FAIL'}"
          + ("" if gate["passed"] else f"：{'; '.join(gate['reasons'])}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
