"""新浪 L1 34 段布局的字段回归测试。

payload 全部取自 2026-10-23 盘后真实报文，并用腾讯源（qt.gtimg.cn，五档单位=手）
逐档交叉验证过：

| 代码 | 新浪 [10] 买一量(股) | 腾讯 买一量(手) | 新浪 [8] 成交量(股) | 腾讯 成交量(手) |
|---|---|---|---|---|
| sh600094 | 4681923 | 46819 | 48170642 | 481706 |
| sh600187 | 6162900 | 61629 | 70664561 | 706646 |
| sh600838 | 9936535 | 99365 | 21359365 | 213594 |
| sz002164 | 9300 | 93 | 95556299 | 955563 |

结论：**8/9 是成交量/成交额**，买一量在 10、买一价在 11，档位顺序是"量,价"。
"""
from __future__ import annotations

import pytest

from tdxfeed.daban.realtime import _parse_line

DACHENG = (
    "大名城,3.950,3.990,4.390,4.390,3.900,4.390,0.000,48170642,201853488.000,"
    "4681923,4.390,458300,4.380,21600,4.370,139600,4.360,42800,4.350,"
    "0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,"
    "2026-10-23,15:00:02,00,"
)
DONGLI = (
    "宁波东力,16.000,16.600,17.780,18.260,15.450,17.770,17.780,95556299,1612386069.820,"
    "9300,17.770,2400,17.760,6100,17.750,400,17.740,1200,17.720,"
    "458000,17.780,2000,17.790,56700,17.800,20500,17.810,18300,17.820,"
    "2026-10-23,15:35:15,00,D|102700|1826006.000"
)
JIUBAI = (
    "上海九百,11.000,10.000,11.000,11.000,11.000,11.000,0.000,21359365,234953015.000,"
    "9936535,11.000,10400,10.990,11900,10.980,10800,10.970,3700,10.960,"
    "0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,"
    "2026-10-23,15:34:59,00,D|700|7700.00"
)


def test_volume_and_amount_come_from_fields_8_and_9():
    q = _parse_line("sh600094", DACHENG)
    assert q.volume == pytest.approx(48_170_642)
    assert q.amount == pytest.approx(201_853_488.0)


def test_seal_is_bid1_volume_not_traded_volume():
    """封单量 = 买一量（468 万股），不是当天成交量（4817 万股）。"""
    q = _parse_line("sh600094", DACHENG)
    assert q.seal_shares == pytest.approx(4_681_923)
    assert q.seal_amount == pytest.approx(4_681_923 * 4.39)
    assert q.bid1_vol == pytest.approx(4_681_923)
    assert q.bid1_price == pytest.approx(4.39)


def test_bids_are_volume_then_price_pairs_descending():
    q = _parse_line("sh600094", DACHENG)
    assert [round(p, 3) for p, _ in q.bids] == [4.39, 4.38, 4.37, 4.36, 4.35]
    assert [v for _, v in q.bids] == [4_681_923, 458_300, 21_600, 139_600, 42_800]


def test_asks_empty_when_sealed():
    q = _parse_line("sh600094", DACHENG)
    assert q.asks == []
    assert q.ask1_vol == 0.0


def test_asks_five_levels_for_broken_name():
    q = _parse_line("sz002164", DONGLI)
    assert [round(p, 3) for p, _ in q.asks] == [17.78, 17.79, 17.80, 17.81, 17.82]
    assert [v for _, v in q.asks] == [458_000, 2_000, 56_700, 20_500, 18_300]
    assert [round(p, 3) for p, _ in q.bids] == [17.77, 17.76, 17.75, 17.74, 17.72]
    assert q.bid1_vol == pytest.approx(9_300)


def test_ohlc_and_timestamp():
    q = _parse_line("sz002164", DONGLI)
    assert (q.open, q.prev_close, q.price, q.high, q.low) == (16.0, 16.6, 17.78, 18.26, 15.45)
    assert q.date == "2026-10-23"
    assert q.time == "15:35:15"


def test_yizi_board_seal_is_the_whole_queue_at_limit():
    """一字板：买卖档全空，封单 993.6 万股 × 11.00 = 1.09 亿。"""
    q = _parse_line("sh600838", JIUBAI)
    assert q.open == q.high == q.low == q.price == pytest.approx(11.0)
    assert q.seal_shares == pytest.approx(9_936_535)
    assert q.seal_amount == pytest.approx(9_936_535 * 11.0)
    assert q.volume == pytest.approx(21_359_365)
    assert q.ask1_vol == 0.0


def test_pct_from_prev_close():
    q = _parse_line("sh600094", DACHENG)
    assert q.pct() == pytest.approx(10.025, abs=1e-3)
