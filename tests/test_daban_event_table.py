"""Stage 0 事件表辅助函数测试（不读全量 parquet）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_event_table():
    spec = importlib.util.spec_from_file_location(
        "daban_event_table_under_test", ROOT / "scripts" / "daban_event_table.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ev = _load_event_table()


def test_as_date_str_handles_numeric_text_and_datetime():
    assert ev._as_date_str(pd.Series([20250905, 20250906])).tolist() == [
        "20250905", "20250906"]
    assert ev._as_date_str(pd.Series(["2025-09-05", "2025-09-06"])).tolist() == [
        "20250905", "20250906"]
    dt = pd.Series(pd.to_datetime(["2025-09-05"]))
    assert ev._as_date_str(dt).tolist() == ["20250905"]


def test_exit_price_open_mode_and_limit_down_defer():
    row = pd.Series({"next_open": 10.5, "next_close": 10.2,
                     "next_lim_dn": 9.0, "next2_open": 10.1})
    px, deferred = ev._exit_price(row, "open")
    assert px == pytest.approx(10.5)
    assert deferred is False

    row["next_open"] = 9.0
    px, deferred = ev._exit_price(row, "open")
    assert px == pytest.approx(10.1)
    assert deferred is True

    row["next_open"] = np.nan
    px, deferred = ev._exit_price(row, "open")
    assert np.isnan(px)
    assert deferred is False


def test_exit_price_close_mode_defers_to_next2_open():
    row = pd.Series({"next_open": 10.5, "next_close": 9.0,
                     "next_lim_dn": 9.0, "next2_open": 9.6})
    px, deferred = ev._exit_price(row, "close")
    assert px == pytest.approx(9.6)
    assert deferred is True

    row["next_close"] = 10.4
    px, deferred = ev._exit_price(row, "close")
    assert px == pytest.approx(10.4)
    assert deferred is False


def _bar_features() -> pd.DataFrame:
    rows = [
        # 三只银行股：一只封板、一只上涨、一只下跌
        ("600001", 11.0, False),
        ("600002", 10.5, False),
        ("600003", 9.8, False),
        # 无行业映射：只进市场统计
        ("600004", 10.1, False),
        # 价格异常样本：必须整体剔除
        ("600005", 11.0, True),
    ]
    return pd.DataFrame([
        {
            "code": code, "date": "20250905", "suspect_ref": suspect,
            "prev_close": 10.0, "limit": 11.0,
            "c0": c0, "c1": c0, "c2": c0, "c3": c0,
        }
        for code, c0, suspect in rows
    ])


def test_bar_stats_market_and_sector_at_each_closed_bar():
    industry = {"600001": "银行", "600002": "银行", "600003": "银行"}
    sector, market = ev._bar_stats(_bar_features(), industry)

    assert market["bar"].tolist() == [1, 2, 3, 4]
    assert sector["bar"].tolist() == [1, 2, 3, 4]

    m1 = market[market["bar"] == 1].iloc[0]
    assert m1["mkt_n"] == 4            # 剔除 suspect 后 4 只
    assert m1["mkt_limitup_n"] == 1
    assert m1["mkt_median_ret"] == pytest.approx(0.03)
    assert m1["mkt_breadth"] == pytest.approx(0.75)

    s1 = sector[sector["bar"] == 1].iloc[0]
    assert s1["sec_n"] == 3            # 无行业映射的 600004 不进板块
    assert s1["sec_limitup_n"] == 1
    assert s1["sec_median_ret"] == pytest.approx(0.05)
    assert s1["sec_leader_pct"] == pytest.approx(0.10)
    assert s1["sec_breadth"] == pytest.approx(2.0 / 3.0)
    assert s1["sec_rank_pct"] == pytest.approx(1.0)
    # 40 * 1.0 + 40 * 1/10 + 20 * 0.10/0.10
    assert s1["sec_score"] == pytest.approx(64.0)


def test_bar_stats_excludes_all_suspect_rows():
    frame = _bar_features()
    frame["suspect_ref"] = True
    sector, market = ev._bar_stats(frame, {"600001": "银行"})
    assert market.empty
    assert sector.empty
