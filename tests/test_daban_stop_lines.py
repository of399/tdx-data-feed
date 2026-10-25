"""五条自动停止线（设计 §7）：每条都要一正一反，否则「停止线」只是横幅里的一句话。

统计口径只有两个来源：**已平仓** pair 的净收益，和**已回填**的成交/未成交两态。
未成交笔（`filled=0`）只进成交率，不进均值——`docs/DABAN_EXEC_RESEARCH_20261022.md` 测到
成交到的那批封板率只有 39.7%（未成交 95.4%），把未成交丢掉，样本就只剩"买到的恰好是更容易输的那批"。

第 2 条（前 10 笔不判均值）反过来也约束报告措辞：样本不足时的正负数都不能拿来当续跑的理由。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402


def row(code="600001", filled=1, closed=1, net_pct=0.5, net_yuan=50.0):
    return {
        "code": code,
        "filled": filled,
        "closed": closed,
        "net_pct": net_pct,
        "net_yuan": net_yuan,
    }


def rows(n, **kw):
    return [row(code=f"6000{i:02d}", **kw) for i in range(n)]


def test_rule1_stops_at_thirty_closed_samples_and_not_before():
    assert trial.evaluate_stop(rows(29, net_pct=1.0, net_yuan=100.0)).triggered is False
    v = trial.evaluate_stop(rows(30, net_pct=1.0, net_yuan=100.0))
    assert v.triggered and v.rule.startswith("第 1 条")
    assert v.n_closed == 30


def test_rule2_does_not_judge_mean_below_ten_samples():
    """9 笔、均值 −5%/笔 也不停：样本不足时这条线故意不看均值（亏损也没摸到第 4 条）。"""
    v = trial.evaluate_stop(rows(9, net_pct=-5.0, net_yuan=-100.0))
    assert v.triggered is False, v.rule


def test_rule3_stops_when_closed_mean_goes_below_minus_twenty_bps():
    assert trial.evaluate_stop(rows(10, net_pct=-0.19, net_yuan=-20.0)).triggered is False
    v = trial.evaluate_stop(rows(10, net_pct=-0.21, net_yuan=-20.0))
    assert v.triggered and v.rule.startswith("第 3 条")
    assert v.mean_pct is not None and v.mean_pct < trial.STOP_MEAN_PCT


def test_rule4_realized_loss_stops_before_the_statistics_do():
    """真金止损不等样本量：5 笔亏 4500 元就停，哪怕均值那条线还没资格判。"""
    assert trial.evaluate_stop(rows(4, net_pct=-1.0, net_yuan=-900.0)).triggered is False
    v = trial.evaluate_stop(rows(5, net_pct=-1.0, net_yuan=-900.0))
    assert v.triggered and v.rule.startswith("第 4 条")
    assert v.realized_yuan <= -trial.STOP_LOSS_YUAN


def test_rule5_stops_when_most_tickets_never_filled():
    """20 笔里只成交 6 笔（miss 70%）⇒ 买不到，样本口径撑不起这条线。"""
    many = rows(6, net_pct=1.0, net_yuan=100.0) + rows(14, filled=0, closed=0, net_pct=None, net_yuan=0.0)
    v = trial.evaluate_stop(many)
    assert v.triggered and v.rule.startswith("第 5 条")
    assert abs(v.miss_ratio - 0.70) < 1e-9

    ok = rows(7, net_pct=1.0, net_yuan=100.0) + rows(13, filled=0, closed=0, net_pct=None, net_yuan=0.0)
    v2 = trial.evaluate_stop(ok)
    assert v2.triggered is False, v2.rule
    assert abs(v2.miss_ratio - 0.65) < 1e-9


def test_missing_fee_rows_are_not_counted_in_the_mean():
    """`net_pct` 没回填（缺券商费用）的行不能冒充样本——不臆造费率。"""
    mixed = rows(10, net_pct=None, net_yuan=None) + rows(1, net_pct=1.0, net_yuan=100.0)
    v = trial.evaluate_stop(mixed)
    assert v.n_closed == 1
    assert v.triggered is False


def test_verdict_text_names_the_rule_and_the_marker_is_the_authority(tmp_path):
    assert trial.marker_exists(tmp_path) is False
    v = trial.evaluate_stop(rows(30, net_pct=1.0, net_yuan=100.0))
    assert v.triggered
    fp = trial.write_marker(tmp_path, v.marker_text)
    assert trial.marker_exists(tmp_path)
    assert "第 1 条" in trial.read_marker(tmp_path)
    assert "disable" in trial.read_marker(tmp_path), "marker 要写清运维收口那一步"

    fp.unlink()
    assert trial.marker_exists(tmp_path) is False


def test_stop_constants_match_the_design():
    assert (trial.STOP_N_FINAL, trial.STOP_N_MIN) == (30, 10)
    assert trial.STOP_MEAN_PCT == -0.20
    assert trial.STOP_LOSS_YUAN == 0.20 * trial.TOTAL_CAP
    assert trial.STOP_FILL_MISS_RATIO == 0.70
