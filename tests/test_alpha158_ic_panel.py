"""步骤③面板装配器的口径测试：全部用合成帧，不读 data/，CI 里真跑。

这里测的不是「代码能不能跑」，而是四个**错了不会报错、只会静默给出可看数字**的点：
① `is_new` 依赖传入帧的绝对位置——切窗后再传会把它变成一个常数；
② 标签必须与特征同口径（hfq 收盘），且 t 行的标签只能用 t+horizon 的价；
③ `volume=0` 的日子 vwap 必须是 NaN——给 0 等于把停牌日当成一次真实的 vwap=0 观测；
④ 污染维名单必须与实际读 amount 的维一致：改了定义而名单没动，这里就红。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed.features_5d import (
    compute_window_features,
    compute_window_features_extended,
)
from tdxfeed.stage1_wide_table import FEAT26_ORDER

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]


def _panel_module():
    spec = importlib.util.spec_from_file_location(
        "alpha158_ic_panel_t", ROOT / "scripts" / "alpha158_ic_panel.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alpha158_ic_panel_t"] = mod
    spec.loader.exec_module(mod)
    return mod


# 只放三维权具：够测通路与口径，又不用为一次单测装配 158 列。
FAKE_PAYLOAD = {
    "columns": ["close", "high", "low", "open", "volume", "vwap"],
    "features": [
        {"name": "VWAPTEST", "expr": "$vwap / $close", "ops": []},
        {"name": "TESTRET", "expr": "$close / Ref($close, 1) - 1", "ops": ["Ref"]},
        {"name": "TESTMEAN", "expr": "Mean($high, 3)", "ops": ["Mean"]},
    ],
}

N_DAYS = 400
START = date(2024, 1, 2)


def _frames(amount_scale: float = 1.0, perturb_day: int | None = None):
    """确定性合成帧：日期升序、价格游走、volume 里埋一个 0（测 ③）。"""
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0005, 0.02, N_DAYS)
    close = 10.0 * np.cumprod(1 + rets)
    dates = [START + timedelta(days=i) for i in range(N_DAYS)]
    volume = np.round(rng.uniform(1e4, 5e4, N_DAYS), 2)
    volume[N_DAYS - 30] = 0.0                      # 停牌/零成交日
    raw = pd.DataFrame({
        "date": dates,
        "open": np.round(close * 0.99, 4),
        "high": np.round(close * 1.02, 4),
        "low": np.round(close * 0.97, 4),
        "close": np.round(close, 4),
        "volume": volume,
        "amount": np.round(close * volume * 100 * amount_scale, 4),
    })
    if perturb_day is not None:
        # 只改一天的 amount：比值型维度（amount_t / mean(amount)）对整体等比缩放是不变的，
        # 只有"单日被扰动"才测得出它到底读不读 amount。
        raw.loc[perturb_day, "amount"] = float(raw["amount"].iloc[perturb_day]) * 1.7
    # 合成一个"后复权帧"：因子恒为 2，价格翻倍——足够检验逐维分派与标签口径
    hfq = raw[["date", "open", "high", "low", "close", "volume"]].copy()
    for c in ("open", "high", "low", "close"):
        hfq[c] = hfq[c] * 2.0
    return raw, hfq


def _build(mod, raw, hfq, *, start=None, end=None, horizon=5):
    return mod.build_code_panel(
        raw, hfq, "600000",
        start=start or str(START), end=end or str(START + timedelta(days=N_DAYS - 1)),
        horizon=horizon, payload=FAKE_PAYLOAD)


# ---------------------------------------------------------------- ① 绝对位置 #
def test_is_new_uses_absolute_index_not_window_position():
    """窗口取最后 20 天时，is_new 必须全是 0：它判的是「上市未满 250 个交易日」。

    如果实现先把帧切到窗口再算，索引会从 0 重新数起，这 20 行会集体变成 is_new=1
    ——一维特征悄悄退化成一个只在窗口开头为 1 的开关，形状照样对。
    """
    mod = _panel_module()
    raw, hfq = _frames()
    late_start = str(raw["date"].iloc[-20])
    panel = _build(mod, raw, hfq, start=late_start)
    assert len(panel) > 0
    assert set(panel["is_new"].unique()) == {0}, (
        f"晚段窗口的 is_new 不该为 1：{sorted(panel['is_new'].unique())}")

    early = _build(mod, raw, hfq, end=str(raw["date"].iloc[250]))
    assert 1 in set(early["is_new"].unique()), "前 250 行里就该有 is_new=1，否则判据没生效"


# ------------------------------------------------------------------ ② 标签 #
def test_label_is_hfq_forward_return_aligned_to_t():
    """t 行标签 = hfq_close[t+5]/hfq_close[t] - 1，且用的是 hfq 而不是 raw。"""
    mod = _panel_module()
    raw, hfq = _frames()
    horizon = 5
    panel = _build(mod, raw, hfq, horizon=horizon)
    close_h = hfq.set_index("date")["close"]
    d0 = panel["date"].iloc[3]
    idx = list(hfq["date"]).index(date.fromisoformat(d0))
    want = close_h.iloc[idx + horizon] / close_h.iloc[idx] - 1.0
    got = float(panel["label"].iloc[3])
    assert got == pytest.approx(want, rel=1e-9), f"标签错位：{got} vs {want}"
    # 同一天 raw 的收益与 hfq 收益在这个合成因子里恰好等比，所以再钉一条：
    # 面板里价格类特征走 hfq（TESTMEAN = Mean($high,3) 应是 raw high 的两倍）
    assert float(panel["TESTMEAN"].iloc[3]) == pytest.approx(
        float(raw["high"].iloc[idx - 2: idx + 1].mean()) * 2.0, rel=1e-9), (
        "价格维没有按 hfq 口径装配")


def test_tail_rows_without_label_are_dropped_not_zeroed():
    """末尾不足 horizon 天的行没有标签，必须整行丢掉而不是留 NaN/0 进 IC。"""
    mod = _panel_module()
    raw, hfq = _frames()
    horizon = 5
    panel = _build(mod, raw, hfq, horizon=horizon)
    last_dates = {str(d) for d in raw["date"].iloc[-horizon:]}
    assert not (set(panel["date"]) & last_dates), "没有标签的行进了面板"
    assert panel["label"].notna().all()


# ------------------------------------------------------------------ ③ vwap #
def test_vwap_is_rebased_to_the_same_caliber_as_close():
    """`$vwap` 必须与 `$close` 同口径：跨口径的比值会静默变成复权因子的替身。

    两处都不显眼：① 日线 `volume` 的单位是**手**，`amount/volume` 是每股价的 100 倍
    （实测 `amount/(volume×100)` 落在当日 [low, high] 的天数占比 0.9999）；② hfq 帧的价
    是 raw × 当日因子。合成帧里 `amount = close×volume×100` ⇒ 每股 VWAP 恰等于 close ⇒
    同口径的 `vwap/close` 必须是 1.0，且**对复权因子是不变量**（因子从 ×2 换到 ×3 不该动它）。
    实装的错法是拿 raw 的 vwap 除 hfq 的 close：值随因子铺三个数量级，截面排序 95% 由
    因子决定（见 KI-DATA-005），把一条本来过 BH 的维压成不显著。
    """
    mod = _panel_module()
    raw, hfq = _frames()
    got = _build(mod, raw, hfq)["VWAPTEST"].dropna()
    assert np.allclose(got, 1.0, atol=1e-6), (
        f"同口径的 vwap/close 应为 1.0，实得中位 {float(got.median())}"
        f"（100 倍=volume 单位没换算，除尽子/跨口径=没 rebase 到 hfq）")

    raw2, hfq3 = _frames()
    for c in ("open", "high", "low", "close"):
        hfq3[c] = hfq3[c] * 1.5                       # 因子从 2 变成 3
    again = _build(mod, raw2, hfq3)["VWAPTEST"].dropna()
    assert np.allclose(again, 1.0, atol=1e-6), (
        f"复权因子变了，同口径比值不该变，实得中位 {float(again.median())}")


def test_zero_volume_day_gives_nan_vwap_not_zero():
    """volume=0 时 vwap 是 NaN：给 0 会把停牌日当成一次真实的 vwap=0 观测。"""
    mod = _panel_module()
    raw, hfq = _frames()
    panel = _build(mod, raw, hfq)
    bad_day = str(raw["date"].iloc[N_DAYS - 30])
    hit = panel[panel["date"] == bad_day]
    assert hit["VWAPTEST"].isna().all(), f"零成交日的 vwap 维不该有值：{hit['VWAPTEST'].tolist()}"


def test_vwap_dims_are_named_from_the_artifact():
    """污染维名单从工件里点名，不在这份脚本里手抄一份。"""
    mod = _panel_module()
    payload = json.loads((ROOT / "config" / "alpha158_expressions.json")
                         .read_text(encoding="utf-8"))
    dims = mod.vwap_dims_from_artifact(payload)
    assert dims, "工件里应当存在用 $vwap 的维度；一个都没有说明点名逻辑坏了"
    # 与工件文本重新对一遍：点名函数改了而清单没改就会不一致
    again = [f["name"] for f in payload["features"] if "$vwap" in f["expr"]]
    assert dims == again


# ---------------------------------------------------- ④ 污染维名单与实际一致 #
def test_feat26_amount_registry_matches_what_actually_moves_with_amount():
    """扰动某一天的 amount，改变的 FEAT26 维必须**恰好**是名单里那几个。

    这条就是名单的意义：代理段里退化的是这几维，名单少一维就漏判污染，多一维就把
    无辜的维一起排除掉。改 features_5d 的口径而不动名单，这里必红。
    """
    mod = _panel_module()
    day = N_DAYS - 60                     # 离尾部够远，5 日窗内的后续行都会跟着动
    raw, hfq = _frames()
    raw_p, _ = _frames(perturb_day=day)
    base = _build(mod, raw, hfq).set_index("date")
    alt = _build(mod, raw_p, hfq).set_index("date")

    moved = [c for c in FEAT26_ORDER
             if not np.allclose(base[c].astype(float).to_numpy(),
                                alt[c].astype(float).to_numpy(),
                                rtol=1e-9, equal_nan=True)]
    assert sorted(moved) == sorted(mod.FEAT26_AMOUNT_DIMS), (
        f"实际随 amount 变化的维 = {sorted(moved)}，与登记的 "
        f"{sorted(mod.FEAT26_AMOUNT_DIMS)} 不一致：污染面判断会跟着错")


def test_ratio_dims_are_scale_invariant_levels_are_not():
    """整体等比缩放 amount 只动水平型维，不动比值型维——这决定了代理段的伤害形态。

    成交额代理化不是把 amount 乘个系数，而是逐日换成 `close×volume`，所以五维都会动；
    但比值型两维（vol_ratio_t1 / amount_surge_3d）只按当日 VWAP 与收盘的差被动，
    水平型三维直接被改写量级。这条把两种退化分开钉住，报告里才能说清谁更不可信。
    """
    mod = _panel_module()
    raw, hfq = _frames()
    raw10, _ = _frames(amount_scale=10.0)
    base = _build(mod, raw, hfq).set_index("date")
    alt = _build(mod, raw10, hfq).set_index("date")

    moved = [c for c in FEAT26_ORDER
             if not np.allclose(base[c].astype(float).to_numpy(),
                                alt[c].astype(float).to_numpy(),
                                rtol=1e-9, equal_nan=True)]
    assert sorted(moved) == ["amt_per_price", "avg_amount_5d", "turnover_proxy"], (
        f"等比缩放该只动水平型三维，实得 {sorted(moved)}")
    for c in ("vol_ratio_t1", "amount_surge_3d"):
        assert c not in moved, f"{c} 是同窗比值，等比缩放不该动它"


def test_amount_registry_names_exist_in_feat26():
    mod = _panel_module()
    assert set(mod.FEAT26_AMOUNT_DIMS) <= set(FEAT26_ORDER), "名单里的维不在生产清单里"
    src = (inspect.getsource(compute_window_features)
           + inspect.getsource(compute_window_features_extended))
    assert "amount" in src, "features_5d 里已经没有 amount 了？名单该整体重估"


# ------------------------------------------------------------- 产物与原子写 #
def test_is_st_is_degenerate_from_this_entry_point():
    """从 `compute_window_features_extended` 直接建面板时，`is_st` 恒为 0。

    它的名字要调用方给（`get_limit(code, name=...)`），而日频面板没有逐日的股票名，
    所以这一维在本面板里没有信息。生产事件表里的 is_st 是另一条链（find_double_up 那侧）
    填的，两边都见过 True——所以这不是生产的缺陷，是**本面板的已知局限**，报告里
    is_st 的 IC 要按"没验"读。把它钉成用例，是为了让下一个人不会去解读这一维的数字。
    """
    mod = _panel_module()
    raw, hfq = _frames()
    panel = _build(mod, raw, hfq)
    assert set(panel["is_st"].unique()) == {0}, (
        "is_st 突然有值了：说明这条入口开始带股票名，报告口径要跟着改")
    assert "is_st" in FEAT26_ORDER


def test_parity_rows_expose_the_sign_convention():
    """qlib 的 ROC* 写成 `Ref($close,N)/$close`（过去价除现价），与现役 ret_5d 反向。

    `parity_rows` 的全部意义就在这条：两套里"看起来同名"的维，符号约定可能相反。
    读 IC 的人按直觉把 ROC5 当"5 日涨幅"，结论会整体反过来。
    """
    mod = _panel_module()
    panel = pd.DataFrame([
        {"date": d, "code": f"{i:06d}", "a": float(i), "b": float(i) * 2, "inv": -float(i)}
        for d in ("2026-01-05", "2026-01-06") for i in range(20)])
    rows = {(r["left"], r["right"]): r
            for r in mod.parity_rows(panel, [("a", "b"), ("a", "inv")], min_codes=5)}
    assert rows[("a", "b")]["mean_corr"] == pytest.approx(1.0)
    assert rows[("a", "inv")]["mean_corr"] == pytest.approx(-1.0), (
        f"反向关系必须现出负号：{rows[('a', 'inv')]}")
    assert rows[("a", "inv")]["note"] == "反向", "标注没跟上符号，报告里会被读成正向"
    assert rows[("a", "b")]["n_days"] == 2


def test_parity_rows_are_within_day_not_pooled():
    """日内同序、跨天水位相反：pooled 被量级水位主导，本函数必须给 +1。

    面板里 avg_amount_5d 是千万量级、VWAP0 在 1 附近——pooled 相关跟两维的语义没关系。
    这与 tdxfeed/alpha158/ic.py 的逐日判据是同一个坑，在这里也钉一遍。
    """
    mod = _panel_module()
    shift = {"2026-01-05": 0.0, "2026-01-06": 1000.0}
    panel = pd.DataFrame([
        {"date": d, "code": f"{i:06d}", "a": float(i) + shift[d], "b": float(i)}
        for d in shift for i in range(20)])
    row = mod.parity_rows(panel, [("a", "b")], min_codes=5)[0]
    assert row["mean_corr"] == pytest.approx(1.0), (
        f"日内该是 +1，实得 {row['mean_corr']}——退化成 pooled 了")
    pooled = panel["a"].corr(panel["b"], method="spearman")
    assert pooled < 0.6, f"对照组失真（pooled 也高说明构造没生效）：{pooled:.3f}"


def test_parity_rows_tolerate_a_dead_pair():
    """某一列整列缺失（未覆盖的维）时给 NaN 并保留这一行：漏掉一对等于宣称它没关系。"""
    mod = _panel_module()
    panel = pd.DataFrame({"date": ["2026-01-05"] * 20,
                          "a": [np.nan] * 20, "b": list(range(20))})
    row = mod.parity_rows(panel, [("a", "b")], min_codes=5)[0]
    assert np.isnan(row["mean_corr"]) and row["n_days"] == 0
    assert row["note"] == "样本/方差不足"


def test_redundancy_separates_same_day_twins_from_independent_signals():
    """158 维"打赢"26 维不等于信息多——同一族维度在同一天一起响。

    `ic_redundancy` 用逐日 IC 序列之间的相关来量这件事。用例构造两对：一对完全同日
    （corr=1），一对逐日反号（corr=-1），并取走每维与他集合的最大绝对相关——实现若
    改成取平均或只看同集合，这两条都会红。
    """
    mod = _panel_module()
    days = pd.date_range("2026-01-05", periods=40, freq="D").strftime("%Y-%m-%d")
    rng = np.random.default_rng(11)
    base = rng.normal(size=40)
    daily = pd.DataFrame({
        "A1": base, "A2": base * 2 + 1,          # 与 A1 同日同序 ⇒ corr = 1
        "F1": -base, }, index=days)              # 与 A1 反号 ⇒ corr = -1
    out = mod.ic_redundancy(daily, ["A1", "A2"], ["F1"])
    assert out["dup_within_alpha158"] == pytest.approx(1.0, abs=1e-9)
    best = {r["feature"]: r["max_abs_corr"] for r in out["cross"]}
    assert best["A1"] == pytest.approx(1.0, abs=1e-6), (
        f"A1 与 F1 逐日反号，绝对相关就是 1：{best}")
    assert out["cross_median"] == pytest.approx(1.0, abs=1e-6)


def test_redundancy_survives_a_dim_with_no_ic():
    """整列没有 IC 的维（常数维/未覆盖维）不能把可算的重叠度拖成 NaN。

    实测踩过：面板里 is_st 恒 0 ⇒ 它的 IC 全是 NaN ⇒ pandas 的相关给 NaN ⇒
    一个 NaN 进 mean/median，"Alpha158 内部 0.35" 整片变 nan。NaN 一旦吞掉平均值，
    报告就只剩下没有数字的段落。
    """
    mod = _panel_module()
    days = pd.date_range("2026-01-05", periods=40, freq="D").strftime("%Y-%m-%d")
    rng = np.random.default_rng(12)
    base = rng.normal(size=40)
    other = rng.normal(size=40)
    daily = pd.DataFrame({"A1": base, "A2": other, "DEAD": [np.nan] * 40}, index=days)
    out = mod.ic_redundancy(daily, ["A1", "A2", "DEAD"], ["A1"])
    assert np.isfinite(out["dup_within_alpha158"]), "死维把重叠度拖成 NaN 了"
    assert "DEAD" not in [r["feature"] for r in out["cross"]], (
        "给不出 IC 的维不该出现在跨集合对照里——它没有可比的序列")
    assert out["dims_without_ic"] == ["DEAD"], "没进表的维要单独点名，不能默默少算"


def test_json_safe_writes_strict_json():
    """run_meta 里的 NaN 必须写成 null：jq 见到裸 NaN 会当场失败。

    台账与巡检脚本都用 jq 取字段，`json.dumps` 默认却写出 `NaN`（Python 能读回，
    严格解析器不能）。这条把"能被 jq 读"钉成判据，而不是等下次巡检报错才发现。
    """
    import json as _json

    mod = _panel_module()
    obj = {"a": float("nan"), "b": [float("inf"), 1.0, {"c": float("-inf")}],
           "d": "文本", "e": 3}
    cleaned = mod.json_safe(obj)
    assert cleaned["a"] is None and cleaned["b"][0] is None and cleaned["b"][2]["c"] is None
    assert cleaned["b"][1] == 1.0 and cleaned["d"] == "文本" and cleaned["e"] == 3
    _json.dumps(cleaned, allow_nan=False)      # 不抛才算过


def test_atomic_write_parquet_leaves_no_temp(tmp_path):
    mod = _panel_module()
    p = tmp_path / "panel_x.parquet"
    df = pd.DataFrame({"date": ["2026-01-05"], "v": [1.0]})
    mod.atomic_write_parquet(df, p)
    assert p.exists() and not list(tmp_path.glob("*.tmp*"))
    assert pd.read_parquet(p).equals(df)


def test_report_names_both_sets_and_the_contamination():
    """报告必须把「哪些维被成交额代理污染」写在脸上——否则读者会拿污染维下结论。"""
    mod = _panel_module()
    idx = ["KMID", "ROC5", "avg_amount_5d", "ret_5d"]
    summary = pd.DataFrame({
        "n_days": [10, 10, 10, 10], "mean_ic": [0.02, -0.01, 0.05, 0.001],
        "ic_std": [0.1] * 4, "icir": [0.2] * 4, "pos_rate": [0.6] * 4,
        "nw_t": [2.0] * 4, "nw_p": [0.04] * 4, "nw_se": [0.01] * 4,
        "q_value": [0.08] * 4, "reject": [False] * 4,
    }, index=idx)
    meta = {"tag": "t", "codes_built": 3, "days": 10, "rows": 30,
            "sample_requested": 3, "skipped": {}, "horizon": 5, "min_codes": 2,
            "alpha": 0.05, "elapsed_sec": 1.0, "include_proxy": False,
            "proxy_caliber_start": "2026-09-16",
            "feat26_amount_dims": ["avg_amount_5d"],
            "alpha158_vwap_dims": ["VWAP0"], "codes_per_day_median": 3.0}
    text = "\n".join(mod.report_lines(summary, ["KMID", "ROC5"],
                                      ["avg_amount_5d", "ret_5d"], meta))
    assert "avg_amount_5d" in text and "VWAP0" in text
    assert "| Alpha158 | 2 |" in text and "| FEAT26 | 2 |" in text
    assert "已排除" in text


def test_report_noise_scale_comes_from_the_data():
    """「差多少不算结论」这个门槛必须由本表的 HAC 标准误算出来，不能写死。

    上一版报告里印着「均值噪声尺度 0.002」——那个数是我手写的，跟本样本对不上：
    实测各维 `nw_se` 的中位是 0.0092，最小值才 0.0021。写死的数字会随样本漂，
    而且漂的时候没人发现（它不参与任何计算，只出现在读法那句话里）。
    现在这条用例把两件事钉住：门槛取自 `median(nw_se)`，且换一批 `nw_se` 就跟着变。
    """
    mod = _panel_module()
    idx = ["A", "B"]
    base = dict(n_days=[656, 656], mean_ic=[0.05, 0.03], ic_std=[0.1] * 2,
                icir=[0.5, 0.3], pos_rate=[0.7, 0.6], nw_t=[5.0, 3.0],
                nw_p=[1e-6, 1e-3], q_value=[1e-6, 1e-3], reject=[True, True])
    meta = {"tag": "t", "codes_built": 3, "days": 656, "rows": 100,
            "sample_requested": 3, "skipped": {}, "horizon": 5, "min_codes": 2,
            "alpha": 0.05, "elapsed_sec": 1.0, "include_proxy": False,
            "proxy_caliber_start": "2026-09-16", "feat26_amount_dims": [],
            "alpha158_vwap_dims": [], "codes_per_day_median": 3.0}

    wide = pd.DataFrame({**base, "nw_se": [0.010, 0.012]}, index=idx)
    text_wide = "\n".join(mod.report_lines(wide, ["A"], ["B"], meta))
    assert "0.0110" in text_wide, "读法里的门槛应等于 median(nw_se)"
    assert "0.002" not in text_wide, "写死的旧门槛不得残留"

    thin = pd.DataFrame({**base, "nw_se": [0.002, 0.004]}, index=idx)
    text_thin = "\n".join(mod.report_lines(thin, ["A"], ["B"], meta))
    assert "0.0030" in text_thin, "门槛要随 nw_se 变——同一个字面量就是没接数据"
    assert text_wide != text_thin


# ------------------------------------------------------------------ 去重后的独立信息 #
def _dedup_inputs():
    """三条 Alpha158 维 + 两条 FEAT26 维，关系全是解析值：K1≡F1、K2=−K1、K3 独立。"""
    rng = np.random.default_rng(11)
    q, r = np.linalg.qr(rng.standard_normal((300, 3)))
    u = q * np.sign(np.diag(r))
    idx = [f"d{i:03d}" for i in range(300)]
    daily = pd.DataFrame({
        "F1": 0.25 + u[:, 0],
        "F2": 0.10 + u[:, 1],
        "K1": 0.20 + u[:, 0],                 # 与 F1 同一个信号（跨集合孪生）
        "K2": 0.05 - u[:, 0],                 # 与 K1 反号孪生
        "K3": 0.15 + u[:, 2],                 # 真独立
    }, index=idx)
    summary = pd.DataFrame({
        "n_days": [300] * 5,
        "mean_ic": [0.25, 0.10, 0.20, 0.05, 0.15],
        "ic_std": [0.1] * 5, "icir": [2.5] * 5, "pos_rate": [0.9] * 5,
        "nw_t": [9.0, 4.0, 7.0, 2.0, 6.0], "nw_p": [1e-18, 1e-4, 1e-14, 0.05, 1e-10],
        "nw_se": [0.009] * 5,
        "q_value": [1e-17, 2e-4, 4e-14, 0.05, 5e-10],
        "reject": [True, True, True, False, True],
    }, index=["F1", "F2", "K1", "K2", "K3"])
    return daily, summary


def test_dedup_views_separate_within_set_from_net_new():
    """「剩几维独立」与「净新增几维」是两个数，必须分开报。

    K1/K2 与现役 F1 是同一个信号：集合内看 Alpha158 剩 2 条（K1、K3），
    但扣掉现役已有的之后**净新增只有 1 条**（K3）。只看前者就会把替身当新增，
    那正是「切特征集值不值」这个问题上最贵的一次误读。
    """
    from tdxfeed.alpha158.ic import DEDUP_THRESHOLD

    mod = _panel_module()
    daily, summary = _dedup_inputs()
    out = mod.dedup_views(daily, summary, ["K1", "K2", "K3"], ["F1", "F2"])
    prim = out[DEDUP_THRESHOLD]
    assert prim["within"]["Alpha158"]["kept"] == 2
    assert prim["within"]["FEAT26"]["kept"] == 2
    assert prim["net_new"]["kept_dims"] == ["K3"], f"实得 {prim['net_new']}"
    assert prim["net_new"]["dropped_by_seed"] == 2
    assert prim["net_new"]["median_abs_ic"] == pytest.approx(0.15, abs=0.01)
    assert prim["net_new"]["reject"] == 1
    # 被挡掉的那批也必须有效应量：只有「净新增 vs 被挡」并排，才能判
    # 「现役覆盖的是弱维还是强维」。漏掉这个数，读者会默认挡掉的都是杂讯。
    assert prim["net_new"]["blocked_median_abs_ic"] == pytest.approx(0.125, abs=0.01)
    # 反向视图：现役相对 158 维的净新增。F2 与 K1/K3 都正交，所以它不是空集——
    # 「谁替身谁」必须两个方向都算，只问单边会得出「借来的一定划算」。
    assert prim["reverse"]["kept_dims"] == ["F2"], f"实得 {prim['reverse']}"
    assert prim["reverse"]["dropped_by_seed"] == 1


def test_dedup_view_names_the_dims_blocked_within_the_set():
    """集内换位也要点名：只点名「被现役挡」会让『离开净新增』读起来像信号消失。

    h=5 的 47 条在别的窗口少了 8–11 条，其中大多数不是变弱，而是**同族里另一条当了代表**
    （`SUMN60` 与 `SUMP60` 的 `|corr|` 恰为 1.0）。这一层看不见，读者就会把「清单换人」
    读成「信息丢了」，而那正好是候选集取舍上最贵的一次误读。
    """
    mod = _panel_module()
    daily, summary = _dedup_inputs()
    # K4 与 K3 是同一个自由度（差一个常数 ⇒ |corr|=1），但与现役两套都正交：
    # 它只能被**集内**的 K3 吸收，落不进 seed 那份清单。
    daily["K4"] = daily["K3"] - 0.03
    summary.loc["K4"] = [300, 0.12, 0.1, 2.0, 0.9, 5.0, 1e-9, 0.009, 3e-6, True]
    dims = ["K1", "K2", "K3", "K4"]
    prim = mod.dedup_views(daily, summary, dims, ["F1", "F2"])[mod.DEDUP_PRIMARY]
    nn = prim["net_new"]
    assert [t["dim"] for t in nn["blocked_by_seed"]] == ["K1", "K2"], \
        f"K1/K2 是现役 F1 的替身，该进 seed 那份清单：{nn}"
    twin = nn["blocked_within"]
    assert [t["dim"] for t in twin] == ["K4"], f"K4 只能被集内的 K3 吸收：{twin}"
    assert twin[0]["by"] == "K3" and twin[0]["abs_corr"] > 0.99, f"没点到挡它的那条同族维：{twin[0]}"
    # 两份清单合起来 = 全部被挡掉的维，且互不重叠（谁挡的要能被唯一归因）
    assert nn["dropped_by_seed"] == 2 and nn["dropped_within"] == 1
    assert not ({t["dim"] for t in nn["blocked_by_seed"]} & {t["dim"] for t in twin})


def test_dedup_views_report_every_threshold():
    """三档阈值并报：独立维数不能读成一个阈值的 artifact。"""
    mod = _panel_module()
    daily, summary = _dedup_inputs()
    out = mod.dedup_views(daily, summary, ["K1", "K2", "K3"], ["F1", "F2"])
    assert sorted(out) == [0.7, 0.8, 0.9], f"实得 {sorted(out)}"
    counts = [out[t]["net_new"]["kept"] for t in (0.7, 0.8, 0.9)]
    assert counts[0] <= counts[1] <= counts[2], f"阈值越高净新增不该变少：{counts}"


def test_dedup_view_names_the_dims_blocked_by_seed():
    """被 seed 挡掉的维要**点名到「谁挡的」**，不能只给条数与中位 |IC|。

    「现役已经覆盖了哪几条」是这套比较里最容易被糊成一句的结论：条数只说「挡掉 28 条」，
    读者会以为是 28 条一一对应；只有点到 `K1 ← F1` 这种程度，才能核对挡掉的到底是同一个
    自由度、还是名义上相关的两条。（条数与中位 |IC| 继续留着：那是强度，名字是身份。）
    """
    mod = _panel_module()
    daily, summary = _dedup_inputs()
    out = mod.dedup_views(daily, summary, ["K1", "K2", "K3"], ["F1", "F2"])
    blocked = out[mod.DEDUP_PRIMARY]["net_new"]["blocked_by_seed"]
    assert isinstance(blocked, list), f"实得 {type(blocked)}：要的是记录清单"
    assert [b["dim"] for b in blocked] == ["K1", "K2"], \
        f"被现役挡的该是 K1、K2 两条替身（按 |mean_IC| 序）：{blocked}"
    assert all(b["by"] == "F1" and b["abs_corr"] > 0.99 for b in blocked), \
        f"没点到挡它们的那条 seed 维：{blocked}"
    assert out[mod.DEDUP_PRIMARY]["net_new"]["dropped_by_seed"] == len(blocked), \
        "条数与清单必须一致"


def test_report_renders_the_dedup_table():
    """报告里必须同时出现「独立维数」「净新增维数」和点名到维的净新增清单。"""
    mod = _panel_module()
    daily, summary = _dedup_inputs()
    a158, f26 = ["K1", "K2", "K3"], ["F1", "F2"]
    meta = {"tag": "t", "codes_built": 3, "days": 300, "rows": 900,
            "sample_requested": 3, "skipped": {}, "horizon": 5, "min_codes": 2,
            "alpha": 0.05, "elapsed_sec": 1.0, "include_proxy": False,
            "proxy_caliber_start": "2026-09-16", "feat26_amount_dims": [],
            "alpha158_vwap_dims": [], "codes_per_day_median": 3.0,
            "dedup": mod.dedup_views(daily, summary, a158, f26)}
    text = "\n".join(mod.report_lines(summary, a158, f26, meta))
    assert "净新增" in text and "独立信号" in text
    assert "K3" in text, f"净新增清单没点到维名：{text[:200]}"
    # 并集必须由代码加好：让读者自己加两个表里的数，就会把「集合内独立」与
    # 「净新增」两种口径加错组合（2+2、57+15 之类），得出一个谁都没算过的数。
    assert "并集 = 现役独立 2 + 净新增 1 = 3 条独立信号" in text
    assert "被挡" in text, "被 seed 挡掉的那批没有效应量列，读者会默认它是杂讯"


# ------------------------------------------------------------------ 多 horizon #
def test_label_formula_is_single_sourced():
    """标签算式只能有一份：装配侧必须调用 `forward_return`，不许自己再写 shift。

    多 horizon 的全部前提就是「换 horizon 只换标签列」。装配侧与 relabel 侧各写
    一份 shift 的话，两者会在尾段悄悄分叉——而尾段正是最容易写错的地方。
    """
    mod = _panel_module()
    src = inspect.getsource(mod.build_code_panel)
    assert "forward_return(" in src, "装配侧没有走共享的标签算式"
    assert ".shift(-" not in src, "装配侧自己又写了一份 shift：两处口径会分叉"

    close = pd.Series([10.0, 11.0, 12.1, 12.1])
    got = mod.forward_return(close, 1).to_numpy()
    assert got[0] == pytest.approx(0.1) and got[1] == pytest.approx(0.1)
    assert got[2] == pytest.approx(0.0)
    assert np.isnan(got[3]), "尾部不足 horizon 天必须是 NaN，不是 0"


def test_attach_horizon_labels_reproduces_the_primary_label():
    """复用面板加标签列时，主 horizon 那一列必须逐格重现面板里已有的 label。

    这条不是「顺便测一下」：它是 relabel 路径与装配路径唯一的咬合点。日期没对齐、
    用了 raw 价而不是 hfq、shift 的轴错了——都会在这里红，而下游每个数都靠它。
    """
    mod = _panel_module()
    raw, hfq = _frames()
    panel = _build(mod, raw, hfq, horizon=5)
    closes = {"600000": hfq.assign(date=hfq["date"].astype(str)).set_index("date")["close"]
              .astype(float).sort_index()}

    out, stats = mod.attach_horizon_labels(panel, closes, horizons=(5, 10, 20))

    assert len(out) == len(panel), "加标签列不许丢行：丢行会悄悄改变横截面"
    assert {"label", "label_h5", "label_h10", "label_h20"} <= set(out.columns)
    assert stats["mismatch"] == 0, f"主 horizon 与面板 label 不一致：{stats}"
    np.testing.assert_allclose(out["label_h5"].to_numpy(dtype=float),
                               panel["label"].to_numpy(dtype=float),
                               rtol=1e-12, equal_nan=True)
    # 手工复算中间一行：t→t+10 的 hfq 收盘收益
    series = closes["600000"]
    pos_of = {dd: i for i, dd in enumerate(series.index)}
    d = out["date"].iloc[50]
    pos = pos_of[d]
    assert out["label_h10"].iloc[50] == pytest.approx(
        series.iloc[pos + 10] / series.iloc[pos] - 1.0)
    # 尾部缺不缺，判据是「t+N 在不在价格帧里」——面板建的时候按 h=5 已经丢过一批行，
    # 所以「NaN 的格数」不等于 N，只能逐行对着帧的位置算。
    for h in (5, 10, 20):
        expect_nan = np.array([pos_of[dd] + h >= len(series) for dd in out["date"]])
        assert (out[f"label_h{h}"].isna().to_numpy() == expect_nan).all(), (
            f"h={h} 的缺失位置与帧内位置不吻合")


def test_attach_horizon_labels_counts_unmatched_rows_instead_of_ignoring_them():
    """面板里有、价格帧里没有的那一天：给 NaN 并**计数**，不许静默对齐错位。"""
    mod = _panel_module()
    raw, hfq = _frames()
    panel = _build(mod, raw, hfq, horizon=5)
    s = (hfq.assign(date=hfq["date"].astype(str)).set_index("date")["close"]
         .astype(float).sort_index())
    s = s.drop(s.index[10])                       # 抽掉一天
    out, stats = mod.attach_horizon_labels(panel, {"600000": s}, horizons=(5,))
    assert stats["unmatched_rows"] == 1, f"实得 {stats}"
    assert int(out["label_h5"].isna().sum()) == 1
    # 抽掉一天，代价不止那一天：帧尾 5 行从此失去 t+5 伙伴，面板里它们却仍带 label。
    # mismatch = 1 + horizon 就是这个算术——帧不完整时尾段标签不可信，必须被抓住。
    assert stats["mismatch"] == 1 + 5, f"实得 {stats}"


def test_attach_horizon_labels_is_nan_not_zero_when_a_code_has_no_frame():
    """整只票没有价格帧：标签全 NaN + 计数，绝不填 0（0 是「知道没有」）。"""
    mod = _panel_module()
    raw, hfq = _frames()
    panel = _build(mod, raw, hfq, horizon=5)
    out, stats = mod.attach_horizon_labels(panel, {}, horizons=(5, 10))
    assert stats["codes_without_frame"] == 1, f"实得 {stats}"
    assert out["label_h5"].isna().all() and out["label_h10"].isna().all()
    assert len(out) == len(panel)


# ---------------------------------------------------------- 多 horizon 与逐年衰减（分析层） #
def _multi_panel(mod):
    """一个小面板：12 只票 × 40 天、两天一维、跨年，标签按 h1/h5 在尾部留 NaN。

    码数必须 ≥ 8 左右：`rank_ic` 要 5 对以上有效样本才给数，3 只票的横截面会让每一天
    都是 NaN——那样测的是构造，不是判据。
    """
    n_days, n_codes = 40, 12
    days = [date(2024, 12, 30) + timedelta(days=i) for i in range(n_days)]
    rng = np.random.default_rng(3)
    rows = []
    for k in range(n_codes):
        px = 10.0 * np.cumprod(1 + rng.normal(0.0005, 0.02, n_days))
        for j, d in enumerate(days):
            rows.append({
                "date": str(d), "code": f"6000{k:02d}",
                "label_h1": px[j + 1] / px[j] - 1.0 if j + 1 < n_days else np.nan,
                "label_h5": px[j + 5] / px[j] - 1.0 if j + 5 < n_days else np.nan,
                "F1": (k * 7 + j) % 11, "F2": (k * 3 + 2 * j) % 9,
                "A1": -((k * 5 + j) % 13),
            })
    return pd.DataFrame(rows)


def test_horizon_block_runs_the_same_judgment_per_horizon():
    """每个 horizon 都走同一套判据，只是换标签列与 HAC 滞后。

    `maxlags` 必须跟着 horizon 走：h=1 的标签不重叠，套 h=5 的滞后是在给不存在的
    自相关留余量；反过来 h=20 用 5 会低估标准误。
    """
    mod = _panel_module()
    panel = _multi_panel(mod)
    out = mod.horizon_block(panel, ["F1", "F2", "A1"], ["A1"], ["F1", "F2"],
                            horizon=5, min_codes=2, alpha=0.05)
    assert out["horizon"] == 5
    assert out["days_with_ic"] > 0
    for key in ("median_abs_ic", "reject", "within", "net_new", "reverse"):
        assert key in out, f"缺 {key}：{sorted(out)}"
    assert set(out["median_abs_ic"]) == {"Alpha158", "FEAT26"}
    assert out["net_new"]["kept"] + out["net_new"]["dropped_by_seed"]
    # h=1 与 h=5 是两个独立的块：标签列不同，数字也该不同
    out1 = mod.horizon_block(panel, ["F1", "F2", "A1"], ["A1"], ["F1", "F2"],
                             horizon=1, min_codes=2, alpha=0.05)
    assert out1["days_with_ic"] >= out["days_with_ic"]


def test_yearly_decay_reports_median_abs_ic_and_sign_agreement():
    """逐年衰减要给两个数：那一年的 IC 中位绝对值，以及**与全样本同号的维占比**。

    只报均值会掩盖「一半维在换向」；同号率才是衰减的形状。年份从日期字符串取，
    所以跨年面板要能分开。
    """
    from tdxfeed.alpha158.ic import daily_ic

    mod = _panel_module()
    panel = _multi_panel(mod)
    cfg = mod.IcConfig(label_col="label_h5", min_codes=2, maxlags=5, alpha=0.05)
    daily = daily_ic(panel, ["F1", "F2", "A1"], cfg)
    summary = mod.summarize_ic(daily, cfg)
    groups = {"全部": ["F1", "F2", "A1"], "净新增": ["F1"]}
    out = mod.yearly_decay(daily, summary, groups, maxlags=5)
    assert sorted(out["years"]) == ["2024", "2025"], f"实得 {out['years']}"
    g = out["groups"]["全部"]
    assert set(g["median_abs_ic"]) <= set(out["years"])
    assert set(g["median_abs_ic"]) == set(g["sign_agreement"]) == set(g["n_dims"])
    for y in g["median_abs_ic"]:
        assert 0.0 <= g["sign_agreement"][y] <= 1.0
        assert g["n_dims"][y] > 0, "没有维的年份不该出现"
    nn = out["groups"]["净新增"]
    assert nn["n_dims"], "净新增组一个年份都没出：聚合层把有数的维丢了"
    assert all(v == 1 for v in nn["n_dims"].values()), f"单维组该每年 1 条：{nn['n_dims']}"


def test_horizon_block_persists_both_blocker_lists():
    """每个 horizon 的块要落两份「被谁挡」清单，报告与文档的换位结论才不用一次性脚本。

    块里只有条数的话，「47 条在 h=20 少了几条」就没法回答是现役覆盖还是同族换位——
    而那正是这份研究交付的结论之一。
    """
    mod = _panel_module()
    panel = _multi_panel(mod)
    out = mod.horizon_block(panel, ["F1", "F2", "A1"], ["A1"], ["F1", "F2"],
                            horizon=5, min_codes=2, alpha=0.05)
    nn = out["net_new"]
    for key in ("blocked_by_seed", "blocked_within"):
        assert key in nn, f"块里缺 {key}：{sorted(nn)}"
        assert all(set(r) == {"dim", "by", "abs_corr"} for r in nn[key]), f"{key} 记录形状不对"
    dims = [r["dim"] for r in nn["blocked_by_seed"]] + [r["dim"] for r in nn["blocked_within"]]
    assert len(dims) == len(set(dims)) == nn["dropped_by_seed"] + nn["dropped_within"], \
        "两份清单该不重不漏地拼成全部被挡掉的维"


def test_yearly_decay_persists_the_per_year_noise_scale():
    """逐年表也要有「该年该组自己的」门槛：171 天的那段与 656 天不是一把尺子。

    报告里 3.7 那张「比值 = 中位 |IC| ÷ 该年该组 2×SE」的表必须能从产物复核；如果
    `yearly_decay` 不落这个数，它就只存在于一次性脚本里，下一轮重跑就没了。
    `maxlags` 由调用方给（与主口径同一判据），函数自己不猜。
    """
    from tdxfeed.alpha158.ic import daily_ic

    mod = _panel_module()
    panel = _multi_panel(mod)
    cfg = mod.IcConfig(label_col="label_h5", min_codes=2, maxlags=5, alpha=0.05)
    daily = daily_ic(panel, ["F1", "F2", "A1"], cfg)
    summary = mod.summarize_ic(daily, cfg)
    groups = {"全部": ["F1", "F2", "A1"]}
    out = mod.yearly_decay(daily, summary, groups, maxlags=5)
    g = out["groups"]["全部"]
    assert set(g["noise_scale"]) == set(out["years"]), "门槛要逐年出"
    checked = 0
    for y in out["years"]:
        sub = daily.loc[[d for d in daily.index if str(d).startswith(y)], groups["全部"]]
        vals = [mod.cv_utils.ic_newey_west(sub[c], maxlags=5)["se_hac"]
                for c in groups["全部"]]
        expect = float(np.nanmedian(vals)) if any(np.isfinite(vals)) else float("nan")
        got = g["noise_scale"][y]
        if np.isfinite(expect):
            assert got == pytest.approx(expect), f"{y} 年门槛不是该年该组的数"
            checked += 1
        else:
            # 那一年不足 10 天：HAC 给 NaN，这里也必须 NaN——编一个门槛比缺门槛更坏
            assert np.isnan(got), f"{y} 年样本不足却给出了门槛 {got}"
    assert checked >= 1, "一年都没算出门槛：这个字段等于没实现"


def test_horizon_block_persists_its_own_noise_scale():
    """每个 horizon 的噪声门槛要跟着块落盘：HAC 标准误是随 maxlags 变的数。

    h=1 的标签不重叠、h=20 的重叠 19 天，同一张日频 IC 表算出的 `nw_se` 中位差一截。
    拿主口径那一条门槛去判其他 horizon 的差值，等于用一个本块不存在的门槛；而块里
    没有这个数，报告的门槛列就只能写死一个常量。
    """
    mod = _panel_module()
    panel = _multi_panel(mod)
    cols = ["F1", "F2", "A1"]
    out = mod.horizon_block(panel, cols, ["A1"], ["F1", "F2"],
                            horizon=5, min_codes=2, alpha=0.05)
    cfg = mod.IcConfig(label_col="label_h5", min_codes=2, maxlags=5, alpha=0.05)
    expect = float(mod.summarize_ic(mod.daily_ic(panel, cols, cfg), cfg)["nw_se"].median())
    assert out["noise_scale"] == pytest.approx(expect), "门槛不是本块 summary 的中位 SE"
    out1 = mod.horizon_block(panel, cols, ["A1"], ["F1", "F2"],
                             horizon=1, min_codes=2, alpha=0.05)
    assert out1["noise_scale"] > 0, "门槛给成 0/NaN：报告里 2×SE 会变成「什么都能分开」"


def test_report_noise_column_is_per_horizon():
    """多 horizon 表里每一行的门槛是该块自己的 2×SE，不是主口径那一条。

    判据只能钉在「那一行的那一格」上：0.0250 这种数在本表别处也会出现（FEAT26 的
    |IC| 中位就是它），整篇文本 `in` 判断会给出假通过。
    """
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    meta = _multi_meta(mod)
    meta["horizons"]["1"]["noise_scale"] = 0.0050
    meta["horizons"]["5"]["noise_scale"] = 0.0125
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], meta))
    assert "2×SE" in text, "没给判定门槛：读者会把 0.004 的差当成结论"

    def _row(h):
        lines = [ln for ln in text.splitlines() if ln.startswith(f"| {h} | ")]
        assert len(lines) == 1, f"多 horizon 表里 h={h} 的行不唯一/缺失：{lines}"
        return lines[0]

    def _threshold(h):
        # 门槛是倒数第二格（最后一格是「被现役挡」的点名，长度会变，别按末位取）
        cells = [c.strip() for c in _row(h).strip("|").split("|")]
        return cells[-2]

    assert _threshold(1) == "0.0100", f"h=1 行的门槛格不是自己的 2×SE：{_row(1)}"
    assert _threshold(5) == "0.0250", f"h=5 行的门槛格不是自己的 2×SE：{_row(5)}"


def test_report_read_note_follows_the_actual_day_counts():
    """读法要由天数派生：四块同基时还写「天数会随 h 变短」就是凭空提醒。"""
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    same = _multi_meta(mod)
    for blk in same["horizons"].values():
        blk["days_with_ic"] = 39
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], same))
    assert "同一批天" in text and "39" in text, f"同基要说同基：{text[-900:]}"
    diff = _multi_meta(mod)
    text2 = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], diff))
    assert "同一批天" not in text2, "天数不等还写同基：覆盖差被抹掉了"
    assert "窗口尽头" in text2, "覆盖不等时要说明那是尾段窗口，不是信号消失"


def test_report_reuse_line_does_not_claim_zero_seconds():
    """复用面板时没有 `elapsed_sec`：印「耗时 0s」会被读成这一轮不花钱。

    复用省下的是**装配**（几十分钟），标签列与判据那一轮仍然要跑。报告要么印实测
    秒数，要么明说「装配未重跑」——不能拿缺省值 0 冒充一个测过的数。
    """
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    meta = _multi_meta(mod)
    meta.pop("elapsed_sec", None)
    meta["reused_panel"] = "data/alpha158_ic/panel_x.parquet"
    meta["label_attach_sec"] = 22.6
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], meta))
    assert "耗时 0s" not in text, f"缺省值被印成了实测：{text.splitlines()[5]}"
    assert "22.6s" in text, "加标签列的实际秒数要出现在报告里"
    meta.pop("label_attach_sec")
    text2 = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], meta))
    assert "耗时" not in text2, "两个数都没测过时，宁可整条不写"


def test_report_yearly_table_shows_ratio_to_that_years_threshold():
    """逐年表的每一格要同时给「比值 = 中位 |IC| ÷ 该年 2×SE」，否则 0.0121 会被读成没了。

    判据只能钉在行内容上：整篇文本里 0.0 开头的数字到处都是，`in` 判断会假通过。
    """
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    meta = _multi_meta(mod)
    meta["yearly"] = {"years": ["2024", "2025"],
                      "groups": {"净新增": {
                          "median_abs_ic": {"2024": 0.0400, "2025": 0.0100},
                          "sign_agreement": {"2024": 1.0, "2025": 0.5},
                          "n_dims": {"2024": 1, "2025": 1},
                          # 2×SE：2024 = 0.0200、2025 = 0.0300 ⇒ 比值 2.00 与 0.33
                          "noise_scale": {"2024": 0.0100, "2025": 0.0150}}}}
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], meta))
    rows = [ln for ln in text.splitlines() if ln.startswith("| 净新增")]
    assert len(rows) == 3, f"该组应给出中位/比值/同号率三行：{rows}"
    ratio = next(ln for ln in rows if "比值" in ln)
    assert "2.00" in ratio and "0.33" in ratio, f"比值不是该年自己的门槛算的：{ratio}"


def test_report_multihorizon_names_dims_blocked_by_current_set():
    """多 horizon 表要按窗口点出「被现役挡掉的那几条」——跨窗口的覆盖关系会变。

    只给条数就会漏掉一件要紧事：同一维可能只在某个窗口被现役覆盖（本面板实测
    `STD60` 只在 h=20 被 `volatility_5d` 挡）。这一列也是「现役已经覆盖了什么」的
    唯一逐窗证据，所以名字必须在产物里，而不是只活在一次性脚本的输出里。
    """
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    meta = _multi_meta(mod)
    meta["horizons"]["1"]["net_new"]["blocked_by_seed"] = []
    meta["horizons"]["5"]["net_new"]["blocked_by_seed"] = [
        {"dim": "A1", "by": "F1", "abs_corr": 0.93}]
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], meta))

    def _row(h):
        return next(ln for ln in text.splitlines() if ln.startswith(f"| {h} | "))

    assert "A1←F1" in _row(5), f"h=5 行没点出被现役挡的维：{_row(5)}"
    assert "A1←F1" not in _row(1), "没挡就别写"


def test_parse_horizons_includes_primary_and_rejects_nonpositive():
    """主 horizon 必须在列表里（没有它就没有与主口径对齐的锚点）；h≤0 直接拒。"""
    mod = _panel_module()
    assert mod.parse_horizons("1,10,20", 5) == [1, 5, 10, 20]
    assert mod.parse_horizons("", 5) == [5]
    assert mod.parse_horizons(" 5 , 5 ", 5) == [5], "重复与空白要能吞掉"
    for bad in ("0", "5,-3"):
        with pytest.raises(ValueError):
            mod.parse_horizons(bad, 5)
    with pytest.raises(ValueError):
        mod.parse_horizons("1", 0)


def _multi_meta(mod):
    return {"tag": "t", "codes_built": 3, "days": 40, "rows": 480,
            "sample_requested": 3, "skipped": {}, "horizon": 5, "min_codes": 2,
            "alpha": 0.05, "elapsed_sec": 1.0, "include_proxy": False,
            "proxy_caliber_start": "2026-09-16", "feat26_amount_dims": [],
            "alpha158_vwap_dims": [], "codes_per_day_median": 12.0,
            "horizons": {
                "1": {"horizon": 1, "days_with_ic": 39, "noise_scale": 0.0070,
                      "median_abs_ic": {"Alpha158": 0.020, "FEAT26": 0.030},
                      "reject": {"Alpha158": 1, "FEAT26": 1},
                      "within": {"Alpha158": 1, "FEAT26": 2},
                      "net_new": {"kept": 1, "reject": 0, "median_abs_ic": 0.02,
                                  "blocked_median_abs_ic": 0.03, "dropped_by_seed": 0,
                                  "dropped_within": 0, "kept_dims": ["A1"]},
                      "reverse": {"kept": 2, "median_abs_ic": 0.03,
                                  "kept_dims": ["F1", "F2"]}},
                "5": {"horizon": 5, "days_with_ic": 35, "noise_scale": 0.0110,
                      "median_abs_ic": {"Alpha158": 0.040, "FEAT26": 0.031},
                      "reject": {"Alpha158": 1, "FEAT26": 2},
                      "within": {"Alpha158": 1, "FEAT26": 2},
                      "net_new": {"kept": 1, "reject": 1, "median_abs_ic": 0.04,
                                  "blocked_median_abs_ic": 0.03, "dropped_by_seed": 0,
                                  "dropped_within": 0, "kept_dims": ["A1"]},
                      "reverse": {"kept": 2, "median_abs_ic": 0.03,
                                  "kept_dims": ["F1", "F2"]}}},
            "yearly": {"years": ["2024", "2025"],
                       "groups": {"净新增": {"median_abs_ic": {"2024": 0.05, "2025": 0.02},
                                             "sign_agreement": {"2024": 1.0, "2025": 0.5},
                                             "n_dims": {"2024": 1, "2025": 1}}}}}


def test_report_renders_multi_horizon_and_yearly_tables():
    """两张表都要出现，且要带「天数随 h 变短」与「同号率换向」的读法。

    多 horizon 表少一行就会被读成「只有两个窗口」；逐年表没有同号率就会被读成
    「均值掉了就是变弱」，而换向与变弱的处置完全不同。
    """
    mod = _panel_module()
    idx = ["A1", "F1", "F2"]
    summary = pd.DataFrame({
        "n_days": [35] * 3, "mean_ic": [0.04, 0.03, -0.02], "ic_std": [0.1] * 3,
        "icir": [0.4] * 3, "pos_rate": [0.6] * 3, "nw_t": [4.0, 3.0, -2.0],
        "nw_p": [1e-4] * 3, "nw_se": [0.01] * 3, "q_value": [3e-4] * 3,
        "reject": [True, True, False]}, index=idx)
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1", "F2"], _multi_meta(mod)))
    assert "多 horizon" in text and "逐年衰减" in text
    assert "| 1 | 39 |" in text and "| 5 | 35 |" in text, "两个 horizon 的行都要在"
    assert "maxlags=h" in text, "没说清每块的 HAC 滞后跟着 h 走"
    assert "净新增 同号率" in text and "0.50" in text
    assert "换向" in text, "同号率的含义必须写在表旁边"


def test_report_without_multi_horizon_omits_both_sections():
    """没跑多 horizon 时不要出现空表——空表看起来像「结论是没有」。"""
    mod = _panel_module()
    summary = pd.DataFrame({
        "n_days": [10] * 2, "mean_ic": [0.04, 0.03], "ic_std": [0.1] * 2,
        "icir": [0.4] * 2, "pos_rate": [0.6] * 2, "nw_t": [4.0, 3.0],
        "nw_p": [1e-4] * 2, "nw_se": [0.01] * 2, "q_value": [3e-4] * 2,
        "reject": [True, True]}, index=["A1", "F1"])
    meta = _multi_meta(mod)
    meta.pop("horizons")
    meta.pop("yearly")
    text = "\n".join(mod.report_lines(summary, ["A1"], ["F1"], meta))
    assert "多 horizon" not in text and "逐年衰减" not in text


# ------------------------------------------------- 代理段敏感性（KI-DATA-001 的位移） #
def _two_segments():
    """两段日频 IC：同一条维在代理段前后的均值不同，另一条不变，第三条一段全 NaN。

    解析可对照的量：`S` 前后都是 0.10（口径改了但没动它的信息）、`W` 从 0.10 掉到 −0.10
    （方向都翻）、`D` 在代理段整段给不出 IC（伪信号那一路）。
    """
    rng = np.random.default_rng(7)
    pre = pd.DataFrame({"S": 0.10 + rng.normal(0, 0.10, 200),
                        "W": 0.10 + rng.normal(0, 0.10, 200),
                        "D": np.full(200, np.nan)},
                       index=pd.Index([f"2026-08-{i % 28 + 1:02d}" for i in range(200)]))
    post = pd.DataFrame({"S": 0.10 + rng.normal(0, 0.10, 60),
                         "W": -0.10 + rng.normal(0, 0.10, 60),
                         "D": np.zeros(60)},
                        index=pd.Index([f"2026-10-{i % 27 + 1:02d}" for i in range(60)]))
    return pre, post


def test_proxy_shift_compares_the_same_judgment_across_the_caliber_break():
    """位移必须用**同一判据**（`summarize_ic` + HAC）算两段，差的 SE 由两段自己的 SE 合成。

    两个坑：① 一段给不出 IC 时差必须是 NaN 并计数，不能把「没验到」当成 0 位移；
    ② `se_diff` 不能写死，必须是 `sqrt(pre² + post²)`——代理段只有几十天，它的 SE
    比主段大一截，用主段的门槛判代理段会把噪声读成位移。
    """
    from tdxfeed.alpha158.ic import IcConfig, summarize_ic

    mod = _panel_module()
    pre, post = _two_segments()
    out = mod.proxy_shift(pre, post, ["S", "W", "D"], maxlags=5)
    rows = {r["dim"]: r for r in out["rows"]}
    assert set(rows) == {"S", "W", "D"}
    s = rows["S"]
    assert abs(s["diff"]) < 0.02, f"同一条不该报出大位移：{s}"
    w = rows["W"]
    assert w["diff"] < -0.15 and abs(w["z"]) > 4, f"方向翻转必须看得见：{w}"
    d = rows["D"]
    assert np.isnan(d["diff"]) and d["missing"], f"一段全 NaN 要给 NaN 并计数：{d}"
    assert out["uncomparable"] == 1, f"实得 {out}"
    # se_diff 是两段 SE 的平方和开根，不是其中一段
    cfg = IcConfig(min_codes=1, maxlags=5, alpha=0.05)
    sp = float(summarize_ic(pre[["S", "W", "D"]], cfg).loc["S", "nw_se"])
    sq = float(summarize_ic(post[["S", "W", "D"]], cfg).loc["S", "nw_se"])
    assert s["se_diff"] == pytest.approx(float(np.hypot(sp, sq)))
    assert out["pre_days"] == pre.shape[0] and out["post_days"] == post.shape[0]


def test_report_renders_the_proxy_sensitivity_table():
    """敏感性表要点名「受污染维」并给每段天数——否则读者会拿 30 天的位移去比 656 天。"""
    mod = _panel_module()
    idx = ["S", "W", "D"]
    summary = pd.DataFrame({
        "n_days": [200] * 3, "mean_ic": [0.1, 0.1, np.nan], "ic_std": [0.1] * 3,
        "icir": [1.0] * 3, "pos_rate": [0.9] * 3, "nw_t": [8.0, 8.0, np.nan],
        "nw_p": [1e-16] * 3, "nw_se": [0.0125] * 3, "q_value": [1e-15] * 3,
        "reject": [True, True, False]}, index=idx)
    meta = _multi_meta(mod)
    meta.pop("horizons", None)
    meta.pop("yearly", None)
    pre, post = _two_segments()
    meta["proxy_sensitivity"] = mod.proxy_shift(pre, post, ["S", "W", "D"], maxlags=5)
    meta["proxy_caliber_start"] = "2026-09-16"
    text = "\n".join(mod.report_lines(summary, ["S"], ["W", "D"], meta))
    assert "代理段敏感性" in text
    assert "200" in text and "60" in text, "两段各自的天数要在表旁边"
    # 只在这张表里找行：`| D |` 这种前缀在上面的 |IC| 前 15 表里同样出现，整篇扫描会抓错。
    section = text.split("## 代理段敏感性", 1)[1]
    rows = [ln for ln in section.splitlines() if ln.startswith("| W ")]
    assert rows and "-" in rows[0], f"W 那行要给出翻向的位移：{rows}"
    # 列序：维度｜主段天数｜主段 IC｜主段 t｜代理段天数｜代理段 IC｜代理段 t｜位移｜差的 2×SE｜z
    wcells = [c.strip() for c in rows[0].strip("|").split("|")]
    assert len(wcells) == 10, f"敏感性表该有 10 列：{rows[0]}"
    assert wcells[7].startswith("-") and wcells[9].startswith("-"), \
        f"位移与 z 都该是负的（0.10 → −0.10）：{rows[0]}"
    assert not wcells[8].startswith("-"), f"差的 2×SE 是尺度，不该带符号：{rows[0]}"
    drow = next(ln for ln in section.splitlines() if ln.startswith("| D "))
    assert "—" in drow, f"给不出 IC 的那条要印「—」而不是 0：{drow}"
    assert "nan" not in section, f"报告里不该出现 nan 文本（判不出的格子统一印「—」）：{drow}"


def test_proxy_shift_refuses_overlapping_segments():
    """两段有同日就必须拒：`se_diff = hypot(pre, post)` 的前提是两段的日互不重叠。

    重叠时两条估计量共用同一批交易日，差的方差不是两边之和——把它当独立合成会**低估**
    差的 SE，于是把噪声读成位移。这里宁可退出，让人把窗口切干净。
    """
    mod = _panel_module()
    pre, post = _two_segments()
    shared = pre.index[0]
    post2 = pd.concat([post, pre.iloc[[0]].rename(index={shared: shared})])
    with pytest.raises(ValueError, match="重叠"):
        mod.proxy_shift(pre, post2, ["S", "W", "D"], maxlags=5)


def test_panel_window_follows_the_panel_not_the_cli_defaults():
    """复用路径的时窗必须由面板自己决定：否则这一轮的产物会盖掉另一轮的。

    2026-11-04 实踩：`--from-panel` 指代理段面板、命令行没给 `--start/--end`，
    `tag` 取的是默认值 2024-01-01_2026-09-15 ⇒ 主面板那 656 天的 `daily_ic` /
    `ic_summary` / `report` / `run_meta` 四份被 24 天的内容原地覆盖。面板文件本身没事，
    但所有下游结论的落盘物都没了——而这种覆盖**一个字的报错都不会有**。
    """
    mod = _panel_module()
    dates = pd.Series(["2026-09-18", "2026-09-16", "2026-10-27", "2026-10-20"])
    lo, hi, changed = mod.panel_window(dates, start="2024-01-01", end="2026-09-15")
    assert (lo, hi) == ("2026-09-16", "2026-10-27"), f"时窗要按面板取，实得 {(lo, hi)}"
    assert changed, "面板与命令行不一致却没报出来，就是静默覆盖"
    # 面板正好落在命令行说的窗口上：原样返回，不许改成面板的首末行（那是同一轮的 tag）
    same = pd.Series(["2024-01-01", "2024-01-05", "2026-09-15"])
    assert mod.panel_window(same, start="2024-01-01", end="2026-09-15") == (
        "2024-01-01", "2026-09-15", False)


def test_reuse_path_derives_its_window_from_the_panel():
    """接线判据：`run()` 的复用分支必须真的调用 `panel_window`。

    只测函数不测接线，等于允许下一版把这一行删掉——覆盖事故就回来了（`KI-SILENT-003`）。
    """
    mod = _panel_module()
    src = inspect.getsource(mod.run)
    reuse = src[src.index("if args.from_panel:"):]
    assert "panel_window(" in reuse[:1200], "复用分支没按时窗派生 tag：会覆盖另一轮的产物"
