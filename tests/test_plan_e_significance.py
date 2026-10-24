"""方案 E 报告里两处统计推断的口径修正（2026-10-24）。

缺陷 1 — Deflated Sharpe 量纲：`deflated_sharpe_ratio` 的方差是 `V = (1 − γ₃·SR + …)/T`，
T 是**观测期数**（这里是 2397 个交易日），所以 SR 必须是**每期**夏普。而两个调用点
（plan_e_backtest.summarize / multifactor.engine）传的都是 `mu/sd·√252` 的**年化**夏普，
z 被放大 √252≈15.9 倍。实测本次 110 笔样本：年化 0.081 → p=0.010（"显著"），
换成每期 0.00509 → p=1.000（不显著）。同一份数据的 Newey-West 日收益 p=0.822 站在后者一边。

缺陷 2 — 置换检验被当成择时 alpha：`permutation_return_test` 默认统计量是滞后-1 自相关
（均值/夏普对重排不变，会退化，所以选它是对的），但资金曲线是把一笔持仓的收益摊在 hold 个
连续交易日上算出来的，重叠持仓天然产生正自相关——观测 0.137 vs 打乱后 ≈0，p=0.000 只证明
曲线被平滑过，不证明 alpha。plan_e 侧把它写成「策略 vs 随机时序」；同一处误读在
`multifactor.engine` 风险提示第 9 条里写成「置换 p<0.05 ⇒ 日盈亏顺序显著异于随机时序…
代表有顺序信息」，和它自己表格里的「判时序结构而非择时 alpha」自相矛盾，一并改掉。

缺陷 3 — 胜率二项检验的原假设选错了：报告里 `胜率 H₀=50% (精确二项 p) 0.001 对随机基准`
是 112 笔样本上唯一「显著」的数字，而它测的是赔付几何，不是本事。TP +30%/SL −10% 的阶梯
把打平胜率压到 −avg_loss/(avg_win−avg_loss)=31.2%，零边际策略的胜率必然远低于 50%，
于是「显著偏离 50%」几乎是这条规则的同义反复。真正该问的是胜率有没有高过 31.2%：实测
33.9% vs 31.2% 单侧 p=0.296，和另外三个收益检验一致——什么都拒绝不了。反过来，50% 这个
基准还会漏掉真边际：胜率恰好 50%（对 2.21 盈亏比是暴利）时旧检验的 p≈1.0，判「不显著」。
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_pe():
    spec = importlib.util.spec_from_file_location(
        "plan_e_backtest_under_test", ROOT / "scripts" / "plan_e_backtest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pe = _load_pe()


def _daily_series(n_days: int, ann_sharpe: float, seed: int = 7) -> np.ndarray:
    """造一条指定年化夏普的日收益序列（sd 固定，均值由夏普反推）。"""
    rng = np.random.default_rng(seed)
    sd = 0.00357
    x = rng.normal(0.0, sd, size=n_days)
    x = (x - x.mean()) / x.std() * sd          # 精确零均值、指定波动
    return x + ann_sharpe * sd / np.sqrt(252)  # 年化夏普 ⇒ 每期均值


def _fake_bt_and_records(daily: np.ndarray):
    """每天恰好 1 笔在持仓 ⇒ 组合权重恒为 0.10，daily_full == daily。"""
    dates = pd.date_range("2020-01-01", periods=len(daily), freq="D")
    per_trade = daily / 0.10
    bt = pd.DataFrame({
        "code": [f"sh6000{i % 10:02d}" for i in range(len(daily))],
        "signal_date": [d.strftime("%Y-%m-%d") for d in dates],
        "t1_pct": 0.10,
        "t1_vol_ratio": 2.0,
        "return_pct": per_trade,
        "exit_reason": np.where(per_trade > 0, "TP", "SL"),
    })
    daily_records = [[(d, r)] for d, r in zip(dates, per_trade, strict=True)]
    return bt, daily_records


def _dsr_row_pvalue(md: str) -> float:
    m = re.search(r"Deflated Sharpe p\)\*\* \| ([\d.]+)", md)
    assert m, "报告里没有 Deflated Sharpe 行"
    return float(m.group(1))


# ---------- 缺陷 1：年化 / 每期量纲 ----------

def test_dsr_helper_converts_annualized_sharpe_before_the_formula():
    """年化 0.081 的弱边际不该被判显著；直接喂进公式会得到 0.010 的假阳性。"""
    daily = _daily_series(2397, 0.081)
    ann = float(daily.mean() / daily.std() * np.sqrt(252))
    p_fixed = pe.dsr_p_from_annual_sharpe(ann, daily)
    p_trap = pe.deflated_sharpe_ratio(
        ann, len(daily), float(pd.Series(daily).skew()), float(pd.Series(daily).kurt()),
        pe.DSR_N_TRIALS)
    assert p_fixed >= 0.05, f"弱边际被判显著了: {p_fixed}"
    assert p_trap < 0.05, "旧算法本身已不再是假阳性，测试前提失效"


def test_dsr_helper_still_detects_a_genuine_edge():
    """修量纲不是把检验阉掉：年化 2.0 的曲线仍应显著。"""
    daily = _daily_series(2397, 2.0)
    ann = float(daily.mean() / daily.std() * np.sqrt(252))
    assert pe.dsr_p_from_annual_sharpe(ann, daily) < 0.05


def test_dsr_helper_on_too_short_a_series_is_nan():
    assert np.isnan(pe.dsr_p_from_annual_sharpe(1.0, np.array([0.01, -0.02])))


def test_report_dsr_row_matches_the_helper():
    bt, daily_records = _fake_bt_and_records(_daily_series(2397, 0.081))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    p = _dsr_row_pvalue(md)
    assert p >= 0.05, f"报告把年化夏普当每期喂进方差公式 ⇒ 假阳性 p={p}"


# ---------- 缺陷 2：置换检验的命名 ----------

def test_report_permutation_row_names_the_statistic_it_actually_tests():
    bt, daily_records = _fake_bt_and_records(_daily_series(1500, 0.4))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert "策略 vs 随机时序" not in md, "自相关检验被冒充成择时 alpha"
    row = [ln for ln in md.splitlines() if "置换检验 p" in ln]
    assert row and "滞后-1 自相关" in row[0], row
    assert "重叠持仓" in md, "报告没说明重叠持仓会天然造出自相关"


def test_risk_note_drops_the_timing_alpha_claim():
    bt, daily_records = _fake_bt_and_records(_daily_series(1500, 0.4))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert "判策略是否优于随机时序" not in md


def test_multifactor_risk_note_drops_the_timing_alpha_claim():
    """engine.py 风险提示第 9 条同样把置换检验读成 alpha，和多因子表格里那行自相矛盾。"""
    from types import SimpleNamespace

    from quant.multifactor import engine
    cfg = SimpleNamespace(cov_method="lw", vol_target=0.0, cvar_alpha=0.95)
    valp = SimpleNamespace(mode="proxy")
    md = engine._build_report(
        {"sharpe": 0.42, "dsr_p": 1.0, "perm_p_two": 0.272, "perm_p_one": 0.136},
        [], [], {}, cfg, valp, None)
    assert "日盈亏顺序显著异于随机时序" not in md, "自相关检验被读成有顺序信息 ⇒ 择时 alpha"
    assert "滞后-1 自相关" in md, "风险提示没说这个 p 值实际测的是什么"
    assert "重叠持仓" in md, "风险提示没说明重叠持仓会天然造出自相关"


# ---------- 两个调用点共用同一换算 ----------

def test_multifactor_engine_shares_the_conversion_helper():
    """engine.py 的 metrics['sharpe'] 同样是年化值，必须走同一个换算入口。"""
    from quant.multifactor import engine
    src = inspect.getsource(engine.run_mf_backtest)
    assert "dsr_p_from_annual_sharpe" in src, "多因子侧还在直接喂年化夏普"

    s = pd.Series(_daily_series(600, 1.0))
    dates = pd.date_range("2024-01-01", periods=len(s), freq="D")
    m = engine._portfolio_metrics(list(zip(dates.astype(str), s, strict=True)))
    per_period = float(s.mean() / s.std())
    assert abs(m["sharpe"] / per_period - np.sqrt(252)) < 1e-6, "年化假设变了，换算入口要跟着改"


# ---------- 缺陷 3：胜率检验的 50% 原假设 ----------

AVG_WIN = 0.2903      # 本次 112 笔样本的实测平均盈利
AVG_LOSS = -0.1315    # 实测平均亏损 ⇒ 赔率隐含打平胜率 31.2%


def _bt_with_returns(returns: list[float]):
    """每天恰好 1 笔成交的构造样本，胜率/赔付由 returns 直接决定。"""
    dates = pd.date_range("2024-01-02", periods=len(returns), freq="B")
    bt = pd.DataFrame({
        "code": [f"sh6000{i % 10:02d}" for i in range(len(returns))],
        "signal_date": [d.strftime("%Y-%m-%d") for d in dates],
        "t1_pct": 0.10,
        "t1_vol_ratio": 2.0,
        "return_pct": list(returns),
        "exit_reason": ["TP" if r > 0 else "SL" for r in returns],
    })
    daily_records = [[(d, r)] for d, r in zip(dates, returns, strict=True)]
    return bt, daily_records


def _ladder(n_wins: int, n_losses: int) -> list[float]:
    return [AVG_WIN] * n_wins + [AVG_LOSS] * n_losses


def _wr_row(md: str) -> str:
    row = [ln for ln in md.splitlines() if "精确二项" in ln]
    assert row, "报告里没有胜率显著性行"
    return row[0]


def _wr_row_pvalue(md: str) -> float:
    m = re.search(r"精确二项[^|]*\|\s*([\d.]+)", _wr_row(md))
    assert m, _wr_row(md)
    return float(m.group(1))


def test_breakeven_win_rate_helper():
    """打平胜率由赔付阶梯决定：平均盈 29.03% / 平均亏 −13.15% ⇒ 31.2%。"""
    assert abs(pe.breakeven_win_rate(AVG_WIN, AVG_LOSS) - 0.31174) < 1e-4
    assert np.isnan(pe.breakeven_win_rate(0.0, AVG_LOSS)), "没有盈利笔时赔付阶梯不存在"


def test_report_win_rate_row_drops_the_50pct_null():
    """胜率刚好压在赔率打平线上（31.4% vs 31.2%）⇒ 没有边际，旧检验会给 p=0.000 的假阳性。"""
    bt, daily_records = _bt_with_returns(_ladder(32, 70))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert "H₀=50%" not in md, "还在拿 50% 当胜率的原假设"
    p = _wr_row_pvalue(md)
    assert p >= 0.05, f"零边际样本被胜率检验判显著了: p={p}"


def test_win_rate_row_still_detects_the_edge_the_50pct_null_misses():
    """胜率 50% 对 2.21 的盈亏比是暴利，旧基准反而判『不显著』——修正后必须抓到。"""
    from scipy.stats import binomtest

    bt, daily_records = _bt_with_returns(_ladder(51, 51))
    old_p = float(binomtest(51, 102, 0.5).pvalue)
    assert old_p > 0.05, "旧检验已不再是瞎的，测试前提失效"

    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert _wr_row_pvalue(md) < 0.05, "真边际被漏掉了"


def test_win_rate_row_states_which_null_it_tested():
    bt, daily_records = _bt_with_returns(_ladder(32, 70))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert "盈亏平衡" in _wr_row(md), "报告没写这条 p 值比的是哪个基准"


def test_risk_note_stops_blaming_beijing_exchange():
    """北交所 2026-10-21 起已下线，本次 112 笔 100% 沪深，风险提示不能再讲 920XXX。"""
    bt, daily_records = _bt_with_returns(_ladder(32, 70))
    md = pe.summarize(bt, daily_records, 10, 0.30, -0.10, "v2", fee=0.0003, slippage=0.001)
    assert "北交所（920XXX）" not in md, "风险提示还在为一个不在标的池里的交易所预警"
    assert "单季" in md, "风险提示没交代信号在时间上的集中度"
