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
from tdxfeed.alpha158.ic import (
    IcConfig,
    daily_ic,
    independent_subset,
    summarize_ic,
)

NEEDS_DATA = False

RNG = np.random.default_rng(20261104)


def _panel(dates: list[str], codes: list[str], **cols) -> pd.DataFrame:
    rows = []
    for d in dates:
        for c in codes:
            rows.append({"date": d, "code": c, **{k: v(d, c) for k, v in cols.items()}})
    return pd.DataFrame(rows)


def test_constant_input_is_nan_without_a_warning():
    """一侧是常数（未覆盖的维、停牌日集合）就是没有秩相关：给 NaN，且不许刷 warning。

    pandas 对常数列做 spearman 会抛 ConstantInputWarning 再给 NaN。数值本来就对，
    但 158 维 × 660 天的循环里这条警告会以十万计刷屏——真异常看不见了。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")            # 任何警告都变成失败
        assert np.isnan(cv_utils.rank_ic([1.0] * 20, list(range(20))))
        assert np.isnan(cv_utils.rank_ic(list(range(20)), [3.0] * 20))


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


# ------------------------------------------------------------------ 去重：独立信息计数 #
def _basis(n: int = 400, k: int = 4, seed: int = 7) -> np.ndarray:
    """一组标准正交的 IC 序列基底。

    为什么要费劲造正交基：这组用例要的是**解析相关系数**（0.85 就是 0.85），
    随机序列的两两相关只有近似值，阈值判据会随种子漂。加常数不改变协方差，
    所以每条序列的均值可以单独设、与两两相关互不干扰——这正是「效应量序」与
    「冗余序」两个旋钮必须分开的地方。
    """
    rng = np.random.default_rng(seed)
    q, r = np.linalg.qr(rng.standard_normal((n, k)))
    return q * np.sign(np.diag(r))


def _daily(spec: dict[str, tuple[float, np.ndarray]]) -> pd.DataFrame:
    days = len(next(iter(spec.values()))[1])
    idx = [f"d{i:03d}" for i in range(days)]
    return pd.DataFrame({name: m + v for name, (m, v) in spec.items()}, index=idx)


def test_dedup_collapses_twins_and_keeps_independent_signals():
    """完全反号的两维是**同一个信号的两种写法**，必须合成一条。

    只看正相关的去重会把 `x` 与 `-x` 当成两条独立信息，而模型里它们是同一个自由度
    （符号由系数吸收）。所以判据取 |corr|。
    """
    u = _basis()
    daily = _daily({"sig": (0.20, u[:, 0]),
                    "twin": (0.05, -u[:, 0]),
                    "indep": (0.10, u[:, 1])})
    out = independent_subset(daily, ["sig", "twin", "indep"], threshold=0.8)
    assert out["kept"] == ["sig", "indep"], f"实得 {out}"
    assert out["dropped"]["twin"]["by"] == "sig"
    assert out["dropped"]["twin"]["abs_corr"] == pytest.approx(1.0, abs=1e-9)


def test_dedup_representative_is_the_strongest_dim_not_the_first_name():
    """留谁必须有规则：同簇里 |mean_IC| 最大的那条。按名字序会留下弱的那条并把强的丢掉。"""
    u = _basis()
    daily = _daily({"AAA": (0.05, -u[:, 0]), "BBB": (0.20, u[:, 0])})
    assert daily["AAA"].abs().mean() < daily["BBB"].abs().mean()
    assert sorted(daily.columns)[0] == "AAA", "构造没生效：名字序与效应量序必须相反"
    out = independent_subset(daily, ["AAA", "BBB"], threshold=0.8)
    assert out["kept"] == ["BBB"], f"实得 {out}"
    assert out["dropped"]["AAA"]["by"] == "BBB"


def test_dedup_counts_are_monotone_in_the_threshold():
    """阈值越高留得越多；三档并报，是为了让「独立维数」不被读成一个阈值 artifact。"""
    u = _basis()
    a, b = u[:, 0], 0.85 * u[:, 0] + 0.5267 * u[:, 1]
    c = 0.85 * b + 0.527 * u[:, 2]          # corr(a,c)=0.72、corr(b,c)=0.85（解析值）
    daily = _daily({"A": (0.20, a), "B": (0.15, b), "C": (0.10, c)})
    assert daily["A"].corr(daily["B"]) == pytest.approx(0.85, abs=0.01)
    assert daily["A"].corr(daily["C"]) == pytest.approx(0.72, abs=0.01)

    counts = {thr: len(independent_subset(daily, ["A", "B", "C"],
                                          threshold=thr)["kept"])
              for thr in (0.7, 0.8, 0.9)}
    assert counts == {0.7: 1, 0.8: 2, 0.9: 3}, f"实得 {counts}"
    assert counts[0.7] <= counts[0.8] <= counts[0.9]


def test_dedup_with_seed_drops_dims_the_other_set_already_covers():
    """跨集合去重才是「切特征集值不值」的判据：现役已有的信号，借来的替身不算新增。"""
    u = _basis()
    daily = _daily({"S": (0.30, u[:, 0]),
                    "dup_of_S": (0.20, 0.85 * u[:, 0] + 0.5267 * u[:, 1]),
                    "fresh": (0.15, u[:, 2])})
    out = independent_subset(daily, ["dup_of_S", "fresh"], seed=["S"], threshold=0.8)
    assert out["kept"] == ["fresh"], f"实得 {out}"
    assert out["dropped"]["dup_of_S"]["by"] == "S"
    assert out["seed"] == ["S"]


def test_dedup_reports_dims_that_never_gave_an_ic_separately():
    """整列无 IC 的维既不该被当成独立信息，也不该出现在 dropped 里——它是「没验」。"""
    u = _basis()
    daily = _daily({"sig": (0.20, u[:, 0]), "dead": (0.0, u[:, 1])})
    daily["dead"] = np.nan
    thin = daily["sig"].copy()
    thin.iloc[2:] = np.nan                     # 只有 2 个有效日：< min_days
    daily["thin"] = thin
    out = independent_subset(daily, ["sig", "dead", "thin"], threshold=0.8)
    assert out["kept"] == ["sig"]
    assert sorted(out["no_ic"]) == ["dead", "thin"], f"实得 {out['no_ic']}"
    assert "dead" not in out["dropped"] and "thin" not in out["dropped"]


def test_dedup_rejects_an_impossible_threshold():
    """threshold=1.0 意思是「永不合并」，等于关掉去重——这种数只能显式报错。"""
    u = _basis()
    daily = _daily({"a": (0.2, u[:, 0]), "b": (0.1, u[:, 1])})
    for bad in (0.0, 1.0, 1.5):
        with pytest.raises(ValueError):
            independent_subset(daily, ["a", "b"], threshold=bad)


def test_dedup_does_not_invent_a_relation_it_cannot_measure():
    """两条维没有共同有效日 ⇒ 相关根本判不了。不合并，但必须**记账**。

    静默保留会让「独立维数」把「没法比」也算成独立；`uncomparable` 计数就是为了让
    这个数不冒充证据。真实面板里所有维共用同一批交易日，这一项应当是 0。
    """
    u = _basis(400, 2)
    daily = _daily({"a": (0.2, u[:, 0]), "b": (0.2, u[:, 0])})
    daily.loc[daily.index[:200], "a"] = np.nan
    daily.loc[daily.index[200:], "b"] = np.nan
    out = independent_subset(daily, ["a", "b"], threshold=0.8)
    assert sorted(out["kept"]) == ["a", "b"]
    assert out["uncomparable"] == 1, f"实得 {out}"
