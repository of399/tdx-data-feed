"""DABAN cb≥3 纸盘自动化测试（规则见 docs/DABAN_PAPER_TRADE_RULES_20261022.md）。"""
from __future__ import annotations

import json
from decimal import Decimal

import pandas as pd
import pytest

from tdxfeed.daban.limit_price import limit_down_price, limit_up_price
from tdxfeed.daban.paper import (
    JOURNAL_FIELDS,
    PaperConfig,
    backfill,
    board_tag,
    daily_report,
    empty_journal,
    enrich_history,
    execute_signal,
    generate_signals,
    load_histories,
    load_journal,
    save_journal,
    settle_deferred,
    upsert_rows,
)

DATES = [f"2026-09-{i:02d}" for i in range(1, 11)]


def _frame(closes, *, opens=None, volumes=None, amounts=None, dates=None,
           code="600001", name="示例股"):
    n = len(closes)
    opens = opens or list(closes)
    volumes = volumes if volumes is not None else [1000.0] * n
    amounts = amounts if amounts is not None else [1e8] * n
    dates = dates or DATES[:n]
    df = pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
        "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)],
        "close": closes,
        "volume": volumes,
        "amount": amounts,
    })
    return enrich_history(df, code, name)


def _signal_row(**kw):
    base = {f: "" for f in JOURNAL_FIELDS}
    close_t = kw.pop("close_T", 11.0)
    ratio = kw.pop("ratio", 0.10)
    base.update({
        "signal_date": "20260906",
        "code": "600001",
        "name": "示例股",
        "cb": 4,
        "is_st": 0,
        "board": "main10",
        "close_T": close_t,
        "limit_T": limit_up_price(close_t, Decimal(str(ratio))),
        "amount_T": 2e8,
        "avg_amount_5d": 1.8e8,
        "rank": 1,
        "selected": 1,
        "status": "pending",
        "limit_up_T1": limit_up_price(close_t, Decimal(str(ratio))),
        "limit_dn_T1": limit_down_price(close_t, Decimal(str(ratio))),
        "deferred": 0,
        "defer_count": 0,
    })
    base.update(kw)
    return base


def _cfg(**kw):
    kw.setdefault("new_stock_days", 0)
    return PaperConfig(**kw)


def test_board_tag():
    assert board_tag("600001", "示例股") == "main10"
    # 2026-07-06 起主板风险警示票与主板普通票并轨 10%
    assert board_tag("600001", "ST示例") == "st10"
    assert board_tag("600001", "ST示例", "2026-07-05") == "st5"
    assert board_tag("600001", "ST示例", "20260706") == "st10"
    assert board_tag("300001", "ST示例") == "gem20"
    assert board_tag("688001", "示例") == "gem20"


def test_enrich_history_limit_chain_and_cb():
    h = _frame([10.0, 11.0, 12.1, 13.31, 14.64])
    assert h["limit_up"].tolist()[1:] == pytest.approx([11.0, 12.1, 13.31, 14.64])
    assert h["sealed"].tolist() == [False, True, True, True, True]
    assert h["cb"].tolist() == [0, 1, 2, 3, 4]
    assert h["hist_n"].tolist() == [1, 2, 3, 4, 5]


def test_enrich_history_suspended_day_preserves_streak():
    h = _frame([10.0, 11.0, 11.0, 12.1], volumes=[100.0, 100.0, 0.0, 100.0])
    assert h["suspended"].tolist() == [False, False, True, False]
    assert h["cb"].tolist() == [0, 1, 1, 2]
    assert h["hist_n"].tolist() == [1, 2, 2, 3]


def test_enrich_history_st_main_uses_10pct_after_rule_change():
    h = _frame([10.0, 11.0, 12.1, 13.31, 14.64], name="ST示例")
    assert h["ratio"].iloc[0] == pytest.approx(0.10)
    assert h["limit_up"].tolist()[1:] == pytest.approx([11.0, 12.1, 13.31, 14.64])
    assert h["sealed"].tolist() == [False, True, True, True, True]
    assert h["cb"].tolist() == [0, 1, 2, 3, 4]


def test_enrich_history_st_main_ratio_switches_on_rule_date():
    """同一只主板 ST 票跨 2026-07-06：5% 与 10% 两档按行各自成立。"""
    dates = ["2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]
    closes = [10.0, 10.5, 11.55, 12.71]
    h = _frame(closes, dates=dates, name="ST示例")
    assert h["ratio"].tolist() == pytest.approx([0.05, 0.05, 0.10, 0.10])
    assert h["limit_up"].tolist()[1:] == pytest.approx([10.50, 11.55, 12.71])
    assert h["sealed"].tolist() == [False, True, True, True]
    assert h["cb"].tolist() == [0, 1, 2, 3]


def test_generate_signals_filters_ranks_and_selects():
    a = _frame([10.0, 11.0, 12.1, 13.31, 14.64, 16.1],
               amounts=[1e8] * 5 + [1e8], code="600001")
    b = _frame([10.0, 11.0, 12.1, 13.31, 14.64, 16.1],
               amounts=[1e8] * 5 + [3e8], code="600002")
    c = _frame([10.0, 11.0, 12.1, 12.2, 12.5, 12.6], code="600003")
    sig = generate_signals({"600001": a, "600002": b, "600003": c},
                           "2026-09-06", _cfg(max_new_per_day=1))
    assert len(sig) == 2
    assert [r["code"] for r in sig] == ["600002", "600001"]
    assert [r["rank"] for r in sig] == [1, 2]
    assert [r["selected"] for r in sig] == [1, 0]
    assert sig[0]["cb"] == 5
    assert sig[0]["amount_T"] == pytest.approx(3e8)
    assert sig[0]["limit_up_T1"] == pytest.approx(17.71)
    assert sig[0]["limit_dn_T1"] == pytest.approx(14.49)


def test_generate_signals_new_stock_excluded_without_consuming_slot():
    closes = [10.0] * 12 + [11.0, 12.1, 13.31, 14.64, 16.1, 17.71, 19.48, 21.43]
    dates = ([f"2026-08-{i:02d}" for i in range(18, 31)]
             + [f"2026-09-{i:02d}" for i in range(1, 8)])
    new = _frame(closes, dates=dates, code="600009",
                 volumes=[0.0] * 12 + [1000.0] * 8)
    old = _frame([10.0] * 15 + [11.0, 12.1, 13.31, 14.64, 16.1],
                 dates=dates, code="600001")
    sig = generate_signals({"600009": new, "600001": old},
                           "2026-09-06",
                           _cfg(max_new_per_day=1, new_stock_days=10))
    by_code = {r["code"]: r for r in sig}
    assert by_code["600009"]["status"] == "new_stock"
    assert by_code["600009"]["selected"] == 0
    assert by_code["600009"]["rank"] == 0
    assert by_code["600001"]["selected"] == 1


def test_execute_signal_fills_open_sells_close():
    row = _signal_row()
    bar = {"date": "2026-09-07", "open": 11.2, "close": 11.6, "volume": 1000.0}
    out = execute_signal(row, bar, _cfg())
    assert out["status"] == "filled"
    assert out["entry_date"] == "20260907"
    assert out["fill_price"] == pytest.approx(11.2)
    assert out["fill_time"] == "09:25"
    assert out["shares"] == 8900
    assert out["slippage_open_bps"] == pytest.approx(0.0)
    assert out["sell_price"] == pytest.approx(11.6)
    assert out["sell_date"] == "20260907"
    assert out["slippage_close_bps"] == pytest.approx(0.0)
    gross = 11.6 / 11.2 - 1
    assert out["gross_ret"] == pytest.approx(gross)
    assert out["net_fixed"] == pytest.approx(gross - 0.003)
    buy, sell = 8900 * 11.2, 8900 * 11.6
    cost = max(5.0, buy * 2.5e-4) + max(5.0, sell * 2.5e-4) + sell * 5e-4
    assert out["net_actual"] == pytest.approx((sell - buy - cost) / buy)


def test_execute_signal_skips_open_limit():
    row = _signal_row()
    bar = {"date": "2026-09-07", "open": 12.1, "close": 12.1, "volume": 1000.0}
    out = execute_signal(row, bar, _cfg())
    assert out["status"] == "unbuyable_open_limit"
    assert out["shares"] == 0
    assert out["sell_price"] == ""


def test_execute_signal_suspended_when_bar_missing():
    out = execute_signal(_signal_row(), None, _cfg())
    assert out["status"] == "suspended"
    assert out["shares"] == 0


def test_execute_signal_defers_on_limit_down_close():
    row = _signal_row()
    bar = {"date": "2026-09-07", "open": 9.9, "close": 9.9, "volume": 1000.0}
    out = execute_signal(row, bar, _cfg())
    assert out["status"] == "filled"
    assert out["deferred"] == 1
    assert out["sell_price"] == ""
    assert out["open_limit_down"] == 1


def test_execute_signal_not_selected_keeps_theoretical_fill():
    row = _signal_row(selected=0)
    bar = {"date": "2026-09-07", "open": 11.2, "close": 11.6, "volume": 1000.0}
    out = execute_signal(row, bar, _cfg())
    assert out["status"] == "not_selected"
    assert out["fill_price"] == pytest.approx(11.2)
    assert out["shares"] == 0
    assert out["gross_ret"] == pytest.approx(11.6 / 11.2 - 1)


def test_execute_signal_size_too_small():
    row = _signal_row()
    bar = {"date": "2026-09-07", "open": 11.2, "close": 11.6, "volume": 1000.0}
    out = execute_signal(row, bar, _cfg(capital=5000.0))
    assert out["status"] == "size_too_small"
    assert out["shares"] == 0


def _defer_bars():
    return [
        {"date": "2026-09-08", "open": 10.8, "close": 10.9, "volume": 1000.0,
         "limit_down": 10.08},
        {"date": "2026-09-09", "open": 10.0, "close": 10.2, "volume": 1000.0,
         "limit_down": 9.81},
    ]


def test_settle_deferred_sells_t2_open():
    row = execute_signal(_signal_row(),
                         {"date": "2026-09-07", "open": 11.0, "close": 9.9,
                          "volume": 1000.0}, _cfg())
    out = settle_deferred(row, _defer_bars(), _cfg())
    assert out["status"] == "filled"
    assert out["deferred"] == 1
    assert out["defer_count"] == 1
    assert out["sell_date"] == "20260908"
    assert out["sell_price"] == pytest.approx(10.8)
    assert out["gross_ret"] == pytest.approx(10.8 / 11.0 - 1)


def test_settle_deferred_waits_t3_after_t2_sealed_limit_down():
    row = execute_signal(_signal_row(),
                         {"date": "2026-09-07", "open": 11.0, "close": 9.9,
                          "volume": 1000.0}, _cfg())
    bars = [
        {"date": "2026-09-08", "open": 10.08, "close": 10.08, "volume": 1000.0,
         "limit_down": 10.08},
        {"date": "2026-09-09", "open": 9.07, "close": 9.5, "volume": 1000.0,
         "limit_down": 9.07},
    ]
    out = settle_deferred(row, bars, _cfg())
    assert out["status"] == "filled"
    assert out["defer_count"] == 2
    assert out["sell_date"] == "20260909"
    assert out["sell_price"] == pytest.approx(9.5)
    assert "t3_close" in out["notes"]


def test_settle_deferred_sells_t2_even_before_t3_available():
    row = execute_signal(_signal_row(),
                         {"date": "2026-09-07", "open": 11.0, "close": 9.9,
                          "volume": 1000.0}, _cfg())
    bars = [{"date": "2026-09-08", "open": 10.8, "close": 10.9,
             "volume": 1000.0, "limit_down": 10.08}]
    out = settle_deferred(row, bars, _cfg())
    assert out["sell_date"] == "20260908"
    assert out["sell_price"] == pytest.approx(10.8)
    assert out["defer_count"] == 1


def test_settle_deferred_keeps_waiting_without_t2_t3_bars():
    row = execute_signal(_signal_row(),
                         {"date": "2026-09-07", "open": 11.0, "close": 9.9,
                          "volume": 1000.0}, _cfg())
    out = settle_deferred(row, [], _cfg())
    assert out["status"] == "filled"
    assert out["deferred"] == 1
    assert out["sell_price"] == ""


def test_settle_deferred_marks_unsold_after_t3_suspension():
    row = execute_signal(_signal_row(),
                         {"date": "2026-09-07", "open": 11.0, "close": 9.9,
                          "volume": 1000.0}, _cfg())
    bars = [
        {"date": "2026-09-08", "open": 10.08, "close": 10.08, "volume": 1000.0,
         "limit_down": 10.08},
        None,
    ]
    out = settle_deferred(row, bars, _cfg())
    assert out["status"] == "deferred_unsold"
    assert out["sell_price"] == ""


def test_journal_roundtrip_and_upsert():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        j = empty_journal()
        assert list(j.columns) == JOURNAL_FIELDS
        row = _signal_row(code="000001")
        j = upsert_rows(j, [row])
        save_journal(j, p)
        got = load_journal(p)
        assert list(got.columns) == JOURNAL_FIELDS
        assert got.iloc[0]["code"] == "000001"
        assert got.iloc[0]["signal_date"] == "20260906"

        changed = dict(row, amount_T=9e8)
        j2 = upsert_rows(got, [changed])
        assert len(j2) == 1
        assert float(j2.iloc[0]["amount_T"]) == pytest.approx(9e8)

        filled = dict(row, status="filled", sell_price=11.5)
        j3 = upsert_rows(j2, [filled])
        assert j3.iloc[0]["status"] == "pending"


def test_daily_report_two_books():
    a = _signal_row(selected=1, status="filled", fill_price=11.2,
                    sell_price=11.6, shares=8900, gross_ret=11.6 / 11.2 - 1,
                    net_fixed=11.6 / 11.2 - 1 - 0.003,
                    net_actual=0.0345, signal_date="2026-09-06")
    b = _signal_row(selected=0, status="not_selected", fill_price=11.0,
                    sell_price=11.33, shares=0, gross_ret=0.03,
                    net_fixed=0.027, net_actual=0.025,
                    signal_date="2026-09-06", code="600002")
    c = _signal_row(selected=0, status="unbuyable_open_limit",
                    signal_date="2026-09-06", code="600003")
    rep = daily_report(upsert_rows(empty_journal(), [a, b, c]), _cfg())
    assert rep["book_b"]["n_trades"] == 2
    assert rep["book_b"]["avg_net_fixed"] == pytest.approx((0.027 + (11.6 / 11.2 - 1 - 0.003)) / 2)
    assert rep["book_a"]["n_trades"] == 1
    assert rep["book_a"]["pnl"] > 0
    assert rep["status_counts"]["unbuyable_open_limit"] == 1


def test_daily_report_groups_days_by_entry_date():
    a = _signal_row(selected=1, status="filled", fill_price=11.0,
                    sell_price=11.2, shares=9000, gross_ret=0.01818,
                    net_fixed=0.01518, net_actual=0.017, entry_date="20260908",
                    signal_date="20260905", code="600001")
    b = _signal_row(selected=0, status="not_selected", fill_price=10.0,
                    sell_price=10.2, shares=0, gross_ret=0.02,
                    net_fixed=0.017, net_actual=0.019, entry_date="20260908",
                    signal_date="20260906", code="600002")
    rep = daily_report(upsert_rows(empty_journal(), [a, b]), _cfg())
    assert rep["book_b"]["n_days"] == 1


def test_backfill_end_to_end():
    dates = DATES
    df = _frame([10.0, 11.0, 12.1, 13.31, 14.50, 14.50, 14.50, 14.50, 14.50, 14.50],
                opens=[10.0, 11.0, 12.1, 13.31, 14.00, 14.50, 14.50, 14.50, 14.50, 14.50],
                amounts=[1e8] * 10, dates=dates, code="600001")
    other = _frame([10.0] * 10, dates=dates, code="600002")
    histories = {"600001": df, "600002": other}
    cfg = _cfg(new_stock_days=0)
    j = backfill(histories, dates, "2026-09-01", "2026-09-10", cfg)
    assert len(j) == 1
    row = j.iloc[0]
    assert row["signal_date"] == "20260904"
    assert row["entry_date"] == "20260905"
    assert row["status"] == "filled"
    assert row["fill_price"] == pytest.approx(14.0)
    assert row["sell_price"] == pytest.approx(14.5)
    rep = daily_report(j, cfg)
    assert rep["book_b"]["n_trades"] == 1
    assert rep["book_a"]["n_trades"] == 1


def test_load_histories_from_parquet(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    df = pd.DataFrame({
        "date": DATES[:5],
        "open": [10.0, 11.0, 12.1, 13.31, 14.64],
        "high": [10.1, 11.1, 12.2, 13.4, 14.7],
        "low": [9.9, 10.9, 12.0, 13.2, 14.5],
        "close": [10.0, 11.0, 12.1, 13.31, 14.64],
        "volume": [1000.0] * 5,
        "amount": [1e8] * 5,
    })
    df.to_parquet(daily / "sh600001.parquet", index=False)
    uni = tmp_path / "uni.json"
    uni.write_text(json.dumps({"600001": "示例股"}), encoding="utf-8")
    hist = load_histories(daily, uni)
    assert list(hist) == ["600001"]
    assert hist["600001"]["name"].iloc[0] == "示例股"
    assert hist["600001"]["date"].iloc[-1] == "2026-09-05"
