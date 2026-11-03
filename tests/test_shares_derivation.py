"""股本反推层（tdxfeed/shares.py）：从 finance.parquet 长表反推股本的口径与只读性。

三层判据：
  1. 纯函数：tmp_path 造最小长表，覆盖可成对反推 / 分母为 0 / 缺对应 metric / 量级带
  2. 只读契约：列裁剪 + metric 谓词（不得整表 load 后过滤）、目标文件字节不变
  3. 真数据一致性：两条通道在同一 report_date 上的相对差（主判据）、相邻报告期跳变

NEEDS_DATA = False：1/2 层走 tmp_path；第 3 层按 finance.parquet 是否存在自行 skip。
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter

import numpy as np
import pandas as pd
import pytest

from tdxfeed import shares
from tdxfeed.paths import finance_parquet

NEEDS_DATA = False

# 口径配对的唯一权威形状：分子总量 ÷ 分母每股量
REV_NUM, REV_DEN = shares.CHANNELS[shares.BASIS_REVENUE]
NP_NUM, NP_DEN = shares.CHANNELS[shares.BASIS_ATT_NP]

ALL_METRICS = [REV_NUM, REV_DEN, NP_NUM, NP_DEN]

# 成对样例：营业总收入 1e9 元 / 每股营业总收入 1.0 元 ⇒ 1e9 股
# 归母净利润 2e8 元 / 摊薄每股收益_最新股数 0.2 元 ⇒ 1e9 股
ROWS = [
    ("sh600000", "20240331", REV_NUM, 1_000_000_000.0),
    ("sh600000", "20240331", REV_DEN, 1.0),
    ("sh600000", "20240331", NP_NUM, 200_000_000.0),
    ("sh600000", "20240331", NP_DEN, 0.2),
    ("sh600000", "20240630", REV_NUM, 2_400_000_000.0),
    ("sh600000", "20240630", REV_DEN, 1.2),
    ("sh600000", "20240630", NP_NUM, 420_000_000.0),
    ("sh600000", "20240630", NP_DEN, 0.21),
]


def write_finance(root, rows=ROWS):
    """按真实 schema（symbol/report_date/option/metric/value，均 string+double）造长表。"""
    recs = [{"symbol": s, "report_date": d, "option": o, "metric": m, "value": v}
            for s, d, (o, m), v in rows]
    path = root / "finance.parquet"
    pd.DataFrame.from_records(recs).to_parquet(path, index=False)
    return path


# ---------------------------------------------------------------------------
# 0. 口径配对本身（防有人改回「每股营业收入」这种错配）
# ---------------------------------------------------------------------------
def test_channel_pairings_are_the_documented_ones():
    assert REV_NUM == ("常用指标", "营业总收入")
    assert REV_DEN == ("每股指标", "每股营业总收入")
    assert NP_NUM == ("常用指标", "归母净利润")
    assert NP_DEN == ("每股指标", "摊薄每股收益_最新股数")


def test_wrong_pairing_is_not_used():
    """每股营业收入 的分子是「营业收入」（表里没有），与营业总收入不等 ⇒ 不得配对。"""
    used = {m for pair in shares.CHANNELS.values() for m in pair}
    assert ("每股指标", "每股营业收入") not in used


def test_metric_budget_is_two_to_four_pairs():
    assert 2 <= len(shares.CHANNELS) <= 4


def test_docstring_declares_lag_as_approximation():
    doc = shares.shares_frame.__doc__ or ""
    assert "近似" in doc and "前视" in doc, "披露滞后必须是标注过的近似，不能当真实披露日"


def test_docstring_declares_the_used_pairs():
    doc = (shares.__doc__ or "") + (shares.shares_frame.__doc__ or "")
    for metric in ("营业总收入", "每股营业总收入", "归母净利润", "摊薄每股收益_最新股数"):
        assert metric in doc, metric


# ---------------------------------------------------------------------------
# 1. 披露滞后的月份映射（保守近似）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("report_date,expect", [
    (20240331, 20240430),   # 一季报 +1 个月
    ("20240331", 20240430),
    (20240630, 20240831),   # 半年报 +2 个月
    (20240930, 20241031),   # 三季报 +1 个月
    (20241231, 20250430),   # 年报 +4 个月（跨年）
    (20201231, 20210430),
])
def test_available_after_month_mapping(report_date, expect):
    assert shares.available_after(report_date) == expect


def test_available_after_rolls_to_month_end():
    """非标准报告期（月中）也落到月末，不给「差一天就早于法定披露」的日子。"""
    assert shares.available_after(20241215) == 20250430


# ---------------------------------------------------------------------------
# 2. 反推本体
# ---------------------------------------------------------------------------
def test_shares_frame_shape_and_columns(tmp_path):
    out = shares.shares_frame(path=write_finance(tmp_path))
    assert list(out.columns) == ["symbol", "report_date", "shares", "basis", "available_after"]
    assert set(out["basis"]) == {shares.BASIS_REVENUE, shares.BASIS_ATT_NP}
    assert out["report_date"].dtype.kind == "i"
    assert out["available_after"].dtype.kind == "i"


def test_both_channels_derive_the_same_share_count(tmp_path):
    out = shares.shares_frame(path=write_finance(tmp_path))
    got = out.set_index(["report_date", "basis"])["shares"]
    assert got.loc[(20240331, shares.BASIS_REVENUE)] == pytest.approx(1e9)
    assert got.loc[(20240331, shares.BASIS_ATT_NP)] == pytest.approx(1e9)
    assert got.loc[(20240630, shares.BASIS_REVENUE)] == pytest.approx(2e9)
    assert got.loc[(20240630, shares.BASIS_ATT_NP)] == pytest.approx(2e9)


def test_magnitude_band_keeps_1e9_rows(tmp_path):
    """1e9 量级营收配 1e0 量级每股量 ⇒ 股数落在 1e9 量级（不被清洗误杀）。"""
    out = shares.shares_frame(path=write_finance(tmp_path))
    assert out["shares"].between(1e8, 1e10).all()


def test_available_after_is_on_every_row(tmp_path):
    out = shares.shares_frame(path=write_finance(tmp_path))
    exp = out["report_date"].map(shares.available_after)
    assert (out["available_after"] == exp).all()


def test_symbols_filter(tmp_path):
    path = write_finance(tmp_path, ROWS + [
        ("sz000001", "20240331", REV_NUM, 3_000_000_000.0),
        ("sz000001", "20240331", REV_DEN, 3.0),
    ])
    out = shares.shares_frame(symbols=["sh600000"], path=path)
    assert set(out["symbol"]) == {"sh600000"}
    out2 = shares.shares_frame(symbols=("sz000001",), path=path)
    assert set(out2["symbol"]) == {"sz000001"}


def test_unprefixed_codes_are_accepted(tmp_path):
    """symbols 允许裸 6 位：名单/日线两侧都有这种形态。"""
    path = write_finance(tmp_path)
    out = shares.shares_frame(symbols=["600000"], path=path)
    assert set(out["symbol"]) == {"sh600000"}


# ---------------------------------------------------------------------------
# 3. 清洗：分母 0 / 缺失 / None / 负 / NaN / 量级越带
# ---------------------------------------------------------------------------
def test_zero_denominator_dropped_and_counted(tmp_path):
    rows = [("sh600000", "20240331", REV_NUM, 1e9), ("sh600000", "20240331", REV_DEN, 0.0),
            ("sh600000", "20240331", NP_NUM, 2e8), ("sh600000", "20240331", NP_DEN, 0.2)]
    before = Counter(shares._CLEAN_COUNT)
    out = shares.shares_frame(path=write_finance(tmp_path, rows))
    # 营收通道分母为 0 ⇒ 该通道整行不出；每股利润通道仍成立
    assert set(out["basis"]) == {shares.BASIS_ATT_NP}
    assert shares._CLEAN_COUNT["non_positive_denominator"] > before["non_positive_denominator"]


def test_missing_metric_drops_only_that_channel(tmp_path):
    rows = [r for r in ROWS if r[2] != REV_DEN]
    out = shares.shares_frame(path=write_finance(tmp_path, rows))
    assert set(out["basis"]) == {shares.BASIS_ATT_NP}


def test_none_and_nan_values_are_dropped(tmp_path):
    rows = [("sh600000", "20240331", REV_NUM, None), ("sh600000", "20240331", REV_DEN, 1.0),
            ("sh600000", "20240331", NP_NUM, float("nan")), ("sh600000", "20240331", NP_DEN, 0.2)]
    out = shares.shares_frame(path=write_finance(tmp_path, rows))
    assert out.empty
    assert shares._CLEAN_COUNT["non_finite_value"] >= 2


def test_negative_numerator_is_not_absoluted(tmp_path):
    """亏损公司：净利润为负 ⇒ 反推的「股本」是负数，不得取绝对值蒙混过关。"""
    rows = [("sh600000", "20240331", REV_NUM, 1e9), ("sh600000", "20240331", REV_DEN, 1.0),
            ("sh600000", "20240331", NP_NUM, -2e8), ("sh600000", "20240331", NP_DEN, -0.2)]
    out = shares.shares_frame(path=write_finance(tmp_path, rows))
    # 双负相除为正但语义上是「亏损额/每股亏损」，本层不认它：营收通道留、利润通道丢
    assert set(out["basis"]) == {shares.BASIS_REVENUE}


def test_out_of_band_shares_are_rejected(tmp_path):
    """分母极小（1e-6 级 EPS）会造出 1e12 级假股本：必须落在量级带外被丢弃。"""
    rows = [("sh600000", "20240331", REV_NUM, 1e9), ("sh600000", "20240331", REV_DEN, 1e-6),
            ("sh600000", "20240331", NP_NUM, 2e8), ("sh600000", "20240331", NP_DEN, 0.2)]
    out = shares.shares_frame(path=write_finance(tmp_path, rows))
    assert set(out["basis"]) == {shares.BASIS_ATT_NP}
    assert shares._CLEAN_COUNT["out_of_band"] >= 1


def test_clean_counts_are_exposed(tmp_path):
    shares.shares_frame(path=write_finance(tmp_path))
    assert set(shares.clean_counts()) >= {"non_finite_value", "non_positive_denominator",
                                          "out_of_band", "zero_numerator"}
    assert isinstance(shares.error_counts(), Counter)


# ---------------------------------------------------------------------------
# 4. 只读契约
# ---------------------------------------------------------------------------
class _SpyParquet:
    def __init__(self, real):
        self.real = real
        self.calls = []

    def read_table(self, path, columns=None, filters=None, **kw):
        self.calls.append({"columns": columns, "filters": filters})
        return self.real.read_table(path, columns=columns, filters=filters, **kw)


def test_read_prunes_columns_and_pushes_metric_filter(monkeypatch, tmp_path):
    path = write_finance(tmp_path)
    spy = _SpyParquet(shares.pq)
    monkeypatch.setattr(shares, "pq", spy)
    shares.shares_frame(path=path)

    assert len(spy.calls) == 1
    cols = spy.calls[0]["columns"]
    assert cols is not None and set(cols) == {"symbol", "report_date", "option", "metric", "value"}
    # 下推的是「metric 的取值集合」；实现传 sorted list，比较按集合语义做，
    # 不许把断言写成 isinstance(v, set) 这种与实现表示法绑死的形状检查
    pushed = {frozenset(v) for _, _, v in spy.calls[0]["filters"]
              if isinstance(v, (list, tuple, set, frozenset))}
    assert frozenset(m for _, m in ALL_METRICS) in pushed, \
        "必须把 metric 过滤下推到读取，而不是整表 load 后过滤"


def test_symbol_filter_is_pushed_down(monkeypatch, tmp_path):
    path = write_finance(tmp_path)
    spy = _SpyParquet(shares.pq)
    monkeypatch.setattr(shares, "pq", spy)
    shares.shares_frame(symbols=["sh600000"], path=path)
    got = {(f[0], f[1]) for f in spy.calls[0]["filters"]}
    assert ("symbol", "in") in got


def test_shares_frame_does_not_touch_the_file(tmp_path):
    path = write_finance(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = path.stat().st_mtime_ns
    shares.shares_frame(path=path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.stat().st_mtime_ns == mtime


def test_no_write_api_in_the_module():
    """只读层：不得出现 to_parquet / to_csv / open(...,'w') / os.replace 之类的写盘调用。"""
    import ast
    from pathlib import Path as _P

    tree = ast.parse(_P(str(shares.__file__)).read_text(encoding="utf-8"))
    banned = {"to_parquet", "to_csv", "write_table", "write_text", "mkdir", "os.replace"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name in banned:
                seen.add(name)
            if name == "open" and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) and "w" in a.value
                    for a in [*node.args, *[k.value for k in node.keywords]]):
                seen.add("open-w")
    assert not seen, seen


def test_missing_file_raises_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        shares.shares_frame(path=tmp_path / "nope.parquet")


# ---------------------------------------------------------------------------
# 5. 通道一致性出口（真数据与 tmp 都能用）
# ---------------------------------------------------------------------------
def test_channel_agreement_on_synthetic(tmp_path):
    """rel_diff 的口径是文档里写的 |shares_revenue/shares_att_np − 1|，不是对称差。

    两个方向都钉住：营收通道偏小时得 0.5、偏大时得 1.0 —— 这条本身是不对称的，
    把它写死在断言里，改公式的人会立刻看到自己动了什么（上一版把期望写成 1.0，
    是拿反向算的，与被测实现的定义不符）。
    """
    rows = [
        # sh600000：营收 1e9（1e9/1.0），利润 2e9（2e8/0.1）⇒ |1e9/2e9 - 1| = 0.5
        ("sh600000", "20240331", REV_NUM, 1_000_000_000.0),
        ("sh600000", "20240331", REV_DEN, 1.0),
        ("sh600000", "20240331", NP_NUM, 200_000_000.0),
        ("sh600000", "20240331", NP_DEN, 0.1),
        # sz000001：营收 2e9，利润 1e9 ⇒ |2e9/1e9 - 1| = 1.0
        ("sz000001", "20240331", REV_NUM, 2_000_000_000.0),
        ("sz000001", "20240331", REV_DEN, 1.0),
        ("sz000001", "20240331", NP_NUM, 200_000_000.0),
        ("sz000001", "20240331", NP_DEN, 0.2),
    ]
    ag = shares.channel_agreement(shares.shares_frame(path=write_finance(tmp_path, rows)))
    got = dict(zip(ag["symbol"], ag["rel_diff"]))
    assert math.isclose(got["sh600000"], 0.5, rel_tol=1e-9), got
    assert math.isclose(got["sz000001"], 1.0, rel_tol=1e-9), got


# ---------------------------------------------------------------------------
# 6. 真数据只读校验（主判据）
# ---------------------------------------------------------------------------
REAL = finance_parquet()


def _require_real_data():
    if not REAL.exists():
        pytest.skip(f"本机无 {REAL}")
    return REAL


@pytest.fixture(scope="module")
def real_frame():
    path = _require_real_data()
    return shares.shares_frame(path=path)


def _sample_symbols(frame, n=30, seed=7):
    rng = np.random.default_rng(seed)
    syms = sorted(frame["symbol"].unique().tolist())
    return sorted(rng.choice(syms, min(n, len(syms)), replace=False).tolist())


def test_real_two_channels_agree_on_latest_report(real_frame):
    """30 只抽样，同一 report_date 上两条通道的相对差中位 < 0.5%。

    取每只标的**最近一期**报告期：此时「期末股本」与「最新股本」两个口径重合，
    是配对正确性的判据（历史期上二者本就不同，见 test_real_att_np_channel_is_flat）。
    """
    pick = _sample_symbols(real_frame)
    ag = shares.channel_agreement(real_frame, symbols=pick)
    last = ag.groupby("symbol")["report_date"].max()
    ag = ag.merge(last.rename("mx"), on="symbol")
    ag = ag[ag["report_date"] == ag["mx"]].drop(columns=["mx"])
    assert len(ag) >= 25, f"可对照的样本太少：{len(ag)}"
    med = float(ag["rel_diff"].median())
    p90 = float(ag["rel_diff"].quantile(0.9))
    assert med < 0.005, f"中位相对差 {med:.5f} ≥ 0.5%（p90={p90:.4f}）：配对可能选错"


def test_real_channels_agree_on_recent_report_dates(real_frame):
    """近 3 年报期（两口径尚未分道扬镳）的中位相对差同样必须 < 0.5%。"""
    pick = _sample_symbols(real_frame)
    ag = shares.channel_agreement(real_frame, symbols=pick)
    ag = ag[ag["report_date"] >= 20240101]
    assert len(ag) >= 200
    assert float(ag["rel_diff"].median()) < 0.005


def test_real_att_np_channel_is_within_symbol_flat(real_frame):
    """摊薄EPS_最新股数 通道按定义是「最新股本」：同一标的近 8 期应几乎恒定。

    这条把「历史期两条通道为何分叉」钉成有解释的分叉，而不是配对选错。
    """
    pick = _sample_symbols(real_frame)
    # 括号必须包住两个布尔 Series：写成 frame[mask & series] 之前是
    # DataFrame & Series，pandas 会按列广播出巨型中间物——本机实测单这一条用例
    # 吃穿 3 GiB 配额被 cgroup OOM 杀掉（2026-11-03 那次整机 OOM 的同一个形状）。
    g = real_frame[(real_frame["basis"] == shares.BASIS_ATT_NP)
                   & real_frame["symbol"].isin(pick)]
    spreads = []
    for sym, sub in g.groupby("symbol"):
        s = sub.sort_values("report_date")["shares"].tail(8)
        if len(s) >= 3:
            spreads.append(float(s.max() / s.min() - 1.0))
    assert len(spreads) >= 25
    assert float(np.median(spreads)) < 0.005, f"最新股本口径却在漂移：中位跨度 {np.median(spreads):.4f}"


def test_real_revenue_channel_moves_over_time(real_frame):
    """营收通道是「期末股本」：同一标的历史上必须出现过台阶（否则口径没随报告期走）。"""
    pick = _sample_symbols(real_frame)
    g = real_frame[(real_frame["basis"] == shares.BASIS_REVENUE)
                   & real_frame["symbol"].isin(pick)]
    moved = sum(1 for _, sub in g.groupby("symbol")
                if sub.sort_values("report_date")["shares"].nunique() > 1)
    assert moved >= 5, moved


def test_real_no_unexplained_10x_jumps(real_frame):
    """相邻报告期的 ≥10× 跳变必须是永久台阶（增发/回购/上市），不得是回弹的孤立尖峰。

    跳变本身允许存在，但要在报告里列清单——这里把清单交出来并断言「无回弹」。
    """
    jumps = shares.share_jumps(real_frame)
    unexplained = jumps[jumps["reverts"]]
    assert unexplained.empty, unexplained.head(10).to_string()
    assert len(jumps) <= 0.02 * len(real_frame), f"台阶过多：{len(jumps)} 行 / {len(real_frame)}"


def test_real_latest_shares_magnitude_is_sane(real_frame):
    """数量级校验（外部知识，非本仓库取证）：三只大盘股的最新股本量级。

    工商银行/平安银行/浦发银行都是万亿级市值的大盘股，总股本应落在 1e9~1e12 之间；
    这里只用到「大盘股股本 ≫ 1e8 且 ≪ 1e12」这一常识，不用具体官方数字。
    """
    last = real_frame[real_frame["basis"] == shares.BASIS_REVENUE].sort_values("report_date")
    for sym in ["sh601398", "sz000001", "sh600000"]:
        if sym not in set(last["symbol"]):
            pytest.skip(f"finance.parquet 未覆盖 {sym}")
        v = float(last[last["symbol"] == sym]["shares"].iloc[-1])
        assert 1e9 < v < 1e12, f"{sym} 反推股本 {v:.3g} 不在大盘股应有的量级"


def test_real_frame_has_both_channels_for_most_symbols(real_frame):
    per = real_frame.groupby("symbol")["basis"].nunique()
    assert float((per == 2).mean()) > 0.9, "两条通道应当几乎同时存在"


def test_real_read_only_on_the_parquet_file():
    _require_real_data()
    digest = hashlib.sha256(REAL.read_bytes()).hexdigest()
    shares.shares_frame(symbols=["sh600000", "sz000001"])
    assert hashlib.sha256(REAL.read_bytes()).hexdigest() == digest
