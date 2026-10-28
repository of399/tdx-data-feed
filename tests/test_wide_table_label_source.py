"""宽表的标签必须来自当前 CSV，且覆盖当前有日线的全部信号。

这是对数据产物的守卫（不是对函数）：只在本机有 `data/` 时跑，CI 无数据环境由根 conftest 跳过。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
WIDE = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
CSV = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
FEATURES = ROOT / "data" / "double_up" / "stage1_selected_features.json"
DAILY = ROOT / "data" / "parquet" / "daily"

KEY = ["code", "signal_date"]
LABEL = "hypothetical_pnl"
# 日线文件名是 `<sh|sz|bj><6 位数字>.parquet`；同目录还躺着 `<...>.v1.bak.parquet`
# 这类备份，stem 不按格式过滤就会把并不存在的 code 算进 have。
_DAILY_STEM = re.compile(r"(?:sh|sz|bj)?\d{6}")

pytestmark = pytest.mark.skipif(
    not (WIDE.exists() and CSV.exists() and FEATURES.exists() and DAILY.exists()),
    reason="缺 stage1 宽表 / 标签 CSV / 特征清单 / 日线目录",
)


def _frames():
    """返回 (wide, csv)，两侧 code 都规范成带市场前缀的 6 位形式。

    CSV 必须用 `float_precision="round_trip"`：默认解析器会把部分浮点读偏 1 ULP，
    那样「宽表与 CSV 同源」就变成了「宽表与 CSV 的解析误差同源」。
    去重不在这里做——各测试把去重后的帧单独命名（`wide_keys` / `csv_keys`），
    免得「去重」和「比较」混在同一个变量里看不出来。
    """
    wide = pd.read_parquet(WIDE, columns=KEY + [LABEL])
    csv = pd.read_csv(CSV, usecols=KEY + [LABEL], float_precision="round_trip")
    for df in (wide, csv):
        df["code"] = df["code"].astype(str).str.zfill(6)
    return wide, csv


def test_labels_match_current_csv():
    wide, csv = _frames()
    # 宽表有重复键（重算前既有，本阶段不处置），按键去重后对齐，避免叉积放大行数。
    # 去重只能让「键内标签分叉」绕过下面的比较，那一条由
    # test_labels_are_consistent_within_key 单独守。
    wide_keys = wide.drop_duplicates(subset=KEY)
    csv_keys = csv.drop_duplicates(subset=KEY)
    m = wide_keys.merge(csv_keys, on=KEY, suffixes=("_w", "_c"))
    assert len(m) == len(wide_keys), (
        f"宽表有 {len(wide_keys) - len(m)} 个键在 CSV 里找不到"
    )
    # NaN 必须先单独断言：`(a - b).abs().max()` 会跳过 NaN，
    # 「新核对部分行算不出而写 NaN」是最可能的破坏形态，恰好全绿。
    # 合并后 CSV 侧本就不该有空值：空标签只出现在没有日线的 code 上，
    # 那些 code 已被下面的覆盖性测试排除在外。
    assert m[f"{LABEL}_w"].notna().all(), (
        f"宽表有 {int(m[f'{LABEL}_w'].isna().sum())} 个键标签为空：新内核在这些行没算出结果"
    )
    assert m[f"{LABEL}_c"].notna().all(), (
        f"CSV 有 {int(m[f'{LABEL}_c'].isna().sum())} 个键标签为空：对应 code 的日线可能被删了"
    )
    same = m[f"{LABEL}_w"] == m[f"{LABEL}_c"]
    assert same.all(), (
        f"{int((~same).sum())} 个键的标签与当前 CSV 不一致，宽表不是从它装配的；"
        f"样例: {m.loc[~same, KEY + [f'{LABEL}_w', f'{LABEL}_c']].head(3).to_dict('records')}"
    )


def test_labels_are_consistent_within_key():
    """同一 (code, signal_date) 的重复行标签必须一致。

    上面的同源比较按键去重，`keep="first"` 会让键内标签分叉完全绕过比较；
    宽表的重复键是重算前既有的数据质量问题，本阶段不删行，但标签分叉必须红。
    """
    wide, csv = _frames()
    for name, df in (("宽表", wide), ("CSV", csv)):
        counts = df.groupby(KEY, dropna=False)[LABEL].nunique(dropna=False)
        split = counts[counts > 1]
        assert split.empty, (
            f"{name}有 {len(split)} 个键的标签在组内不一致（最多 {int(split.max())} 种取值）；"
            f"去重比较会静默取第一行。样例: {split.head(3).to_dict()}"
        )


def test_wide_table_covers_the_whole_available_universe():
    """宽表键集合 == CSV 里 code 有日线文件的那部分，一个不多一个不少。

    预期集合从数据现算（不写死规模），所以 `--sample` 采样跑必然红。
    """
    wide, csv = _frames()
    have = {p.stem for p in DAILY.glob("*.parquet") if _DAILY_STEM.fullmatch(p.stem)}
    assert have, f"{DAILY} 里没有可识别的日线文件，先确认数据目录"
    expected = csv[csv["code"].isin(have)]
    got = set(zip(wide["code"], wide["signal_date"]))
    want = set(zip(expected["code"], expected["signal_date"]))
    extra, missing = got - want, want - got
    assert got == want, (
        f"宽表与「CSV 中有日线的信号」不一致：多 {len(extra)} 个键、少 {len(missing)} 个键"
        f"（got={len(got)}, want={len(want)}）；"
        f"样例 多={sorted(extra)[:3]} 少={sorted(missing)[:3]}"
    )


def test_wide_table_has_every_selected_feature_column():
    """宽表列集合覆盖特征契约，且没有整列为空的列。

    只比标签的守卫看不到「特征列被删 / 改名 / 整列算失败」，训练侧会静默少特征。
    """
    selected = json.loads(FEATURES.read_text(encoding="utf-8"))["selected_features"]
    wide = pd.read_parquet(WIDE)  # 全列：列完整性本身就是被测对象
    needed = set(selected) | set(KEY) | {LABEL}
    absent = needed - set(wide.columns)
    assert not absent, (
        f"宽表缺 {len(absent)} 列: {sorted(absent)}"
        f"（特征契约 {len(selected)} 列 + 键/标签）"
    )
    empty = wide.columns[wide.isna().all()].tolist()
    assert not empty, f"整列为空: {empty}（特征算失败，或被改名后补了空列）"
