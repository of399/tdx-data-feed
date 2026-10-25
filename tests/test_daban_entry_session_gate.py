"""盘中轮询的门禁：时段、行情自带日期、板块白名单，以及「不碰任何实盘账本」。

`daban-ticket.timer` 每 5 分钟命中 `Mon..Fri 09..11,13..14`，共 60 次触发里只有约 48 次落在
连续竞价（`systemd.time` 里分钟级区间 `09:25..11:25/5:00` 解析不了，实测报「无效的参数」，
所以粗放交给小时、精确交给脚本里的 `current_session()`）。这些门必须**早于任何网络调用**，
否则午间休市也在打新浪行情，一天多打几十轮无意义的请求。

板块过滤放在拉行情**之前**：北交所被踢出试验仓的理由是 `build_intents` 只有一个全局
`lot=100`（`tdxfeed/daban/order.py:157`），本设计不改它，所以只对「100 股整手」成立的板块出票。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_entry():
    fp = ROOT / "scripts" / "daban_entry.py"
    assert fp.exists(), f"{fp} 不存在"
    espec = importlib.util.spec_from_file_location("daban_entry_under_test", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


@dataclass
class Cand:
    code: str
    name: str
    limit_price: float
    score: float = 0.6
    sector_name: str = "板块"
    reasons: tuple = ("封板",)


class Env:
    """替掉真实扫描/行情层：记录构造参数与每次 fetch 的标的，不碰网络也不读 parquet。

    `trade_date` 用 `Scanner._trade_date()` 的**真实形状**（新浪 `Quote.date` 是 `2026-10-26`
    带横杠，缺日期时才回退成 `%Y%m%d`）。紧凑格式当默认值会让「交易日门」这条守卫永远测的是
    我自己的假设而不是上游的口径——2026-10-25 实测就是这个形状不一致。
    """

    SYMBOLS: ClassVar[list[str]] = ["sh600001", "sz300002", "bj920001", "sh600003"]

    def __init__(self, cands=None, trade_date="2026-10-26", with_tencent=True):
        self.trade_date = trade_date
        self.cands = cands if cands is not None else []
        self.with_tencent = with_tencent
        self.fetches: list[list[str]] = []
        self.scanner_kwargs: dict | None = None
        self.scan_calls = 0

    def fetch(self, symbols, **kw):
        self.fetches.append(list(symbols))
        return {s: SimpleNamespace(date=self.trade_date) for s in symbols}

    def build_scanner(self, **kwargs):
        self.scanner_kwargs = kwargs
        outer = self

        class FakeScanner:
            # 入口脚本用 `getattr(scanner, "tquotes")` 判涨停价来源（腾讯源挂了会静默回退），
            # 所以这里必须真的暴露它，否则「来源」那条守卫测的是空对象。
            @property
            def tquotes(self):
                return {s: object() for s in Env.SYMBOLS} if outer.with_tencent else {}

            def symbols(self):
                return list(Env.SYMBOLS)

            def scan(self, quotes):
                outer.scan_calls += 1
                return SimpleNamespace(
                    trade_date=outer.trade_date, candidates=outer.cands, stats={}
                )

        return FakeScanner()


def make_env(cands=None, trade_date="2026-10-26", with_tencent=True):
    return Env(cands, trade_date, with_tencent)


def _latest_json(tmp_path) -> str:
    files = sorted((tmp_path / "data" / "daban" / "tickets").glob("ticket_*.json"))
    assert files, "没有生成委托单 json"
    return files[-1].read_text(encoding="utf-8")


def _day_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "data" / "daban" / "tickets").glob("ticket_2026*"))


def test_cli_at_overrides_the_clock_for_off_hours_rehearsal(tmp_path, monkeypatch):
    """`--at` 只改判据时刻，不改产出口径——盘中每 5 分钟的定时器用的还是系统时钟。

    没有它就只能等到 09:30 才发现真实 Scanner/行情层的形状问题；2026-10-25 那次
    `trade_date` 形状不一致（`2026-10-26` vs `20261026`）就是靠这条入口在周日复现的。
    """
    mod = _load_entry()
    seen = {}

    def fake_poll(root, now=None, *, phase=mod.PHASE, **kw):
        seen["root"] = root
        seen["now"] = now
        seen["phase"] = phase
        return 7

    monkeypatch.setattr(mod, "poll_once", fake_poll)
    rc = mod.main(["--root", str(tmp_path), "--at", "2026-10-26T10:00"])
    assert rc == 7, "CLI 要把 poll_once 的返回码原样带出去（非 0 才能让 OnFailure= 叫）"
    assert seen["now"] == datetime(2026, 10, 26, 10, 0), seen
    assert Path(seen["root"]) == tmp_path
    assert seen["phase"] == mod.PHASE


def test_cli_defaults_to_the_system_clock(tmp_path, monkeypatch):
    mod = _load_entry()
    seen = {"called": False}

    def fake_poll(root, now=None, **kw):
        seen["called"] = True
        seen["now"] = now
        return 0

    monkeypatch.setattr(mod, "poll_once", fake_poll)
    assert mod.main(["--root", str(tmp_path)]) == 0
    assert seen["called"] and seen["now"] is None, "不给 --at 时不能塞一个假时刻进去"


def test_dash_form_quote_date_lands_on_the_compact_day_file(tmp_path):
    """行情给 `2026-10-26`，日文件/日累计必须是 `ticket_20261026.*`。

    不归一化的话两种形状会在两处各自成立：门禁永远判「不是今天」（一天票出不来），
    或者放行后写出 `ticket_2026-10-26.md`（`daban_fill.py` 与停止线按 `ticket_YYYYMMDD` 找对象，
    台账就接不上）。
    """
    mod = _load_entry()
    env = make_env([Cand(f"60000{i}", f"票{i}", 10.0) for i in range(6)])

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    names = _day_files(tmp_path)
    assert "ticket_20261026.md" in names and "ticket_20261026.json" in names, names
    payload = json.loads(_latest_json(tmp_path))
    assert payload["trade_date"] == "20261026", payload


def poll(mod, tmp_path, env, **kw):
    return mod.poll_once(tmp_path, build_scanner=env.build_scanner, fetch=env.fetch, **kw)


def test_auction_lunch_and_closed_never_touch_the_market_layer(tmp_path):
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)])
    # 09:20 竞价、12:00 午休、15:30 收盘、08:50 盘前
    for moment in (
        datetime(2026, 10, 26, 9, 20),
        datetime(2026, 10, 26, 12, 0),
        datetime(2026, 10, 26, 15, 30),
        datetime(2026, 10, 26, 8, 50),
    ):
        rc = poll(mod, tmp_path, env, now=moment)
        assert rc == 0, moment
    assert env.fetches == [], "非连续竞价也去拉行情了"
    assert env.scanner_kwargs is None, "非连续竞价也构造了 Scanner"
    assert not list((tmp_path / "data" / "daban" / "tickets").glob("ticket_*"))


def test_stop_marker_short_circuits_before_any_network_call(tmp_path):
    from tdxfeed.daban import trial

    trial.write_marker(tmp_path, "🛑 命中第 1 条测试")
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)])

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    assert env.fetches == []
    assert env.scanner_kwargs is None


def test_continuous_poll_filters_beijing_before_fetching_quotes(tmp_path):
    mod = _load_entry()
    env = make_env([Cand(f"60000{i}", f"票{i}", 10.0) for i in range(6)])

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    assert len(env.fetches) == 1
    passed = env.fetches[0]
    assert "bj920001" not in passed, passed
    assert "sh600001" in passed and "sz300002" in passed


def test_poll_uses_the_agreed_trial_book_parameters(tmp_path):
    """§3 的参数必须真传下去：不放宽 ST、门槛 4%、环境标签默认震荡市。"""
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)])

    poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    kw = env.scanner_kwargs
    assert kw["allow_st"] is False
    assert kw["min_pct"] == 4.0
    assert kw["phase"] == "震荡市"
    assert kw["session_mode"] == "auto"


def test_quote_date_not_today_skips_the_whole_poll(tmp_path):
    """行情自带日期（交易所口径）不等于本地今天 ⇒ 节假日/滞后，整轮跳过且不留文件。"""
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)], trade_date="2026-10-23")

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    assert not list((tmp_path / "data" / "daban" / "tickets").glob("ticket_*"))


def test_empty_candidates_writes_no_ticket_file(tmp_path):
    mod = _load_entry()
    env = make_env([])

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    # 扫描确实跑了、只是没出候选——否则这条守卫测的是「门挡住了」而不是「空榜不落文件」。
    assert env.scan_calls == 1
    assert not list((tmp_path / "data" / "daban" / "tickets").glob("ticket_*"))


def test_limit_price_source_is_recorded_per_ticket(tmp_path):
    """腾讯源挂了会静默回退（scanner.py:394），涨停价来源必须逐票落到单子上，人工才看得出是回退值。"""
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)], with_tencent=False)

    rc = poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    assert rc == 0
    payload = _latest_json(tmp_path)
    assert "新浪L1推算" in payload, payload


def test_limit_price_source_marks_tencent_when_present(tmp_path):
    mod = _load_entry()
    env = make_env([Cand("600001", "甲", 10.0)])

    poll(mod, tmp_path, env, now=datetime(2026, 10, 26, 10, 0))
    payload = _latest_json(tmp_path)
    assert "腾讯官方" in payload, payload
