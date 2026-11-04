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
