#!/usr/bin/env python
"""Stage 2（诊断）：成交感知回测 + 逆向选择量化。

前置：Stage 1 封板分类器已产出 OOS 概率。Stage 2 只使用 test 段概率，
不再拟合封板模型；成交流模型只在 train/calib 上拟合、test 上评估。

规则
----
- baseline：test 段所有可成交时点触板（首触 K≤3）
- top1/2/3/5：每日按封板概率取前 K
- stage1_threshold：Stage 1 验证集选出的固定概率阈值
- oracle：事后知道收盘封死（不可执行，仅上界）
- 成本敏感性：baseline/top3 在 30bp 双边成本下重跑
- 等待敏感性：baseline wait1/2/3

用法::

    venv/bin/python scripts/daban_exec_research.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.daban.execution_backtest import (  # noqa: E402
    ExecutionConfig,
    run_rule,
    simulate_signals,
)
from tdxfeed.daban.fill_model import evaluate_fill_model, fit_fill_model  # noqa: E402
from tdxfeed.daban.seal_classifier import LABEL, time_split  # noqa: E402

EVENTS = ROOT / "data/daban/event_table_min60_20261022.parquet"
SEAL_JSON = ROOT / "data/daban/seal_classifier_20261022.json"
SCORES = ROOT / "data/daban/seal_scores_20261022.parquet"
OUT = ROOT / "data/daban/execution_backtest_20261022.json"


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def load_events(path: Path, scores_path: Path, seal_json: Path) -> tuple[pd.DataFrame, float, bool]:
    d = pd.read_parquet(path)
    d["date"] = d["date"].astype(str)
    d["code"] = d["code"].astype(str).str.zfill(6)
    d = d[(d["open_limit"] == 0) & (d["is_st"] == 0) & (d["is_new"] == 0)].copy()
    d = d[d[LABEL].notna()].copy()
    scores = pd.read_parquet(scores_path)
    scores["date"] = scores["date"].astype(str)
    scores["code"] = scores["code"].astype(str).str.zfill(6)
    test_scores = scores[scores["split"] == "test"][["code", "date", "seal_prob"]]
    if test_scores.duplicated(["code", "date"]).any():
        raise ValueError("test 概率存在重复 code/date")
    d = d.merge(test_scores, on=["code", "date"], how="left", validate="one_to_one")
    seal = json.loads(seal_json.read_text(encoding="utf-8"))
    return d, float(seal["threshold"]), bool(seal["gate"]["passed"])


def run(events: pd.DataFrame, threshold: float, stage1_passed: bool) -> dict:
    splits = time_split(events)
    test = splits["test"].copy()
    test = test[test["first_touch"] <= 3].copy()  # 首触 K=4 之后没有可成交 K
    missing_p = int(test["seal_prob"].isna().sum())
    if missing_p:
        raise ValueError(f"test 有 {missing_p} 条缺少 seal_prob")
    print(f"[info] test 可成交时点触板 {len(test):,} / {test['date'].nunique()} 天")

    # 成交流模型：train 拟合、calib 校准、test 评估
    train_f = splits["train"]
    calib_f = splits["calib"]
    train_f = train_f[train_f["first_touch"] <= 3]
    calib_f = calib_f[calib_f["first_touch"] <= 3]
    fill_model = fit_fill_model(train_f, calib_f, wait=1, kind="logistic")
    fill_eval = evaluate_fill_model(fill_model, test, wait=1)
    test["fill_prob"] = fill_model.predict_proba(test)
    print(f"[fill] OOS AUC={fill_eval['auc']:.3f} Brier={fill_eval['brier']:.4f} "
          f"base={fill_eval['base_rate']:.3f}")

    base = ExecutionConfig(wait=1, exit_mode="open")
    rules: dict[str, dict] = {}

    def add(name: str, cfg: ExecutionConfig, frame: pd.DataFrame | None = None) -> None:
        summary, _ = run_rule(frame if frame is not None else test, name, cfg)
        rules[name] = summary
        print(f"[rule] {name:18s} signals={summary['n_signals']:4d} "
              f"fills={summary['n_fills']:4d} fill_rate={summary['fill_rate']:.1%} "
              f"net={summary['avg_net']:+.3%} t={summary['t']:.2f}")

    add("baseline", base)
    for k in (1, 2, 3, 5):
        add(f"top{k}", ExecutionConfig(wait=1, exit_mode="open", top_k_per_day=k))
    add("stage1_threshold", ExecutionConfig(wait=1, exit_mode="open", min_prob=threshold))
    oracle = test.copy()
    oracle["seal_prob"] = oracle[LABEL].astype(float)
    add("oracle", ExecutionConfig(wait=1, exit_mode="open", min_prob=0.5), oracle)
    add("baseline_close", ExecutionConfig(wait=1, exit_mode="close"))
    add("top3_close", ExecutionConfig(wait=1, exit_mode="close", top_k_per_day=3))
    add("baseline_wait2", ExecutionConfig(wait=2, exit_mode="open"))
    add("baseline_wait3", ExecutionConfig(wait=3, exit_mode="open"))
    high_cost = ExecutionConfig(wait=1, exit_mode="open", fee_bps=5.0,
                                stamp_bps=10.0, slip_bps=10.0)
    add("baseline_cost30", high_cost)
    add("top3_cost30", ExecutionConfig(wait=1, exit_mode="open", top_k_per_day=3,
                                       fee_bps=5.0, stamp_bps=10.0, slip_bps=10.0))

    # 成交概率分位与收益：只看 baseline 成交样本
    merged_test = test.copy()
    trades = simulate_signals(merged_test.assign(seal_prob=0.0), base)
    ok = trades[trades["status"] == "ok"].copy()
    ok["date"] = ok["signal_date"]
    ok = ok.merge(merged_test[["code", "date", "fill_prob"]], on=["code", "date"], how="left")
    quint = []
    if not ok.empty:
        ok["q"] = pd.qcut(ok["fill_prob"].rank(method="first"), 5,
                          labels=False, duplicates="drop")
        for q, g in ok.groupby("q", sort=True):
            quint.append({
                "quintile": int(q) + 1,
                "n": len(g),
                "mean_fill_prob": float(g["fill_prob"].mean()),
                "avg_net": float(g["net"].mean()),
                "seal_rate": float(g["y_sealed_close"].mean()),
            })

    # Stage 2 门槛只认预指定口径：wait1、T+1 开盘卖、非 oracle。
    # close/wait2/wait3/cost30 只作敏感性，不能用来“挑一个正结果”。
    primary_names = ["baseline", "top1", "top2", "top3", "top5", "stage1_threshold"]
    primary = {k: rules[k] for k in primary_names}
    passed = [
        k for k, v in primary.items()
        if np.isfinite(v["avg_net"]) and v["avg_net"] > 0.002
        and v["fill_rate"] >= 0.30 and v["n_fills"] >= 30
    ]
    best = max(
        primary.items(),
        key=lambda kv: kv[1]["avg_net"] if np.isfinite(kv[1]["avg_net"]) else -9,
    )
    sensitivity = {k: v for k, v in rules.items() if k not in primary_names}
    gate = {
        "net_expectancy_gt": 0.002,
        "fill_rate_ge": 0.30,
        "min_fills": 30,
        "primary_rules": primary_names,
        "sensitivity_rules": list(sensitivity),
        "passed": bool(passed),
        "passed_rules": passed,
        "best_rule": best[0],
        "best_avg_net": best[1]["avg_net"],
        "best_fill_rate": best[1]["fill_rate"],
        "stage1_gate_passed": stage1_passed,
        "overall_go": bool(stage1_passed and passed),
        "note": "Stage 1 未通过，因此 Stage 2 仅作诊断，不进入前向纸盘/小资金",
    }
    return {
        "stage": "execution_backtest",
        "diagnostic_only": True,
        "test_universe": {
            "n": len(test),
            "n_days": int(test["date"].nunique()),
            "date_min": str(test["date"].min()),
            "date_max": str(test["date"].max()),
            "note": "首触 K=4 无法在触板后再等一根 K 成交，已剔除",
        },
        "fill_model": fill_eval,
        "fill_prob_quintiles": quint,
        "rules": rules,
        "gate": gate,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--scores", type=Path, default=SCORES)
    ap.add_argument("--seal-json", type=Path, default=SEAL_JSON)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    events, threshold, stage1_passed = load_events(args.events, args.scores, args.seal_json)
    result = run(events, threshold, stage1_passed)
    args.out.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[ok] {args.out}")
    g = result["gate"]
    print(f"[gate] Stage 2 {'PASS' if g['passed'] else 'FAIL'}；"
          f"可执行规则通过: {g['passed_rules'] or '无'}；"
          f"最佳 {g['best_rule']} net={g['best_avg_net']:+.3%} "
          f"fill_rate={g['best_fill_rate']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
