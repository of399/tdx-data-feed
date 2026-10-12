"""阶段 1 Step 3 — 宽表装配结构校验。

注：不断言与 d_signal_5d_features_vpnl.csv 的数值 parity——该 CSV 是旧 parquet 快照生成，
当前 parquet 历史价/额已漂移（见 docs/stage1_feature_migration_plan.md §3）。结构正确性才是本测试目标。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.factors_candidates import CANDIDATE_NAMES  # noqa: E402
from tdxfeed.stage1_wide_table import (  # noqa: E402
    FEAT26_ORDER,
    build_wide_table,
)

EXPECTED_BASE = ["code", "signal_date", "is_positive", "hypothetical_pnl"]


def test_wide_table_structure() -> None:
    out = ROOT / "data" / "double_up" / "_test_stage1_wide_sample.parquet"
    res = build_wide_table(sample_codes=50, out_parquet=out, verify_parity=False)
    assert res["n_ok"] > 0

    df = pl.read_parquet(out)
    cols = df.columns
    for c in EXPECTED_BASE + FEAT26_ORDER + CANDIDATE_NAMES:
        assert c in cols, f"缺列 {c}"

    # 26 维基础特征：允许极少数历史数据异常的非有限单元，但占比应极低
    n_cells = df.height * len(FEAT26_ORDER)
    n_bad = 0
    for c in FEAT26_ORDER:
        n_bad += int(df[c].is_nan().sum()) + int(df[c].is_infinite().sum())
    assert n_bad / n_cells < 0.01, f"非有限占比过高: {n_bad}/{n_cells}"

    # 候选因子：应大量有限（短历史少数填 0 也算有限）
    cand_finite = sum(int(df[c].is_finite().sum()) for c in CANDIDATE_NAMES)
    assert cand_finite / (df.height * len(CANDIDATE_NAMES)) > 0.9

    # 标签存在且类型合理
    assert df["is_positive"].n_unique() <= 2
    assert df["hypothetical_pnl"].is_finite().sum() > df.height * 0.9
    out.unlink(missing_ok=True)
    print(f"\n wide_table 结构 OK: rows={df.height} 非有限基础单元={n_bad}")


if __name__ == "__main__":
    try:
        test_wide_table_structure()
        print("RESULT: PASS")
    except AssertionError as e:
        print(f"RESULT: FAIL — {e}")
        sys.exit(1)
