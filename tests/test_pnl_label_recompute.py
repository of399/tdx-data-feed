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

pytestmark = pytest.mark.skipif(
    not NEW.exists() or not DAILY.exists(),
    reason="缺 data/double_up 标签文件或日线目录（data/ 在 gitignore 里）",
)

# 快照只在 data/ 里、删了就没了：依赖它的三条单独 skip，不株连不需要快照的守卫。
OLD_MISSING = pytest.mark.skipif(
    not OLD.exists(),
    reason="缺 Task 1 旧标签快照（data/ 在 gitignore 里，不可再生）",
)


def _daily_codes() -> set[str]:
    return {p.stem for p in DAILY.glob("*.parquet")}


@OLD_MISSING
def test_same_keys_and_row_count():
    new = pd.read_csv(NEW, usecols=["code", "signal_date"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date"])
    assert len(new) == len(old)
    assert new["signal_date"].str.len().eq(10).all()
    # 只读表头核对全表列序：usecols 之后列序恒真，不能拿来当契约断言。
    assert list(pd.read_csv(NEW, nrows=0).columns) == list(pd.read_csv(OLD, nrows=0).columns)


def test_labels_exist_exactly_where_daily_bars_exist():
    """缺日线的 code（bj 1539 行 + ETF 1544 行 = 3083 行）重算不出标签，必须为 null；
    有日线的行必须全部算出标签。"""
    df = pd.read_csv(NEW, usecols=["code", "hypothetical_pnl"])
    have = df["code"].isin(_daily_codes())
    assert (~have).sum() > 0, "daily 目录像被整体删了：先确认数据源，别当测试坏了"
    assert df.loc[have, "hypothetical_pnl"].notna().all(), "有日线的行出现了新的 None"
    assert df.loc[~have, "hypothetical_pnl"].isna().all(), "缺日线的行仍有旧口径标签"


@OLD_MISSING
def test_labels_actually_changed():
    """口径变了，有日线的行标签不可能逐位相同——相同说明根本没重算。

    两侧都必须先按 `(code, signal_date)` 去重再 merge：标签 CSV 有 6 个键各 32 行、宽表侧有
    3 个键各 32 行，直接 merge 会得到 32×32 叉积（实测 12957 行），把 3 个样本按 1024× 加权。
    """
    new = pd.read_csv(NEW, usecols=["code", "signal_date", "hypothetical_pnl"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date", "hypothetical_pnl"])
    new = new.drop_duplicates(["code", "signal_date"])
    old = old.drop_duplicates(["code", "signal_date"])
    m = new.merge(old, on=["code", "signal_date"], suffixes=("_new", "_old"))
    m = m[m["hypothetical_pnl_new"].notna() & m["hypothetical_pnl_old"].notna()]
    # 键级 1:1：去重后行数必须等于宽表非空键数；叉积复现时这条会与下面的 >9000 一起红。
    assert not m.duplicated(["code", "signal_date"]).any(), "合并出现重复键：检查两侧是否忘了去重"
    assert len(m) == int(new["hypothetical_pnl"].notna().sum()), "合并行数 ≠ 新标签非空键数（叉积？）"
    assert len(m) > 9000
    diff = (m["hypothetical_pnl_new"] - m["hypothetical_pnl_old"]).abs()
    assert (diff > 1e-9).mean() > 0.95, "标签与旧口径几乎一致，检查是否复用了旧列"


@OLD_MISSING
def test_non_label_columns_are_untouched():
    """只换标签列：其余 29 列必须与快照逐位相同。

    round_trip 读取是必须的——默认解析器会把 ~100 个浮点读偏 1 ULP，重写后
    特征就悄悄变了（这条守卫就是为抓那次漂移而加）。
    """
    new = pd.read_csv(NEW, float_precision="round_trip")
    old = pd.read_csv(OLD, float_precision="round_trip")
    others = [c for c in old.columns if c != "hypothetical_pnl"]
    pd.testing.assert_frame_equal(new[others], old[others], check_exact=True)


def test_refuses_to_write_when_labels_vanish(monkeypatch, tmp_path):
    """安全闸门：attach_pnl 若把标签算没了，脚本必须拒写，不能 exit 0 覆盖唯一产物。"""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import recompute_pnl_labels as R

    dst = tmp_path / NEW.name
    dst.write_bytes(NEW.read_bytes())
    df = pd.read_csv(dst, float_precision="round_trip")
    emptied = df.drop(columns=["hypothetical_pnl"]).assign(hypothetical_pnl=pd.NA)
    monkeypatch.setattr(R, "OUT_DATA", dst)
    monkeypatch.setattr(R, "attach_pnl", lambda features, max_workers=8: emptied)
    assert R.main() == 1
    assert dst.read_bytes() == NEW.read_bytes(), "闸门没拦住：文件被覆盖了"
