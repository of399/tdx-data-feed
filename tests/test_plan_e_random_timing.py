"""择时基线：同一批股票、随机入场日，检验"在这一天买"到底有没有信息。

停用声明（`plan_e_journal.PLAN_E_LIVE_RETIREMENT`）里唯一还没实现的检验就是这条。
已有的四个检验回答的是"这 112 笔的收益是否显著"，没有回答"入场日是挑的还是蒙的"——
把 signal_date 换成随机日、其余规则（TP/SL/hold、SL 优先、跳空按开盘）一字不改地重跑，
才第一次把"择时"本身放进原假设里。

三个必须锁住的口径：
1. 随机入场日的出场必须走 `backtest_one` **同一段**结算代码。两套出场逻辑一旦分叉，
   基线和实样本就不是同一个策略的两个版本，p 值没有意义（所以先有等价测试）。
2. 抽样只动入场日，不动标的池：每笔仍用它自己那只股票的行情，候选日限于真实样本的
   entry_date 跨度内、且 forward 窗口够走完出场规则的日子。否则基线顺带把"选哪些票"
   也检验了，而那不是本检验的问题。
3. 随机种子必须可复现，同种子两次跑出同一组统计量。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_backtest():
    spec = importlib.util.spec_from_file_location(
        "plan_e_backtest_random_timing", ROOT / "scripts" / "plan_e_backtest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pe = _load_backtest()

PARAMS = {"hold_days": 10, "tp": 0.30, "sl": -0.10, "fee": 0.0, "slippage": 0.0}
CODE_A = "600000"
CODE_B = "600001"


def _seed(code: str, start: str, closes: list[float], *, spread: float = 0.0) -> None:
    """按收盘价造一条等差序列；high/low 用 spread 撑开，便于构造 TP/SL 命中。"""
    n = len(closes)
    df = pd.DataFrame({
        "date": pd.date_range(start, periods=n, freq="D"),
        "open": closes,
        "high": [c * (1 + spread) for c in closes],
        "low": [c * (1 - spread) for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })
    pe._PARQUET_CACHE[code] = df


@pytest.fixture(autouse=True)
def _clear_cache():
    pe._PARQUET_CACHE.clear()
    yield
    pe._PARQUET_CACHE.clear()


def _seed_two_codes():
    # 两支走势不同的股票：A 一路上行，B 先涨后崩，保证两者随机入场日的结果不会相同
    _seed(CODE_A, "2026-01-01", [10 * (1.02 ** i) for i in range(20)])
    _seed(CODE_B, "2026-01-01", [10, 11, 12, 13, 14, 13, 11, 9, 7, 6]
          + [6 * 0.97 ** i for i in range(10)], spread=0.05)


def test_backtest_at_entry_shares_the_exit_ladder_with_backtest_one():
    """随机基线用的结算必须和主回测是同一段代码：同一天入场，逐字段相等。"""
    # T=01-01，T+1=01-02（+10% 过涨幅过滤、量比 1.0 需关掉过滤），T+2=01-03 入场
    _seed(CODE_A, "2026-01-01", [10, 11, 10.5, 10.4, 10.3, 13.6, 13.7, 13.8, 13.9, 14.0],
          spread=0.04)
    run = {**PARAMS, "min_t_pct": 0.0, "min_t_vol_ratio": 0.0}
    via_signal = pe.backtest_one(CODE_A, "2026-01-01", **run)
    assert via_signal is not None and not via_signal["filtered"]

    via_entry = pe.backtest_at_entry(CODE_A, via_signal["entry_date"], **PARAMS)

    for key in ("entry_date", "exit_date", "exit_price", "return_pct",
                "return_pct_gross", "exit_reason", "hold", "daily"):
        assert via_entry[key] == via_signal[key], f"{key} 两处口径不一致"


def test_backtest_at_entry_returns_none_without_forward_window():
    """最后一根 bar 上没有出场窗口：直接判 None，不能让基线拿到半截持仓当成一笔。"""
    _seed(CODE_A, "2026-01-01", [10] * 8)
    assert pe.backtest_at_entry(CODE_A, "2026-01-08", **PARAMS) is None


def _real_bt():
    """两笔真实交易：各自的 entry_date 落在跨度两端，中间留出可随机抽的日子。"""
    _seed_two_codes()
    return pd.DataFrame([
        {"code": CODE_A, "signal_date": "2026-01-02", "entry_date": "2026-01-04",
         "return_pct": 0.05},
        {"code": CODE_B, "signal_date": "2026-01-04", "entry_date": "2026-01-06",
         "return_pct": -0.02},
    ])


def test_baseline_keeps_the_universe_and_samples_only_entry_days(monkeypatch):
    """抽样只动入场日：每笔仍用它自己那只股票，且日期在真实样本跨度内。"""
    bt = _real_bt()
    seen: list[tuple[str, str]] = []
    real = pe.backtest_at_entry

    def spy(code, entry_date, **kw):
        seen.append((code, entry_date))
        return real(code, entry_date, **kw)

    monkeypatch.setattr(pe, "backtest_at_entry", spy)
    pe.random_timing_baseline(bt, n_trials=3, seed=1, **PARAMS)

    assert len(seen) == 2 * 3, f"每笔每次都要结算一次，实际 {len(seen)} 次调用"
    for code, day in seen:
        assert code in (CODE_A, CODE_B), f"基线换了标的池：{code}"
        assert "2026-01-04" <= day <= "2026-01-06", f"入场日跑出真实跨度：{code} {day}"


def test_baseline_is_reproducible_and_reports_a_one_sided_p():
    bt = _real_bt()
    a = pe.random_timing_baseline(bt, n_trials=5, seed=7, **PARAMS)
    b = pe.random_timing_baseline(bt, n_trials=5, seed=7, **PARAMS)

    assert a["null_mean"] == b["null_mean"], "同种子不可复现，报告里的 p 值没法复核"
    assert a["p_mean"] == b["p_mean"]
    for key in ("p_mean", "p_sharpe", "p_win"):
        assert 0.0 <= a[key] <= 1.0, f"{key} 不是概率：{a[key]}"
    assert a["n_trials"] == 5 and a["n_trades"] == 2
    assert len(a["null_sharpe"]) == 5


def test_baseline_flags_trades_it_could_not_settle():
    """行情太短、跨度内挑不出有完整出场窗口的日子 ⇒ 必须数出来，不能悄悄少算。"""
    bt = _real_bt()
    _seed(CODE_B, "2026-01-01", [10] * 5)   # 只有 5 根 bar，跨度内没有一天走得完出场规则
    out = pe.random_timing_baseline(bt, n_trials=4, seed=3, **PARAMS)
    assert out["n_skipped"] == 1, "抽不出入场日的笔被静默丢弃"
    assert out["n_trades"] == 1


def test_report_section_names_the_stat_and_the_universe_caveat(monkeypatch):
    """读报告的人必须能看出这个 p 值检验的是什么、不检验什么。"""
    bt = _real_bt()
    md = pe.run_random_timing(bt, n_trials=5, seed=11, **PARAMS)
    assert "随机择时" in md
    assert "标的池不变" in md, "没交代这只检验入场日、不检验选股"
    assert "逐笔" in md and "日频" in md, "per-trade Sharpe 与日频 Sharpe 不能混为一谈"
    assert "p=" in md


def test_section_marks_the_p_as_conditional_and_not_generalizable():
    """固定标的池的随机化检验只回答"这批票上挑哪天买有没有区别"。

    它的标准误来自同一只票的日际波动（比跨标的的标准误小得多），所以 p 小不等于
    "这套信号换到新票上还能赚"——那要跨标的抽样，是 DSR/bootstrap 那些检验的问题。
    """
    md = pe.run_random_timing(_real_bt(), n_trials=5, seed=11, **PARAMS)
    assert "不能外推" in md, "把条件推断写成了可泛化的结论"


def test_section_states_which_direction_the_one_sided_p_tests():
    md = pe.run_random_timing(_real_bt(), n_trials=5, seed=11, **PARAMS)
    assert "H₁" in md and "≥" in md, "单侧 p 没说备择假设的方向"


def test_main_wires_the_baseline_into_the_report():
    """基线算完必须进 `plan_e_backtest_v2_latest.md`：停用声明引用的就是这份报告，
    只在独立小节里跑一次、报告里没有的话，读报告的人依然看不到这条检验。"""
    import inspect
    src = inspect.getsource(pe.main)
    assert "run_random_timing" in src, "随机择时基线没接进报告生成"
