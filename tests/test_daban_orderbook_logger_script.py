"""盯盘脚本 `scripts/daban_orderbook_logger.py` 的两个纯函数测试（不打网络）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_logger():
    spec = importlib.util.spec_from_file_location(
        "daban_orderbook_logger_under_test",
        ROOT / "scripts" / "daban_orderbook_logger.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


lg = _load_logger()


def _row(**overrides) -> dict:
    row = {
        "quote_time": "09:31:00", "code": "600000", "name": "测试股", "board": "主板10%",
        "status": "封板", "event": "SEAL", "price": 11.0, "limit_price": 11.0,
        "seal_amount": 110_000_000.0, "seal_shares": 10_000_000.0,
        "ask1_vol": 0.0, "consecutive": 1, "cancel_rate": 0.0, "score": 0.62,
        "is_yizi": 1, "seal_dead": 1, "break_count": 0, "cb": 1, "daily_last_date": "20261022",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------- 预警行渲染


def test_format_event_marks_seal_with_amount_and_flags():
    line = lg.format_event(_row())
    assert line.startswith("09:31:00 SEAL")
    assert "600000 测试股" in line
    assert "主板10%" in line
    assert "11.00/11.00" in line          # 现价/涨停价
    assert "封单1.10亿" in line
    assert "卖一0" in line
    assert "一字" in line
    assert "封死" in line
    assert "首板" in line


def test_format_event_break_line_shows_sell_side():
    line = lg.format_event(_row(
        status="炸板", event="BREAK", price=10.82, seal_amount=0.0, seal_shares=0.0,
        ask1_vol=500.0, is_yizi=0, seal_dead=0, break_count=1,
    ))
    assert "BREAK" in line
    assert "卖一500" in line
    assert "一字" not in line
    assert "封死" not in line
    assert "炸1次" in line


def test_format_event_reseal_heartbeat_shows_cancel_rate():
    line = lg.format_event(_row(
        status="回封", event="HEARTBEAT", cancel_rate=320.0, cb=2, consecutive=1,
    ))
    assert "HEARTBEAT" in line
    assert "撤单320" in line
    assert "2板" in line
    assert "首板" not in line


def test_format_event_prefers_cb_over_raw_consecutive():
    """cb 是东财口径（含今日），连板标记必须用它，不能用本地 consecutive。"""
    assert "3板+" in lg.format_event(_row(cb=3, consecutive=2))
    assert "首板" in lg.format_event(_row(cb=1, consecutive=0))


# ---------------------------------------------------------------- 自选池过滤


def test_filter_symbols_keeps_order_and_matches_bare_codes():
    symbols = ["sh600000", "sz000001", "bj920578"]
    assert lg.filter_symbols(symbols, ["600000"]) == ["sh600000"]
    assert lg.filter_symbols(symbols, ["000001", "600000"]) == ["sh600000", "sz000001"]


def test_filter_symbols_without_pool_returns_all():
    symbols = ["sh600000", "sz000001"]
    assert lg.filter_symbols(symbols, []) == symbols
    assert lg.filter_symbols(symbols, None) == symbols


def test_filter_symbols_drops_unknown_pool_codes():
    assert lg.filter_symbols(["sh600000"], ["600999"]) == []


# ---------------------------------------------------------------- 本地日线日期


def test_daily_last_date_reader_returns_compacted_max_date(tmp_path):
    pytest.importorskip("pyarrow")
    import pandas as pd

    pd.DataFrame({"date": ["2026-10-21", "2026-10-22"]}).to_parquet(
        tmp_path / "sh600000.parquet")
    read = lg.make_daily_last_date_reader(tmp_path)
    assert read("600000") == "20261022"
    assert read("600000") == "20261022"      # 第二次走缓存
    assert read("600999") == ""              # 本地整只票都没有
