#!/usr/bin/env python3
"""scripts/eval_signal_honest.py — 诚实时序前向重训评测(去泄漏)

对关键候选在 train(<CUTOFF) 上重训,在 test(>=CUTOFF) 上评测,
给出无泄漏的可部署估计,并确认冠军(vpnl_huber_mkt)在去泄漏后仍胜出。

候选:
  v9            LGBMClassifier  26 基础特征,目标 is_positive
  vpnl_huber    LGBMRegressor(huber) 26 基础,目标 hypothetical_pnl
  vpnl_huber_mkt LGBMRegressor(huber) 26+5板块+4市场,目标 hypothetical_pnl

用法:
  venv/bin/python scripts/eval_signal_honest.py --cutoff 2025-01-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
DU = ROOT / "data" / "double_up"


def load_df(csv_name: str) -> pd.DataFrame:
    df = pd.read_csv(DU / csv_name)
    if "hypothetical_pnl" not in df.columns:
        pnl = pd.read_csv(DU / "d_signal_5d_features_vpnl.csv",
                          usecols=["code", "signal_date", "hypothetical_pnl"])
        df = df.merge(pnl, on=["code", "signal_date"], how="left")
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    return df


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    try:
        from scipy.stats import spearmanr
        r = spearmanr(x, y).correlation
        return float(r) if np.isfinite(r) else float("nan")
    except Exception:
        return float(pd.Series(x).corr(pd.Series(y), method="spearman"))


def run(name: str, csv_name: str, kind: str, cutoff: pd.Timestamp) -> dict:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    df = load_df(csv_name).dropna(subset=["signal_date"])
    meta = {"code", "signal_date", "is_positive", "hypothetical_pnl", "industry_code"}
    feat_cols = [c for c in df.columns if c not in meta]
    target = "is_positive" if kind == "cls" else "hypothetical_pnl"

    tr = df[df["signal_date"] < cutoff]
    te = df[df["signal_date"] >= cutoff]
    if len(te) < 100:
        raise RuntimeError(f"test 不足: {len(te)}")

    Xtr, ytr = tr[feat_cols].fillna(0.0), tr[target].astype(float)
    Xte, yte_pnl = te[feat_cols].fillna(0.0), te["hypothetical_pnl"].astype(float)
    yte_pos = te["is_positive"].astype(int)

    if kind == "cls":
        model = lgb.LGBMClassifier(n_estimators=500, learning_rate=0.05, max_depth=6,
                                   num_leaves=31, min_child_samples=20, subsample=0.8,
                                   colsample_bytree=0.8, random_state=42, n_jobs=-1, verbose=-1)
        model.fit(Xtr, ytr)
        pred = model.predict_proba(Xte)[:, 1]
        auc = float(roc_auc_score(yte_pos, pred))
        rank_auc = float("nan")
    else:
        model = lgb.LGBMRegressor(objective="huber", alpha=0.5, n_estimators=500,
                                  learning_rate=0.05, max_depth=6, num_leaves=31,
                                  min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
                                  random_state=42, n_jobs=-1, verbose=-1)
        model.fit(Xtr, ytr)
        pred = model.predict(Xte)
        auc = float("nan")
        rank_auc = float(roc_auc_score(yte_pos, pred))

    order = np.argsort(-pred)
    pnl_sorted = yte_pnl.values[order]
    dec = max(1, len(pnl_sorted) // 10)
    rec = {
        "name": name, "kind": kind, "n_train": len(tr), "n_test": len(te),
        "n_feat": len(feat_cols),
        "top50_pnl": float(np.mean(pnl_sorted[:50])),
        "top100_pnl": float(np.mean(pnl_sorted[:100])),
        "top200_pnl": float(np.mean(pnl_sorted[:200])),
        "long_short": float(np.mean(pnl_sorted[:dec]) - np.mean(pnl_sorted[-dec:])),
        "top200_hit": float(np.mean(yte_pos.values[order[:200]])),
        "spearman_pnl": spearman(pred, yte_pnl.values),
        "auc_ispos": auc, "rank_auc_ispos": rank_auc,
    }
    print(f"  ✓ {name:14s} n_tr={len(tr)} n_te={len(te)} "
          f"top50={rec['top50_pnl']:+.4f} ls={rec['long_short']:+.4f} "
          f"spr={rec['spearman_pnl']:+.3f}", file=sys.stderr)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2025-01-01")
    args = ap.parse_args()
    cutoff = pd.Timestamp(args.cutoff)
    print(f"=== 诚实时序重训评测 (train<{cutoff.date()}, test>={cutoff.date()}) ===\n")

    specs = [
        ("v9",             "d_signal_5d_features_v9.csv",      "cls"),
        ("vpnl_huber",     "d_signal_5d_features_vpnl.csv",    "reg"),
        ("vpnl_huber_mkt", "d_signal_5d_features_v9_market.csv", "reg"),
    ]
    rows = []
    for name, csv, kind in specs:
        try:
            rows.append(run(name, csv, kind, cutoff))
        except Exception as e:
            print(f"  [err] {name}: {e}", file=sys.stderr)

    df = pd.DataFrame(rows).sort_values("long_short", ascending=False).reset_index(drop=True)
    pd.set_option("display.width", 200)
    print("\n=== 去泄漏对比(按 long_short) ===")
    print(df.to_string(index=False))
    print(f"\n>>> 诚实估计最优: {df.iloc[0]['name']}")


if __name__ == "__main__":
    main()
