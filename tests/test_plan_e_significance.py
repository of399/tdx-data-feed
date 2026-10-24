"""方案 E 报告里两处统计推断的口径修正（2026-10-24）。

缺陷 1 — Deflated Sharpe 量纲：`deflated_sharpe_ratio` 的方差是 `V = (1 − γ₃·SR + …)/T`，
T 是**观测期数**（这里是 2397 个交易日），所以 SR 必须是**每期**夏普。而两个调用点
（plan_e_backtest.summarize / multifactor.engine）传的都是 `mu/sd·√252` 的**年化**夏普，
z 被放大 √252≈15.9 倍。实测本次 110 笔样本：年化 0.081 → p=0.010（"显著"），
换成每期 0.00509 → p=1.000（不显著）。同一份数据的 Newey-West 日收益 p=0.822 站在后者一边。

缺陷 2 — 置换检验被当成择时 alpha：`permutation_return_test` 默认统计量是滞后-1 自相关
（均值/夏普对重排不变，会退化，所以选它是对的），但报告行写成「策略 vs 随机时序」，
风险提示第 6 条写「判策略是否优于随机时序」。资金曲线是把一笔持仓的收益摊在 hold 个
连续交易日上算出来的，重叠持仓天然产生正自相关——观测 0.137 vs 打乱后 ≈0，
p=0.000 只证明曲线被平滑过，不证明 alpha。
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
