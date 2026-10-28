"""宽表的标签必须来自当前 CSV，不能是上一轮遗留的旧列。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
WIDE = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
CSV = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
DAILY = ROOT / "data" / "parquet" / "daily"

pytestmark = pytest.mark.skipif(not WIDE.exists(), reason="缺 stage1 宽表")


def _frames():
    wide = pd.read_parquet(WIDE, columns=["code", "signal_date", "hypothetical_pnl"])
    csv = pd.read_csv(CSV, usecols=["code", "signal_date", "hypothetical_pnl"])
    for df in (wide, csv):
        df["code"] = df["code"].astype(str).str.zfill(6)
    return wide, csv


def test_labels_match_current_csv():
    # CSV 有 6 个键各 32 行完全重复（重算前既有），先去重键再比，避免 32×32 叉积
    wide, csv = _frames()
    wide = wide.drop_duplicates(subset=["code", "signal_date"])
    csv = csv.drop_duplicates(subset=["code", "signal_date"])
    m = wide.merge(csv, on=["code", "signal_date"], suffixes=("_w", "_c"))
    assert len(m) == len(wide), "宽表里出现了 CSV 没有的键"
    assert (m["hypothetical_pnl_w"] - m["hypothetical_pnl_c"]).abs().max() < 1e-9


def test_wide_table_covers_the_whole_available_universe():
    """宽表 = CSV 里 code 有日线文件的那部分（缺日线的 3083 行按 n_missing_file 丢弃），
    且不是 --sample 采样产物。预期集合从数据现算，不写死规模。"""
    wide, csv = _frames()
    have = {p.stem for p in DAILY.glob("*.parquet")}
    expected = csv[csv["code"].isin(have)]
    got = set(zip(wide["code"], wide["signal_date"]))
    want = set(zip(expected["code"], expected["signal_date"]))
    assert got.issubset(want), f"宽表多出 {len(got - want)} 个键"
    assert len(got) >= len(want) - 100, f"宽表缺 {len(want - got)} 个键，像被采样了"
    assert wide["code"].nunique() > 3000, "宽表 code 数异常，先查是不是 --sample 没设 0"
