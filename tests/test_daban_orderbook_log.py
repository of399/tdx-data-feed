"""预警盘口快照日志：补齐历史数据里没有的封单/五档/一字维度。

对应设计：盘中触发打板预警时，按"事件驱动 + 兜底心跳"落一行盘口快照。
"""
from __future__ import annotations

import csv

import pytest

from tdxfeed.daban.orderbook_log import (
    FIELDS,
    OrderbookTracker,
    append_orderbook_rows,
    compact_date,
    poll_once,
    read_orderbook_rows,
)
from tdxfeed.daban.realtime import Quote
from tdxfeed.daban.scanner import (
    STATUS_BROKEN,
    STATUS_NEAR,
    STATUS_RESEAL,
    STATUS_SEALED,
    Candidate,
    ScanResult,
)

T0 = 1_800_000_000.0  # 固定基准时刻，心跳按 now 差值判定


def make_quote(
    code: str = "sh600000",
    name: str = "测试股",
    *,
    prev_close: float = 10.0,
    price: float = 11.0,
    open_: float = 10.5,
    high: float = 11.0,
    low: float = 10.4,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    volume: float = 20_000_000.0,
    amount: float = 220_000_000.0,
    quote_time: str = "09:31:00",
    date: str = "2026-10-23",
) -> Quote:
    bids = [(11.0, 1_000_000.0)] if bids is None else bids
    asks = [] if asks is None else asks
    return Quote(
        code=code,
        name=name,
        open=open_,
        prev_close=prev_close,
        price=price,
        high=high,
        low=low,
        bids=bids,
        asks=asks,
        seal_shares=bids[0][1] if bids else 0.0,
        seal_amount=bids[0][0] * bids[0][1] if bids else 0.0,
        volume=volume,
        amount=amount,
        date=date,
        time=quote_time,
    )


def make_candidate(
    code: str = "600000",
    name: str = "测试股",
    **overrides,
) -> Candidate:
    """构造真实 Candidate（默认：封板、1000 万股买一、封单额 1.1 亿）。"""
    base = dict(
        code=code,
        name=name,
        board="主板10%",
        status=STATUS_SEALED,
        price=11.0,
        prev_close=10.0,
        limit_price=11.0,
        pct=10.0,
        seal_shares=1_000_000.0,
        seal_amount=11_000_000.0,
        seal_ratio_shares=0.05,
        seal_ratio_amount=0.08,
        seal_vs_vol5=1.2,
        turnover=0.0412,
        consecutive=1,
        first_seal_time="09:31:00",
        break_count=0,
        seal_decay=0.0,
        cancel_rate=0.0,
        score=0.62,
        vol_ratio=2.5,
    )
    base.update(overrides)
    return Candidate(**base)


def make_result(candidates: list[Candidate], trade_date: str = "20261023") -> ScanResult:
    return ScanResult(
        trade_date=trade_date,
        scan_time="09:31:00",
        phase="震荡市",
        candidates=candidates,
        stats={"sealed_candidates": sum(1 for c in candidates if c.status == STATUS_SEALED)},
        session="continuous",
    )


def observe(tracker: OrderbookTracker, cand, quote, now: float, trade_date: str = "20261023"):
    result = make_result([] if cand is None else [cand], trade_date)
    quotes = {} if quote is None else {quote.code: quote}
    return tracker.observe(result, quotes, now=now)


# ---------------------------------------------------------------- 落盘格式


def test_append_writes_header_once_in_field_order(tmp_path):
    rows = [
        {**{k: "" for k in FIELDS}, "trade_date": "20261023", "code": "600000"},
        {**{k: "" for k in FIELDS}, "trade_date": "20261023", "code": "600001"},
    ]
    path = append_orderbook_rows(rows, out_dir=tmp_path)
    path = append_orderbook_rows(rows, out_dir=tmp_path)

    with path.open(encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    assert lines[0] == ",".join(FIELDS)
    assert len(lines) == 5, "两次各 2 行 = 1 行表头 + 4 行数据，表头不重复"
    assert len(read_orderbook_rows(path)) == 4


def test_append_without_rows_creates_no_file(tmp_path):
    assert append_orderbook_rows([], out_dir=tmp_path) is None
    assert list(tmp_path.glob("orderbook_*.csv")) == []


# ---------------------------------------------------------------- 事件与节流


def test_first_seal_poll_writes_one_seal_event():
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(), make_quote(), T0)
    assert [r["event"] for r in rows] == ["SEAL"]
    assert rows[0]["status"] == STATUS_SEALED
    assert rows[0]["code"] == "600000"
    assert rows[0]["trade_date"] == "20261023"


def test_identical_seal_poll_within_heartbeat_writes_nothing():
    tracker = OrderbookTracker(heartbeat_sec=30.0)
    observe(tracker, make_candidate(), make_quote(), T0)
    assert observe(tracker, make_candidate(), make_quote(), T0 + 3) == []
    assert observe(tracker, make_candidate(), make_quote(), T0 + 20) == []


def test_sealed_heartbeat_writes_after_interval_elapsed():
    tracker = OrderbookTracker(heartbeat_sec=30.0)
    observe(tracker, make_candidate(), make_quote(), T0)
    rows = observe(tracker, make_candidate(), make_quote(), T0 + 31)
    assert [r["event"] for r in rows] == ["HEARTBEAT"]


def test_break_then_reseal_each_write_an_event():
    tracker = OrderbookTracker(heartbeat_sec=999.0)
    q = make_quote()
    observe(tracker, make_candidate(), q, T0)

    broken = make_candidate(
        status=STATUS_BROKEN, price=10.8, pct=8.0,
        seal_shares=0.0, seal_amount=0.0, first_seal_time="",
    )
    rows = observe(tracker, broken, make_quote(price=10.8, bids=[]), T0 + 60)
    assert [r["event"] for r in rows] == ["BREAK"]
    assert rows[0]["sealed"] == 0

    resealed = make_candidate(status=STATUS_RESEAL, break_count=1)
    rows = observe(tracker, resealed, make_quote(), T0 + 120)
    assert [r["event"] for r in rows] == ["RESEAL"]
    assert rows[0]["status"] == STATUS_RESEAL


def test_near_then_seal_writes_both_events():
    tracker = OrderbookTracker(heartbeat_sec=999.0)
    near = make_candidate(
        status=STATUS_NEAR, price=10.98, pct=9.8,
        seal_shares=20_000.0, seal_amount=219_600.0, first_seal_time="",
    )
    rows = observe(tracker, near, make_quote(price=10.98, bids=[(10.98, 20_000.0)]), T0)
    assert [r["event"] for r in rows] == ["NEAR"]
    rows = observe(tracker, make_candidate(), make_quote(), T0 + 10)
    assert [r["event"] for r in rows] == ["SEAL"]


def test_seal_amount_grow_over_threshold_writes_seal_grow():
    tracker = OrderbookTracker(heartbeat_sec=999.0, change_pct=0.2)
    observe(tracker, make_candidate(), make_quote(), T0)
    bigger = make_candidate(seal_shares=1_500_000.0, seal_amount=16_500_000.0)
    rows = observe(tracker, bigger, make_quote(bids=[(11.0, 1_500_000.0)]), T0 + 5)
    assert [r["event"] for r in rows] == ["SEAL_GROW"]


def test_seal_amount_shrink_over_threshold_writes_seal_shrink():
    tracker = OrderbookTracker(heartbeat_sec=999.0, change_pct=0.2)
    observe(tracker, make_candidate(), make_quote(), T0)
    smaller = make_candidate(seal_shares=600_000.0, seal_amount=6_600_000.0, cancel_rate=120.0)
    rows = observe(tracker, smaller, make_quote(bids=[(11.0, 600_000.0)]), T0 + 5)
    assert [r["event"] for r in rows] == ["SEAL_SHRINK"]
    assert rows[0]["cancel_rate"] == pytest.approx(120.0)


def test_shrink_under_threshold_is_suppressed():
    tracker = OrderbookTracker(heartbeat_sec=999.0, change_pct=0.2)
    observe(tracker, make_candidate(), make_quote(), T0)
    smaller = make_candidate(seal_shares=900_000.0, seal_amount=9_900_000.0)
    assert observe(tracker, smaller, make_quote(bids=[(11.0, 900_000.0)]), T0 + 5) == []


def test_ask_volume_appearing_on_sealed_name_writes_ask_open():
    tracker = OrderbookTracker(heartbeat_sec=999.0, change_pct=0.99)
    observe(tracker, make_candidate(), make_quote(asks=[]), T0)
    rows = observe(
        tracker, make_candidate(),
        make_quote(asks=[(11.0, 3_000.0)]), T0 + 5,
    )
    assert [r["event"] for r in rows] == ["ASK_OPEN"]
    assert rows[0]["ask1_vol"] == pytest.approx(3_000.0)
    assert rows[0]["seal_dead"] == 0


# ---------------------------------------------------------------- 盘口字段


def test_yizi_and_dead_seal_flags():
    tracker = OrderbookTracker()
    yizi = make_candidate(status=STATUS_SEALED, price=11.0, prev_close=10.0, limit_price=11.0)
    rows = observe(
        tracker, yizi,
        make_quote(open_=11.0, low=11.0, high=11.0, price=11.0, asks=[]),
        T0,
    )
    assert rows[0]["is_yizi"] == 1
    assert rows[0]["seal_dead"] == 1
    assert rows[0]["ask_empty"] == 1
    assert rows[0]["touched"] == 1
    assert rows[0]["sealed"] == 1

    tracker2 = OrderbookTracker()
    rows = observe(tracker2, make_candidate(), make_quote(open_=10.5, low=10.4), T0)
    assert rows[0]["is_yizi"] == 0
    assert rows[0]["seal_dead"] == 1


def test_five_level_book_and_seal_columns():
    tracker = OrderbookTracker()
    bids = [(11.0, 1_000_000.0), (10.99, 200_000.0), (10.98, 100_000.0),
            (10.97, 50_000.0), (10.96, 10_000.0)]
    asks = [(11.01, 500.0)]
    cand = make_candidate(seal_shares=1_000_000.0, seal_amount=11_000_000.0)
    rows = observe(tracker, cand, make_quote(bids=bids, asks=asks), T0)
    r = rows[0]
    assert r["seal_shares"] == pytest.approx(1_000_000.0)
    assert r["seal_amount"] == pytest.approx(11_000_000.0)
    assert r["bid1_price"] == pytest.approx(11.0)
    assert r["bid5_vol"] == pytest.approx(10_000.0)
    assert r["ask1_price"] == pytest.approx(11.01)
    assert r["ask1_vol"] == pytest.approx(500.0)
    assert r["ask2_vol"] == ""
    assert r["quote_time"] == "09:31:00"


def test_ohlc_and_pct_copied_from_quote():
    tracker = OrderbookTracker()
    rows = observe(
        tracker, make_candidate(),
        make_quote(prev_close=10.0, open_=10.2, high=11.0, low=10.1, price=11.0),
        T0,
    )
    r = rows[0]
    assert (r["open"], r["high"], r["low"], r["price"]) == (10.2, 11.0, 10.1, 11.0)
    assert r["pct"] == pytest.approx(10.0)


def test_traded_volume_and_amount_are_logged():
    """新浪 8/9 段是成交量/成交额（不是封单），必须原样留痕。"""
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(), make_quote(), T0)
    assert rows[0]["volume"] == pytest.approx(20_000_000.0)
    assert rows[0]["amount"] == pytest.approx(220_000_000.0)


def test_seal_vs_amount_is_queue_over_turnover():
    """排队强度 = 封单额 / 当日成交额：1.1e7 / 2.2e8 = 5%。"""
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(), make_quote(), T0)
    assert rows[0]["seal_vs_amount"] == pytest.approx(0.05)


def test_seal_vs_amount_blank_when_amount_missing():
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(), make_quote(volume=0.0, amount=0.0), T0)
    assert rows[0]["seal_vs_amount"] == ""


def test_seal_ratio_src_reports_amount_method_only_with_float_shares():
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(seal_ratio_amount=0.08), make_quote(), T0)
    assert rows[0]["seal_ratio_src"] == "金额法"
    rows[0]["turnover"] == pytest.approx(0.0412)

    tracker = OrderbookTracker()
    rows = observe(
        tracker, make_candidate(seal_ratio_amount=None, turnover=None, seal_vs_vol5=1.35),
        make_quote(), T0,
    )
    assert rows[0]["seal_ratio_src"] == "本地代理"
    assert rows[0]["seal_vs_vol5"] == pytest.approx(1.35)
    assert rows[0]["turnover"] == ""


def test_new_and_st_and_board_flags():
    tracker = OrderbookTracker()
    rows = observe(
        tracker, make_candidate(code="301001", name="C次新", board="双创20%"),
        make_quote(code="sz301001", name="C次新"), T0,
    )
    assert rows[0]["is_new"] == 1
    assert rows[0]["is_st"] == 0

    tracker = OrderbookTracker()
    rows = observe(
        tracker, make_candidate(code="600001", name="ST测试", board="主板ST10%"),
        make_quote(code="sh600001", name="ST测试"), T0,
    )
    assert rows[0]["is_st"] == 1
    assert rows[0]["board"] == "主板ST10%"


def test_limit_price_uses_old_st_ratio_before_rule_change():
    """主板 ST 在 2026-07-06 前是 5%：日志里的涨停价必须按交易日分段。"""
    tracker = OrderbookTracker()
    prev_close = 10.0
    rows = observe(
        tracker,
        make_candidate(code="600001", name="ST测试", prev_close=prev_close),
        make_quote(code="sh600001", name="ST测试", prev_close=prev_close, price=10.5, high=10.5),
        T0,
        trade_date="20260702",
    )
    assert rows[0]["limit_price"] == pytest.approx(10.5)
    assert rows[0]["ratio"] == pytest.approx(0.05)

    tracker = OrderbookTracker()
    rows = observe(
        tracker,
        make_candidate(code="600001", name="ST测试", prev_close=prev_close),
        make_quote(code="sh600001", name="ST测试", prev_close=prev_close, price=10.5, high=10.5),
        T0,
        trade_date="20260709",
    )
    assert rows[0]["limit_price"] == pytest.approx(11.0)
    assert rows[0]["ratio"] == pytest.approx(0.10)
    assert rows[0]["sealed"] == 0


# ---------------------------------------------------------------- 输入边界


def test_trade_date_is_compacted_to_yyyymmdd():
    """与纸盘账本 signal_date 同格式（YYYYMMDD），且紧凑化不能弄坏日期敏感规则。"""
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(), make_quote(), T0, trade_date="2026-10-23")
    assert rows[0]["trade_date"] == "20261023"

    tracker = OrderbookTracker()
    rows = observe(
        tracker,
        make_candidate(code="600001", name="ST测试"),
        make_quote(code="sh600001", name="ST测试", price=10.5, high=10.5),
        T0, trade_date="2026-07-02",
    )
    assert rows[0]["trade_date"] == "20260702"
    assert rows[0]["limit_price"] == pytest.approx(10.5)  # 仍是 5% 规则


def test_cb_adds_today_when_local_daily_is_stale():
    """本地日线只到昨天 → 今日封板没被算进 consecutive，cb 要 +1（东财口径）。"""
    tracker = OrderbookTracker(last_daily_date=lambda code: "2026-10-22")
    rows = observe(tracker, make_candidate(consecutive=1), make_quote(), T0)
    assert rows[0]["cb"] == 2
    assert rows[0]["consecutive"] == 1
    assert rows[0]["daily_last_date"] == "20261022"


def test_cb_unchanged_when_local_daily_has_today():
    tracker = OrderbookTracker(last_daily_date=lambda code: "2026-10-23")
    rows = observe(tracker, make_candidate(consecutive=1), make_quote(), T0)
    assert rows[0]["cb"] == 1


def test_cb_for_broken_name_keeps_local_count():
    tracker = OrderbookTracker(last_daily_date=lambda code: "2026-10-22")
    broken = make_candidate(status=STATUS_BROKEN, price=10.8, pct=8.0, consecutive=2)
    rows = observe(tracker, broken, make_quote(price=10.8), T0)
    assert rows[0]["cb"] == 2


def test_compact_date_blank_when_input_is_not_a_date():
    """空串不能被当成"今天"，否则本地缺日线会被误判成已入库。"""
    assert compact_date("") == ""
    assert compact_date(None) == ""
    assert compact_date("2026-10-22T00:00:00.000000") == "20261022"
    assert compact_date("20261022") == "20261022"


def test_cb_without_daily_lookup_falls_back_to_local_count():
    tracker = OrderbookTracker()
    rows = observe(tracker, make_candidate(consecutive=1), make_quote(), T0)
    assert rows[0]["cb"] == 1
    assert rows[0]["daily_last_date"] == ""


def test_cb_is_first_board_when_no_local_history_at_all():
    """北交所/新股本地无日线：consecutive=0，但今日封板至少是首板。"""
    tracker = OrderbookTracker(last_daily_date=lambda code: "")
    rows = observe(
        tracker,
        make_candidate(code="920091", price=13.0, pct=30.0, consecutive=0),
        make_quote(code="bj920091", price=13.0, high=13.0, bids=[(13.0, 1_000_000.0)]),
        T0,
    )
    assert rows[0]["sealed"] == 1
    assert rows[0]["cb"] == 1
    assert rows[0]["limit_price"] == pytest.approx(13.0)  # 北交所 30%


def test_candidate_without_quote_writes_nothing():
    tracker = OrderbookTracker()
    assert observe(tracker, make_candidate(), None, T0) == []


def test_suspended_quote_writes_nothing():
    tracker = OrderbookTracker()
    assert observe(tracker, make_candidate(), make_quote(price=0.0), T0) == []


def test_no_candidates_writes_no_rows():
    tracker = OrderbookTracker()
    result = make_result([])
    assert tracker.observe(result, {"sh600000": make_quote()}, now=T0) == []


def test_tracker_keeps_multiple_codes_independent():
    tracker = OrderbookTracker(heartbeat_sec=999.0)
    a, b = make_candidate(code="600000"), make_candidate(code="600001", name="测试股2")
    qa, qb = make_quote(code="sh600000"), make_quote(code="sh600001", name="测试股2")
    rows = tracker.observe(make_result([a, b]), {qa.code: qa, qb.code: qb}, now=T0)
    assert [r["code"] for r in rows] == ["600000", "600001"]
    assert [r["event"] for r in rows] == ["SEAL", "SEAL"]
    assert tracker.observe(make_result([a, b]), {qa.code: qa, qb.code: qb}, now=T0 + 5) == []


# ---------------------------------------------------------------- 轮询入口


def test_poll_once_fetch_scans_appends_and_summarises(tmp_path):
    """poll_once 用注入的 fetch/scan，不打网络、不读全市场。"""
    quotes = {"sh600000": make_quote()}
    calls = []

    def fake_fetch(symbols, **kwargs):
        calls.append(symbols)
        return quotes

    def fake_scan(q):
        return make_result([make_candidate()])

    summary = poll_once(
        ["sh600000"],
        out_dir=tmp_path,
        fetch=fake_fetch,
        scan=fake_scan,
        now=T0,
    )
    assert calls == [["sh600000"]]
    assert summary["candidates"] == 1
    assert summary["rows"] == 1
    assert summary["path"].name == "orderbook_20261023.csv"
    rows = read_orderbook_rows(summary["path"])
    assert rows[0]["code"] == "600000"
    assert rows[0]["event"] == "SEAL"

    tracker = summary["tracker"]
    assert summary["records"][0]["event"] == "SEAL"
    assert summary["records"][0]["seal_shares"] == pytest.approx(1_000_000.0)
    again = poll_once(
        ["sh600000"],
        out_dir=tmp_path,
        fetch=fake_fetch,
        scan=fake_scan,
        tracker=tracker,
        now=T0 + 4,
    )
    assert again["rows"] == 0
    assert len(read_orderbook_rows(again["path"])) == 1


def test_poll_once_names_file_with_compacted_trade_date(tmp_path):
    quotes = {"sh600000": make_quote()}
    summary = poll_once(
        ["sh600000"],
        out_dir=tmp_path,
        fetch=lambda symbols, **kw: quotes,
        scan=lambda q: make_result([make_candidate()], trade_date="2026-10-23"),
        now=T0,
    )
    assert summary["path"].name == "orderbook_20261023.csv"
    assert summary["trade_date"] == "20261023"
    assert read_orderbook_rows(summary["path"])[0]["trade_date"] == "20261023"
