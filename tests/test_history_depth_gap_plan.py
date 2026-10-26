"""审计的分类逻辑：不能按「本地行数 ≤1100」猜谁被截断了。

§0 第 2 条实测：112 只「行数像被钉住」的老代码段候选里，sh600925/600930/600938/601022
是真的 2022 年后上市（探测窗 1990-01-01~2000-12-31 返回 0 行），只有 sh600000 这类才是
真缺口。分类必须同时看「本地起始日」和「探测窗首行」。

另一半是审计自己的范围不能静默截断：universe 里有创业板 1407 只、科创板 618 只，
按 c[:2] in ("00","60") 过滤会把这 39% 直接排除在「谁需要回填」之外——和被审计的
毛病同一类，所以 sym_of_a_share/a_share_symbols 单独有测试钉住。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tdxfeed.history_paging import PageRequestError

ROOT = Path(__file__).resolve().parent.parent
TARGET = 20000101


def _load():
    fp = ROOT / "scripts" / "audit_daily_history_depth.py"
    espec = importlib.util.spec_from_file_location("audit_depth_under_test", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


AUD = _load()


def test_local_start_after_target_but_source_has_earlier_is_extend():
    r = AUD.plan_gaps(20220719, 20261023, 19991110, TARGET)
    assert r["state"] == "extend"
    assert r["missing_days_est"] > 5000


def test_source_has_nothing_before_target_is_a_real_late_listing():
    r = AUD.plan_gaps(20230329, 20261023, None, TARGET)
    assert r["state"] == "listed_after_target"
    assert r["missing_days_est"] == 0


def test_already_deep_enough_is_ok():
    assert AUD.plan_gaps(19940202, 20261023, 19940202, TARGET)["state"] == "ok"


def test_request_failure_is_unknown_not_ok():
    """探测被拒（-1 来自异常）绝不能被归成「不用补」。"""
    assert AUD.plan_gaps(20220719, 20261023, -1, TARGET)["state"] == "unknown"


def test_missing_local_file_counts_as_full_extend():
    r = AUD.plan_gaps(None, None, 19930104, TARGET)
    assert r["state"] == "extend"


def test_symbol_prefix_covers_chinext_and_star():
    """60/68 → sh，00/30 → sz。漏掉 30x/68x 就是审计自己截断范围。"""
    assert AUD.sym_of_a_share("600000") == "sh600000"
    assert AUD.sym_of_a_share("688981") == "sh688981"
    assert AUD.sym_of_a_share("000001") == "sz000001"
    assert AUD.sym_of_a_share("300750") == "sz300750"
    assert AUD.sym_of_a_share("001872") == "sz001872"
    assert AUD.sym_of_a_share("123456") is None       # 北交所：本环境无数据，不纳入
    assert AUD.sym_of_a_share("900999") is None       # B 股：不是 A 股 universe
    syms = AUD.a_share_symbols({"600000": "x", "300750": "y", "688981": "z",
                                 "000001": "w", "123456": "bj"})
    assert syms == ["sh600000", "sh688981", "sz000001", "sz300750"]


def test_local_already_deep_skips_the_probe(monkeypatch):
    """本地首行已在 target 之前 = 不用问源；实测 5221 只里 747 只走这一条，
    白问这 747 次会拖慢审计约 11 分钟，也可能引入假 unknown。"""
    monkeypatch.setattr(AUD, "local_span", lambda s: (19940202, 20261023))

    def never_probed(sym):
        raise AssertionError("ok 的标的不应触发网络探测")

    r = AUD.audit_one("sh600000", TARGET, probe=never_probed)
    assert r["state"] == "ok"
    assert r["probed"] is False


def test_refused_probe_is_unknown_and_not_silently_ok(monkeypatch):
    monkeypatch.setattr(AUD, "local_span", lambda s: (20220719, 20261023))

    def refused(sym):
        raise PageRequestError("param error")

    r = AUD.audit_one("sz000021", TARGET, probe=refused)
    assert r["state"] == "unknown"
    assert r["probed"] is True
    assert r["error"]


def test_empty_probe_is_late_listing_not_unknown(monkeypatch):
    monkeypatch.setattr(AUD, "local_span", lambda s: (20230329, 20261023))
    r = AUD.audit_one("sh600925", TARGET, probe=lambda s: [])
    assert r["state"] == "listed_after_target"
    assert r["error"] is None


def test_probe_rows_become_the_earliest_visible_date(monkeypatch):
    monkeypatch.setattr(AUD, "local_span", lambda s: (20220719, 20261023))
    r = AUD.audit_one("sh600000", TARGET,
                      probe=lambda s: [["1999-11-10", "1", "1", "1", "1", "1"]])
    assert r["state"] == "extend"
    assert r["source_first"] == 19991110
