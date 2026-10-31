"""阶段 1 Step 1 — 数值一致性测试。

验证 tdxfeed/factors_polars.py 的 Polars 实现与权威实现
tdxfeed/features_5d.py 在 18 维 / 26 维特征上逐位一致（容差 1e-9）。

运行：venv/bin/python -m pytest tests/test_stage1_factors_parity.py -q
或：  venv/bin/python tests/test_stage1_factors_parity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.factors_polars import (  # noqa: E402
    compute_window_features_extended_polars,
    compute_window_features_polars,
)
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features,
    compute_window_features_extended,
)

# 读 data/parquet/daily；路径用 Path 拼接，源码里没有 data/ 字样，分类抓不到。
NEEDS_DATA = True

DAILY = ROOT / "data" / "parquet" / "daily"


def _files(sample: int = 40) -> list[Path]:
    fs = sorted(DAILY.glob("*.parquet"))
    if sample and len(fs) > sample:
        # 均匀抽样的同时保证覆盖各市场前缀
        step = len(fs) // sample
        fs = fs[::step][:sample]
    return fs


def _compare(base_fn, poly_fn, code: str, df_pd: pd.DataFrame, df_pl: pl.DataFrame) -> dict:
    r_pd = base_fn(df_pd, code)
    r_pl = poly_fn(df_pl, code)
    if r_pd is None or r_pl is None:
        return {"skip": r_pd is None and r_pl is None, "keys_ok": None,
                "max_diff": None, "max_rel": None}
    keys_ok = set(r_pd.keys()) == set(r_pl.keys())
    max_diff = 0.0
    max_rel = 0.0
    if keys_ok:
        for k in r_pd:
            a, b = float(r_pd[k]), float(r_pl[k])
            d = abs(a - b)
            max_diff = max(max_diff, d)
            # 成交额类特征量级可达 1e9，float64 累加顺序差 ~1e-16 相对；
            # 绝对容差 1e-9 在 1e9 量级上等价于要求位级一致，不可行
            max_rel = max(max_rel, d / max(abs(a), abs(b), 1.0))
    return {"skip": False, "keys_ok": keys_ok, "max_diff": max_diff, "max_rel": max_rel}


def test_parity_18_and_26() -> None:
    files = _files()
    assert files, f"未找到日K parquet: {DAILY}"
    worst_18 = worst_26 = 0.0
    rel_18 = rel_26 = 0.0
    n_ok = n_skip = 0
    bad = []
    for fp in files:
        code = fp.stem
        df_pd = pd.read_parquet(fp)
        df_pl = pl.read_parquet(fp)
        if len(df_pd) < 5:
            n_skip += 1
            continue
        c18 = _compare(compute_window_features, compute_window_features_polars, code, df_pd, df_pl)
        c26 = _compare(
            compute_window_features_extended, compute_window_features_extended_polars,
            code, df_pd, df_pl,
        )
        if c18.get("skip") or c26.get("skip"):
            n_skip += 1
            continue
        if not (c18["keys_ok"] and c26["keys_ok"]):
            bad.append((code, "keys", c18, c26))
            continue
        worst_18 = max(worst_18, c18["max_diff"])
        worst_26 = max(worst_26, c26["max_diff"])
        rel_18 = max(rel_18, c18["max_rel"])
        rel_26 = max(rel_26, c26["max_rel"])
        n_ok += 1
    print(f"\n parity: 通过 {n_ok} 只, 跳过(数据不足) {n_skip} 只")
    print(f" 18维 最大差 = {worst_18:.3e}  最大相对差 = {rel_18:.3e}")
    print(f" 26维 最大差 = {worst_26:.3e}  最大相对差 = {rel_26:.3e}")
    if bad:
        print(f" 键不一致: {bad[:5]}")
    assert not bad, f"存在键不一致样本: {bad[:5]}"
    # 相对容差 1e-9：仍能拦住 float32 级误差（~1e-7 相对），放行 float64 累加顺序差
    assert rel_18 <= 1e-9, f"18维 超出相对容差: {rel_18}"
    assert rel_26 <= 1e-9, f"26维 超出相对容差: {rel_26}"


if __name__ == "__main__":
    try:
        test_parity_18_and_26()
        print("RESULT: PASS")
    except AssertionError as e:
        print(f"RESULT: FAIL — {e}")
        sys.exit(1)
