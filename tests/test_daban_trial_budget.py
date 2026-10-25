"""试验仓的额度口径是「当日绝对额」，不是「每轮满仓」（设计 §3 / §4.1）。

`build_intents(cash=200000, max_single_pct=0.05, max_total_pct=0.10)` 单看一轮是对的：
单票 1 万、总仓 2 万。但 timer 每个交易日在连续竞价里命中约 48 轮，每轮都按满仓预算算，
就会各出 2 只、日累计冲到近百只、名义金额远超 20 万本金——总仓上限形同虚设。

正解不改 `tdxfeed/daban/order.py`（它的入参本来就是比例），而是由 trial 层持有
「当日已承诺金额」，把**剩余额度 R** 交给 build_intents，并把比例参数反算成绝对额：
`single_cap = R × (SINGLE_CAP / R) = SINGLE_CAP`，恒等；R 变小时两个上限一起被 R 夹住，
只会更保守。这条换算是本设计最容易出错的地方，所以先写它。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402


@dataclass
class Cand:
    """build_intents 只读这几个属性：limit_price / code / name / score / sector_name / reasons。"""

    code: str
    name: str
    limit_price: float
    score: float = 0.6
    sector_name: str = "测试板块"
    reasons: tuple[str, ...] = ("封板",)


def cands(n: int = 6, price: float = 10.0) -> list[Cand]:
    return [Cand(code=f"60000{i}", name=f"票{i}", limit_price=price) for i in range(n)]


def empty_day() -> dict:
    return trial.load_day(Path("/nonexistent-root-for-unit-tests"), "20261026")


def test_constants_match_the_agreed_trial_book():
    """20 万本金、单票 5%、总仓 10% ⇒ 单票 1 万 / 总仓 2 万。"""
    assert trial.CASH == 200000.0
    assert trial.SINGLE_CAP == 10000.0
    assert trial.TOTAL_CAP == 20000.0
    assert trial.STOP_LOSS_PCT == 0.05
    assert trial.TAKE_PROFIT_PCT == 0.10
    assert trial.LOT == 100


def test_min_order_amount_must_stay_below_single_cap():
    """MIN_ORDER_AMOUNT > SINGLE_CAP 时 build_intents 会把每一笔都跳过——一张都出不来。

    2026-10-25 定本金 20 万时发现过这个算术陷阱（默认 1 万 min_amount 对 5% 单票上限）。
    """
    assert trial.MIN_ORDER_AMOUNT < trial.SINGLE_CAP


def test_build_kwargs_restores_absolute_caps():
    """换算后的两个上限：单票恒等于 SINGLE_CAP，总仓恒等于 min(TOTAL_CAP, R)。

    R 是「当日剩余额度」，build_intents 的 total_cap 只在**本次调用内**累计，所以 R ≤ 2 万时
    总仓上限就是 R 本身（`min(TOTAL_CAP, R)/R = 1.0`），否则一轮能花掉比剩余额度更多的钱。
    """
    for r in (trial.CASH, trial.TOTAL_CAP, 13000.0, 5000.0):
        kw = trial.build_kwargs(r)
        assert kw["cash"] == r
        assert abs(r * kw["max_single_pct"] - min(trial.SINGLE_CAP, r)) < 1e-9, r
        assert abs(r * kw["max_total_pct"] - min(trial.TOTAL_CAP, r)) < 1e-9, r
        assert kw["min_amount"] == trial.MIN_ORDER_AMOUNT
        assert kw["lot"] == trial.LOT
        assert kw["stop_loss_pct"] == trial.STOP_LOSS_PCT
        assert kw["take_profit_pct"] == trial.TAKE_PROFIT_PCT


def test_first_poll_at_the_single_cap_emits_exactly_two_tickets():
    """每笔顶到 1 万时：6 只候选只能出 2 只，合计恰好打满 2 万。"""
    day = empty_day()
    intents = trial.intents_for_poll(cands(6), day)

    assert len(intents) == 2, [i.code for i in intents]
    assert [i.amount for i in intents] == [10000.0, 10000.0]
    assert trial.committed_amount(trial.add_tickets(day, intents)) == 20000.0


def test_cheap_lots_pack_more_than_two_tickets_in_one_day():
    """封的是**金额**不是只数：2026-10-23 真盘一轮出了 3 笔（6480 / 5624 / 5415 元）。

    单笔金额 = 涨停价 × 整手（100 股），落在 [MIN_ORDER_AMOUNT, SINGLE_CAP] 内时票数自然超过 2；
    上界是 floor(TOTAL_CAP / MIN_ORDER_AMOUNT) = 4 笔。「≤2 只/日」是当初按每笔顶满 1 万推的，
    便宜票不成立——n=30 的样本量和这条试验的期限都要按实测形状读。
    """
    day = empty_day()
    intents = trial.intents_for_poll(cands(8, price=55.0), day)  # 5500 元/笔

    assert len(intents) == 3, [i.amount for i in intents]
    assert [i.amount for i in intents] == [5500.0, 5500.0, 5500.0]
    filled = trial.add_tickets(day, intents)  # 返回新账本，不改入参
    assert trial.committed_amount(filled) == 16500.0
    # 第 4 笔要 5500，剩 3500 < MIN_ORDER_AMOUNT ⇒ 当日收口。
    assert trial.remaining_budget(filled) < trial.MIN_ORDER_AMOUNT
    # 上界：全用最小额度也只可能 4 笔。
    assert len(intents) <= trial.TOTAL_CAP // trial.MIN_ORDER_AMOUNT


def test_second_poll_same_day_emits_nothing_once_cap_is_used():
    """日累计不看轮次：第一轮打满 2 万后，第二轮必须 0 票。"""
    day = empty_day()
    day = trial.add_tickets(day, trial.intents_for_poll(cands(6), day))

    assert trial.remaining_budget(day) == 0.0
    assert trial.intents_for_poll(cands(6, price=8.0), day) == []


def test_partial_budget_only_spends_what_is_left():
    """已用 1 万 ⇒ 只剩 1 万；5 元票最多再吃 1 万，日累计仍恰好等于总仓上限。"""
    day = empty_day()
    day = trial.add_tickets(day, trial.intents_for_poll([Cand("600001", "甲", 50.0)], day))
    assert trial.committed_amount(day) == 10000.0
    assert trial.remaining_budget(day) == 10000.0

    rest = trial.intents_for_poll(cands(6, price=5.0), day)
    assert sum(i.amount for i in rest) == 10000.0, [i.amount for i in rest]
    assert trial.committed_amount(trial.add_tickets(day, rest)) == trial.TOTAL_CAP


def test_high_price_ticket_cannot_fill_one_lot_and_is_skipped():
    """100 元以上的票凑不出 1 手（1 万预算 // 101 元 < 100 股）⇒ 静默跳过，不是崩溃。"""
    day = empty_day()
    intents = trial.intents_for_poll([Cand(code="600001", name="高价票", limit_price=101.0)], day)
    assert intents == []
