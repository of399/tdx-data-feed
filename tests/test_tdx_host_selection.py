"""通达信侧主机选择的守卫（②批次）。

钉住的判据：
  G1 选出的主机必须是**实测可达**的那台（不是 `hq_hosts[0]`）
  G2 探活超时与业务请求超时是**不同的数**，且都必须单源（不许在调用点写死）
  G3 「TCP 可达」不等于「这一通道能用」：本环境实测 40 台里 8 台 TCP 通、
     **0 台吐 K 线**，而同一台 F10 通道可用 ⇒ 探活分档（connect / bars / f10），
     吐空判失败并计数
  G4 其它模块不得自己取 `hq_hosts[0]`、不得自带轮询表（入口单源）
  G5 mootdx 的 `probe()` 拿到空帧时必须计数（今天实测 `_ERROR_COUNT={}`）

全部用假候选表 + 假 socket + 假 api 工厂，**不打网络**，`NEEDS_DATA = False`，CI 上也得跑。
"""
from __future__ import annotations

import ast
import importlib
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NEEDS_DATA = False

from tdxfeed.sources import mootdx_source as MS, tdx_hosts as H  # noqa: E402

HOSTS = [
    ("死机A", "10.0.0.1", 7709),      # TCP 超时
    ("拒连机B", "10.0.0.2", 7709),     # TCP 通、协议 connect 返回假
    ("空通道C", "10.0.0.3", 7709),     # 协议通、bars 吐空、F10 有内容
    ("好机D", "10.0.0.4", 7709),       # 全通
]
TCP_ALIVE = {"10.0.0.2", "10.0.0.3", "10.0.0.4"}


class FakeApi:
    """假 pytdx 客户端：按连上的 IP 决定这台机器吐不吐数据。"""

    def __init__(self, bars_empty_everywhere: bool = False):
        self.connected_to = None
        self.connect_kwargs: dict = {}
        self.bars_empty_everywhere = bars_empty_everywhere

    def connect(self, ip, port, *, time_out=None):
        self.connect_kwargs = {"ip": ip, "port": port, "time_out": time_out}
        self.connected_to = ip
        return ip != "10.0.0.2"                      # B：协议层拒绝

    def get_security_bars(self, category, market, code, start, offset):
        if self.bars_empty_everywhere or self.connected_to == "10.0.0.3":
            return []                                 # 连上了但 0 根
        return [{"date": "2026-11-05", "close": 10.0}] * 3

    def get_company_info_category(self, market, code):
        # 真 pytdx 返回的是带 filename/start/length 的栏目字典，假件必须同形，
        # 否则选择器读不到正文位置就会「误判这台机器 F10 不可用」
        return [{"name": "最新提示", "filename": "000600main.txt", "start": 0, "length": 8146}]

    def get_company_info_content(self, market, code, filename, start, length):
        return "【特别处理】\n｜公告日期｜2026-04-22"

    def disconnect(self):
        self.connected_to = None


class _FakeSock:
    """真 socket 支持 `with`（3.6+），假件也得支持，否则测不出生产写法。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _install(monkeypatch, calls, *, bars_empty_everywhere=False):
    monkeypatch.setattr(H, "raw_hosts", lambda: list(HOSTS))

    def _create_connection(addr, timeout=None):
        calls["tcp"].append({"addr": tuple(addr), "timeout": timeout})
        if addr[0] not in TCP_ALIVE:
            raise TimeoutError("timed out")     # 真 socket 今天抛的就是这个（socket.timeout 是别名）
        return _FakeSock()

    monkeypatch.setattr(H.socket, "create_connection", _create_connection)

    def _factory(*args, **kwargs):
        api = FakeApi(bars_empty_everywhere)
        calls["api"].append(api)
        return api

    monkeypatch.setattr(H, "hq_api", _factory)
    H.reset_stats()


@pytest.fixture
def calls(monkeypatch):
    c: dict = {"tcp": [], "api": []}
    _install(monkeypatch, c)
    return c


@pytest.fixture
def dead_bars(monkeypatch):
    """本环境的真实形状：TCP 能通、协议能连，K 线一律 0 根。"""
    c: dict = {"tcp": [], "api": []}
    _install(monkeypatch, c, bars_empty_everywhere=True)
    return c


# G1 -------------------------------------------------------------------------
def test_selects_the_host_that_actually_serves_bars(calls) -> None:
    sel = H.select_server(want="bars")
    assert sel is not None
    assert (sel["ip"], sel["port"], sel["name"]) == ("10.0.0.4", 7709, "好机D"), sel
    assert sel["tried"] == 4, "四台都该探过（不能 TCP 一通就收工）"


def test_connect_tier_stops_at_first_reachable_handshake(calls) -> None:
    sel = H.select_server(want="connect")
    assert sel["ip"] == "10.0.0.3", sel          # B 协议失败 → C 通过
    assert sel["tried"] == 3


def test_control_static_first_host_choice_is_red(calls) -> None:
    """变异对照：实现若退回「取候选表第一台」，这条必红（=今天的生产形状）。"""
    sel = H.select_server(want="bars")
    assert sel["ip"] != HOSTS[0][1], "选中的仍是 hq_hosts[0] ⇒ 可达性驱动没接线"


def test_all_dead_returns_none_and_counts_reasons(calls) -> None:
    H.reset_stats()
    before = sum(H._ERROR_COUNT.values())          # 累计器：只看这一轮的增量
    sel = H.select_server(want="bars", only=("死机A",))
    assert sel is None
    stats = H.last_stats()
    assert stats["reasons"]["tcp_timeout"] == 1, stats
    assert sum(stats["swallowed"].values()) - before >= 1, \
        f"TCP 探活失败没进吞错计数（SEM002 口径）: {stats}"


# G2 -------------------------------------------------------------------------
def test_probe_and_request_timeouts_are_distinct_numbers() -> None:
    trio = {H.CONNECT_PROBE_TIMEOUT_SEC, H.DATA_PROBE_TIMEOUT_SEC, H.REQUEST_TIMEOUT_SEC}
    assert len(trio) == 3, "三个数必须互不相等（KI-OPS-007 同因：探活砍断长请求）"
    assert H.CONNECT_PROBE_TIMEOUT_SEC < H.REQUEST_TIMEOUT_SEC


def test_probe_calls_actually_use_probe_timeouts(calls) -> None:
    H.select_server(want="bars")
    assert {c["timeout"] for c in calls["tcp"]} == {H.CONNECT_PROBE_TIMEOUT_SEC}, calls["tcp"]
    used = {a.connect_kwargs["time_out"] for a in calls["api"] if a.connect_kwargs}
    assert used == {H.DATA_PROBE_TIMEOUT_SEC}, [a.connect_kwargs for a in calls["api"]]


def test_mootdx_client_uses_request_timeout(monkeypatch) -> None:
    seen: dict = {}

    class FakeQuotes:
        @staticmethod
        def factory(**kw):
            seen.update(kw)
            return object()

    monkeypatch.setattr(MS, "Quotes", FakeQuotes)
    monkeypatch.setattr(MS, "select_server", lambda **kw: {
        "name": "好机D", "ip": "10.0.0.4", "port": 7709, "tried": 4})
    MS.MootdxSource()
    assert seen["server"] == ("10.0.0.4", 7709), seen
    assert seen["timeout"] == H.REQUEST_TIMEOUT_SEC, seen


def test_no_reachable_host_raises_counted_instead_of_bestip(monkeypatch) -> None:
    """无路时必须**抛 + 计数**，不许悄悄退回 mootdx bestip 再造一个客户端。

    原先的 else 分支就是「静默降级」的形状：探活全失败仍然构造 client，调用方看到的
    只是后面某次 `probe() -> False`，中间没有任何地方说「为什么无路」。
    """
    seen: dict = {}

    class FakeQuotes:
        @staticmethod
        def factory(**kw):
            seen.update(kw)
            return object()

    monkeypatch.setattr(MS, "Quotes", FakeQuotes)
    monkeypatch.setattr(MS, "select_server", lambda **kw: None)
    MS._ERROR_COUNT.clear()
    with pytest.raises(RuntimeError, match="无可用主机"):
        MS.MootdxSource()
    assert sum(MS._ERROR_COUNT.values()) >= 1, "失败没计数"
    assert seen == {}, f"无路时仍构造了 bestip 客户端: {seen}"


# G3 -------------------------------------------------------------------------
def test_tcp_reachable_but_empty_channel_is_rejected_and_counted(dead_bars) -> None:
    """40 台里 8 台 TCP 通、0 台吐 K 线 ⇒ want='bars' 必须判无路，并把原因记全。"""
    sel = H.select_server(want="bars")
    assert sel is None, "把「连上了」读成「能用」就是这条守卫要拦的"
    reasons = H.last_stats()["reasons"]
    assert reasons["bars_empty"] == 2, reasons       # C 与 D 都吐空
    assert reasons["connect_false"] == 1, reasons    # B
    assert reasons["tcp_timeout"] == 1, reasons      # A


def test_f10_channel_on_same_host_is_still_selectable(calls) -> None:
    """同一台不吐 K 线的机器，F10 档必须仍可选（否则面板采集被误判为无路）。"""
    sel = H.select_server(want="f10")
    assert sel["ip"] == "10.0.0.3", sel
    assert sel["tried"] == 3


# G4 -------------------------------------------------------------------------
def test_no_module_takes_hq_hosts_first_entry() -> None:
    """AST 判据（不是文本）：注释里提一句 `hq_hosts[0]` 不该被判红，代码里取才算。

    写法沿用仓库其它守卫的做法：解析 AST、只认 `Subscript(hq_hosts, 0)` 这一形状。
    """
    offenders = []
    for rel in ("tdxfeed/sources/mootdx_source.py", "scripts/collect_st_history.py",
                "tdxfeed/full_sync.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name) and node.value.id == "hq_hosts"
                    and isinstance(node.slice, ast.Constant) and node.slice.value == 0):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"仍在代码里取第一台：{offenders}"


def test_no_module_defines_its_own_host_polling() -> None:
    """轮询逻辑只能在 tdx_hosts 里：别的模块不许再 import hq_hosts 自己扫。"""
    bad = []
    for rel in ("tdxfeed/sources/mootdx_source.py", "scripts/collect_st_history.py",
                "tdxfeed/full_sync.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                txt = ast.unparse(node)
                if "hq_hosts" in txt:
                    bad.append(f"{rel}:{node.lineno} {txt}")
    assert not bad, "主机候选表只能在 tdx_hosts 里取：" + " | ".join(bad)


def test_collector_delegates_to_the_selector() -> None:
    src = (ROOT / "scripts" / "collect_st_history.py").read_text(encoding="utf-8")
    assert "tdx_hosts" in src, "采集脚本没接选择器"
    sys.path.insert(0, str(ROOT / "scripts"))
    mod = importlib.import_module("collect_st_history")
    assert mod.SCAN_LIMIT == H.DEFAULT_SCAN_LIMIT, "轮询上限没走单源常量"


# G5 -------------------------------------------------------------------------
def test_mootdx_probe_counts_an_empty_frame(monkeypatch) -> None:
    class _DF:
        empty = True

    monkeypatch.setattr(MS, "_run_with_timeout", lambda *a, **k: _DF())
    src = MS.MootdxSource.__new__(MS.MootdxSource)
    src.client = object()
    MS._ERROR_COUNT.clear()
    assert src.probe() is False
    assert sum(MS._ERROR_COUNT.values()) >= 1, "空返回没被计数 ⇒ 「通道活着但吐空」在统计里隐身"
