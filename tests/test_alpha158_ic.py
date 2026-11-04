"""日频横截面 Rank IC 的汇总核（步骤③：158 维 vs FEAT26 的对照判据）。

放在这里的原因：IC 的**算法**不是本次要新造的东西——Spearman 逐日横截面、ICIR、
Newey-West HAC、BH-FDR 都已经在 `quant/multifactor/cv_utils.py` 里实现并检验过。
本模块只负责把它们按 (date × feature) 这张宽表串起来，所以这里测的是**接线**：
哪个函数在什么参数下被调用、哪些天被丢弃、哪些维度算不出显著性。

三条不能靠直觉的口径（写错不会报错，只会静默给出可看的假数字）：
① IC 必须逐日算——把整张面板堆一起做 pooled Spearman，会被日期效应污染；
② 横截面不足的天数是**丢弃**，不是补 0：补 0 会把「没数据」伪装成「无信号」，
   直接压低 IC 均值并虚增 ICIR 的分母；
③ 远期收益标签按天重叠 ⇒ IC 序列天然自相关 ⇒ 裸 t 会低估标准误，必须走 HAC。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.multifactor import cv_utils
from tdxfeed.alpha158.ic import IcConfig, daily_ic, summarize_ic

NEEDS_DATA = False

RNG = np.random.default_rng(20261104)


def _panel(dates: list[str], codes: list[str], **cols) -> pd.DataFrame:
    rows = []
    for d in dates:
        for c in codes:
            rows.append({"date": d, "code": c, **{k: v(d, c) for k, v in cols.items()}})
    return pd.DataFrame(rows)


def test_daily_ic_is_cross_sectional_not_pooled():
    """当日内 f 与标签严格同序 ⇒ 每日 IC=1；但每天都有一个远大于截面间距的整体冲击
    ⇒ 把整张面板堆一起做 pooled Spearman，冲击成了主导方差，相关被冲成 ~0。

    这条就是「逐日横截面」与「整表堆一起」的判据差：写成 pooled 会给出 ~0，
    于是 158 维全部被判成无信号——形状完全正常，数字全错。
    """
    codes = [f"{i:06d}" for i in range(40)]
    # 冲击的间距远大于截面内部间距（0~39），且天与天之间正负交替：日内的排序信息
    # 全留着（逐日 IC=1），堆到一起后被日期效应冲掉（pooled ~0.1）。
    days = [f"2026-01-{d:02d}" for d in range(2, 14)]
    shocks = {d: (-1.0) ** k * (k + 1) * 100.0 for k, d in enumerate(days)}

    panel = _panel(
        days, codes,
        f=lambda d, c: float(int(c)),
        label=lambda d, c: int(c) + shocks[d],
    )
    daily = daily_ic(panel, ["f"], IcConfig(label_col="label"))
    assert list(daily.columns) == ["f"]
    assert daily["f"].notna().sum() == 12, f"十二天都该有 IC：{daily}"
    assert np.allclose(daily["f"].dropna().to_numpy(), 1.0), (
        f"逐日 IC 应为 +1，实得 {daily['f'].to_list()}"
    )
    pooled = panel["f"].corr(panel["label"], method="spearman")
    assert abs(pooled) < 0.2, f"对照组失真说明构造没生效：pooled={pooled:.3f}"


def test_thin_cross_sections_are_dropped_not_zeroed():
    """不足 min_codes 的天不产生 IC 行；n_days 也不能把它算进去。"""
    codes = [f"{i:06d}" for i in range(40)]
    panel = _panel(
        ["2026-01-05", "2026-01-06"], codes,
        f=lambda d, c: int(c) + RNG.normal(0, 0.01),
        label=lambda d, c: int(c) + RNG.normal(0, 0.01),
    )
    thin = panel[panel["code"].isin(codes[:10]) & (panel["date"] == "2026-01-06")]
    panel = pd.concat([panel[panel["date"] == "2026-01-05"], thin], ignore_index=True)

    daily = daily_ic(panel, ["f"], IcConfig(label_col="label", min_codes=30))
    assert "2026-01-06" not in set(str(x) for x in daily.index), "10 只的横截面不该产出 IC"
    summary = summarize_ic(daily, IcConfig(label_col="label", min_codes=30))
    assert int(summary.loc["f", "n_days"]) == 1, (
        f"n_days 把丢弃的天算进去了：{summary.loc['f'].to_dict()}"
    )


def test_all_nan_feature_yields_no_days_not_a_crash():
    """整列缺失（预热段/未覆盖）是给不出结论，不是 0：必须 n_days=0 且 p 为 NaN。"""
    panel = _panel(
        ["2026-01-05", "2026-01-06"], [f"{i:06d}" for i in range(40)],
        f=lambda d, c: np.nan,
        label=lambda d, c: float(int(c)),
    )
    daily = daily_ic(panel, ["f"], IcConfig(label_col="label", min_codes=30))
    assert daily["f"].notna().sum() == 0
    summary = summarize_ic(daily, IcConfig(label_col="label", min_codes=30))
    assert int(summary.loc["f", "n_days"]) == 0
    assert np.isnan(float(summary.loc["f", "nw_p"])), "样本不足不得给出显著性"


def test_summary_delegates_to_the_existing_multifactor_helpers(monkeypatch):
    """ICIR / Newey-West / BH-FDR 都不许在本模块另写一份：改 cv_utils 要能穿透。"""
    dates = [f"2026-01-{d:02d}" for d in range(2, 22)]
    panel = _panel(
        dates, [f"{i:06d}" for i in range(60)],
        f=lambda d, c: (int(c) % 7) + RNG.normal(0, 1),
        label=lambda d, c: (int(c) % 7) + RNG.normal(0, 1),
        noise=lambda d, c: RNG.normal(0, 1),
    )
    daily = daily_ic(panel, ["f", "noise"], IcConfig(label_col="label", min_codes=30))

    seen: dict[str, object] = {}
    real_icir = cv_utils.icir
    real_nw = cv_utils.ic_newey_west
    real_bh = cv_utils.bh_fdr_correct

    def spy_icir(s):
        seen["icir"] = len(s)
        return real_icir(s)

    def spy_nw(s, maxlags=5):
        seen["nw"] = maxlags
        return real_nw(s, maxlags=maxlags)

    def spy_bh(pvals, alpha=0.05):
        seen["bh_n"] = len(pvals)
        seen["bh_alpha"] = alpha
        return real_bh(pvals, alpha=alpha)

    monkeypatch.setattr(cv_utils, "icir", spy_icir)
    monkeypatch.setattr(cv_utils, "ic_newey_west", spy_nw)
    monkeypatch.setattr(cv_utils, "bh_fdr_correct", spy_bh)

    cfg = IcConfig(label_col="label", min_codes=30, maxlags=4, alpha=0.10)
    summary = summarize_ic(daily, cfg)

    assert "icir" in seen, "ICIR 没走 cv_utils.icir（要么另写了一份，要么没算）"
    assert seen["nw"] == 4, f"HAC 滞后没把 cfg.maxlags 传下去：{seen['nw']}"
    assert seen["bh_n"] == 2, f"BH-FDR 必须跨全部维度做：{seen['bh_n']}"
    assert seen["bh_alpha"] == 0.10
    assert {"icir", "nw_t", "nw_p", "q_value", "reject"} <= set(summary.columns)


def test_rank_ic_promoted_to_public_helper():
    """cv_utils.rank_ic 是公共件；engine._rank_ic 只能是它的转发，不是第二份实现。"""
    ic = cv_utils.rank_ic([1, 2, 3, 4, 5], [1, 3, 2, 5, 4])
    assert ic == pytest.approx(0.8), f"秩相关手算应为 0.8（d²=4），实得 {ic}"
    # 有效对不足 5 对给 NaN，而不是抛或给 0：调用方靠 NaN 判「这天不算」
    assert np.isnan(cv_utils.rank_ic([1, 2], [1, 2]))

    from quant.multifactor import engine

    calls = []
    real = cv_utils.rank_ic

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cv_utils, "rank_ic", spy)
    try:
        assert engine._rank_ic([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    finally:
        monkey.undo()
    assert calls, "engine._rank_ic 没有委托给 cv_utils.rank_ic——那是第二份实现"
