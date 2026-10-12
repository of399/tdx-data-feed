"""阶段 1 Step 2 — 候选因子有效性校验。

检查 compute_candidate_features 在真实日K 上：
  - 不抛异常、返回全部 9 个键
  - 无 inf / 无意外 nan（warmup 已填 0）
  - 取值范围合理（打印各因子 min/max/mean 供人工 sanity check）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.factors_candidates import (  # noqa: E402
    CANDIDATE_NAMES,
    compute_candidate_features,
)

DAILY = ROOT / "data" / "parquet" / "daily"


def test_candidates_finite_and_complete() -> None:
    files = sorted(DAILY.glob("*.parquet"))
    assert files, f"未找到日K: {DAILY}"
    files = files[:: max(1, len(files) // 60)]  # 抽 ~60 只
    cols = {k: [] for k in CANDIDATE_NAMES}
    n_ok = n_skip = 0
    for fp in files:
        df = pl.read_parquet(fp)
        if df.height < 60:
            n_skip += 1
            continue
        r = compute_candidate_features(df, fp.stem)
        if r is None:
            n_skip += 1
            continue
        for k in CANDIDATE_NAMES:
            v = float(r[k])
            assert np.isfinite(v), f"{fp.stem}.{k} 非有限: {v}"
            cols[k].append(v)
        n_ok += 1
    print(f"\n candidates: 通过 {n_ok} 只, 跳过(历史不足) {n_skip} 只")
    for k in CANDIDATE_NAMES:
        a = np.array(cols[k])
        print(f"  {k:16s} min={a.min(): .4f}  max={a.max(): .4f}  mean={a.mean(): .4f}")
    assert n_ok > 0


if __name__ == "__main__":
    try:
        test_candidates_finite_and_complete()
        print("RESULT: PASS")
    except AssertionError as e:
        print(f"RESULT: FAIL — {e}")
        sys.exit(1)
