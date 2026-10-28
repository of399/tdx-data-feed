"""重算后的标签必须满足内核口径；这是对数据文件的守卫，不是对函数的。

只在 data/ 存在时跑：CI 无数据环境由根 conftest 跳过。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
NEW = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
OLD = ROOT / "data" / "double_up" / "_pre_settle_kernel_20261028" / "d_signal_5d_features_vpnl.csv"
DAILY = ROOT / "data" / "parquet" / "daily"

pytestmark = pytest.mark.skipif(not NEW.exists(), reason="缺 data/double_up 标签文件")


def _daily_codes() -> set[str]:
    return {p.stem for p in DAILY.glob("*.parquet")}


def test_same_keys_and_row_count():
    new = pd.read_csv(NEW, usecols=["code", "signal_date"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date"])
    assert len(new) == len(old)
    assert new["signal_date"].str.len().eq(10).all()


def test_labels_exist_exactly_where_daily_bars_exist():
    """缺日线的 code（bj 1539 行 + ETF 1544 行 = 3083 行）重算不出标签，必须为 null；
    有日线的行必须全部算出标签。"""
    df = pd.read_csv(NEW, usecols=["code", "hypothetical_pnl"])
    have = df["code"].isin(_daily_codes())
    assert (~have).sum() > 0, "daily 目录像被整体删了：先确认数据源，别当测试坏了"
    assert df.loc[have, "hypothetical_pnl"].notna().all(), "有日线的行出现了新的 None"
    assert df.loc[~have, "hypothetical_pnl"].isna().all(), "缺日线的行仍有旧口径标签"


def test_labels_actually_changed():
    """口径变了，有日线的行标签不可能逐位相同——相同说明根本没重算。"""
    new = pd.read_csv(NEW, usecols=["code", "signal_date", "hypothetical_pnl"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date", "hypothetical_pnl"])
    m = new.merge(old, on=["code", "signal_date"], suffixes=("_new", "_old"))
    m = m[m["hypothetical_pnl_new"].notna() & m["hypothetical_pnl_old"].notna()]
    assert len(m) > 9000
    diff = (m["hypothetical_pnl_new"] - m["hypothetical_pnl_old"]).abs()
    assert (diff > 1e-9).mean() > 0.95, "标签与旧口径几乎一致，检查是否复用了旧列"


def test_non_label_columns_are_untouched():
    """只换标签列：其余 29 列必须与快照逐位相同。

    round_trip 读取是必须的——默认解析器会把 ~100 个浮点读偏 1 ULP，重写后
    特征就悄悄变了（这条守卫就是为抓那次漂移而加）。
    """
    new = pd.read_csv(NEW, float_precision="round_trip")
    old = pd.read_csv(OLD, float_precision="round_trip")
    others = [c for c in old.columns if c != "hypothetical_pnl"]
    pd.testing.assert_frame_equal(new[others], old[others], check_exact=True)
