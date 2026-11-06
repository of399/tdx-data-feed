"""通达信行情主机选择：可达性驱动，且**探活按通道分档**（②批次）。

为什么不是 `hq_hosts[0]`
----------------------
`tdxfeed/sources/mootdx_source.py` 原先取 pytdx 静态表第一台，2026-11-05 本机实测：

```
hq_hosts[0] = ('长城国瑞电信1', '218.85.139.19', 7709)
  TCP 探活(3s)            -> TimeoutError: timed out   （3004ms）
  pytdx connect(time_out=3) -> False                   （3.00s）
  MootdxSource() 构造      -> 成功，但用时 15.02s        （Quotes.factory(timeout=15) 把
                                                         不可达主机的握手等满）
  probe()                  -> False，且 _ERROR_COUNT = {}   ← 空返回不计数
```

换成实测可达的主机（手工指定 `ip:port`）后构造只要 **0.11–0.15s**。也就是说「整条通达信路由为空、
全程回落 Sina」里**有一整段是这一台死机器造成的 15 秒白等**，不是策略、是 bug。
但注意：修掉选主**只**消掉这段白等——`want="bars"` 的选主在本机仍然返回 `None`（见下），
所以 `MootdxSource()` 在 K 线用途上按契约直接抛 `RuntimeError` 带原因统计，由调用方回落 Sina。

TCP 可达 ≠ 这一通道能用：能力是分档的
------------------------------------
2026-11-05 对 `hq_hosts` **前 14 台逐台分三档探活**的实测（同一台机器上分别验 connect / bars / f10）：

```
connect（TCP + 协议握手）  = 6/14   ← 长城国瑞 3 台、上证云 2 台、沪电信 Z2、京联通 Z1/Z2 全连不上
bars（真 K 线非空）         = 0/14   ← 6 台连上的机器 get_security_bars / get_index_bars 一律 0 根
f10（栏目 + 正文非空）      = 6/14   ← 与 connect 可用的那 6 台完全同一批
可用主机 = 180.153.18.170 / 180.153.18.172:80 / 202.108.253.139:80 / 60.191.117.167 / 115.238.56.198 / 218.75.126.9
select_server(want="bars") = None，原因统计 {tcp_timeout: 6, tcp_refused: 2, bars_empty: 6}，试 14 台 / 12.1s
```

**由此得出的能力分档结论（下一轮别再去换主机白耗一天）**：
- **可用端点**：F10 段落（`get_company_info_category` + `get_company_info_content`）与
  板块文件（`get_and_parse_block_info("block_gn.dat")`）——本仓库的 ST 台账与当日成员就是走这两条。
- **不可用端点**：K 线（个股日线、指数日线）**一台都不吐**，与选主无关：
  上面那 6 台是「连得上、F10 有正文、K 线 0 根」同一批。
- **所以日线/指数的权威通道仍然是 Sina**（`tdxfeed/sources/sina_source.py` 那条兜底路径
  在这台机器上其实是**主路径**）。换主机、改选主策略都不会把通达信侧的 K 线接回来；
  要接回来得找别的数据源或别的网络出口，那是另一件事。
- 因此本模块这一修的**作用域要说清**：它改善的是**失败可见性**（原因计数替代一行 WARN）
  与**选主正确性**（不再把 15 秒白等花在一台死机器上、不再把 TCP 通当成可用），
  **不是**把日线接回来。守卫 `test_no_reachable_host_raises_counted_instead_of_bestip`
  与 `test_tcp_reachable_but_empty_channel_is_rejected_and_counted` 钉的就是这两条。

所以探活必须**按要用的通道分档**：`want="connect"`（只验握手）/ `"bars"`（要真 K 线）/
`"f10"`（要正文）。把「TCP 通」读成「通达信路由恢复」是这条模块存在的理由。

三个超时，不是一个
------------------
`CONNECT_PROBE_TIMEOUT_SEC`（TCP 握手）< `DATA_PROBE_TIMEOUT_SEC`（协议连接 + 一次取数）
< `REQUEST_TIMEOUT_SEC`（业务请求）。三者各一个数、都在本文件单源，调用点不许写死。
成因与 `KI-OPS-007`（baostock 登录握手被 30 秒上限砍断）相同，只是方向相反：
**探活超时长了会拖死整轮选择，业务超时短了会砍断正常请求**。

不做什么
--------
不做 `bestip` 全表扫描（104 台 × 探活 = 分钟级，且 mootdx 的 bestip 在本环境失败过，
见本模块历史 docstring）；默认只扫 `DEFAULT_SCAN_LIMIT` 台并按命中缓存；
不吞错：每个失败形状都按原因计数（SEM002）。
"""
from __future__ import annotations

import os
import socket
import time
from collections import Counter
from collections.abc import Callable

# ─────────────────────────────── 三个超时（单源） ───────────────────────────────
CONNECT_PROBE_TIMEOUT_SEC = float(os.environ.get("TDX_PROBE_CONNECT_TIMEOUT", "1.5"))
DATA_PROBE_TIMEOUT_SEC = float(os.environ.get("TDX_PROBE_DATA_TIMEOUT", "3.0"))
REQUEST_TIMEOUT_SEC = float(os.environ.get("TDX_REQUEST_TIMEOUT", "15"))

DEFAULT_SCAN_LIMIT = int(os.environ.get("TDX_HOST_SCAN_LIMIT", "14"))
WANTS = ("connect", "bars", "f10")

# 探活用的样板标的：沪市主板龙头，任何活着的主机都该有它的少量数据
PROBE_MARKET = 1
PROBE_CODE = "600000"

_ERROR_COUNT: Counter[str] = Counter()          # 键 = 函数名（SEM002 口径）
_STATS: dict = {"reasons": Counter(), "tried": 0, "selected": None, "elapsed_sec": 0.0}

try:  # pytdx 是可选依赖：装不上就整模块判「无路」，不抛、但要计数
    from pytdx.config.hosts import hq_hosts as _HQ_HOSTS
    from pytdx.hq import TdxHq_API as hq_api
except Exception:  # pragma: no cover - 环境缺依赖时的退路
    _ERROR_COUNT["<module>"] += 1
    _HQ_HOSTS = []
    hq_api = None  # type: ignore[assignment]


def raw_hosts() -> list[tuple[str, str, int]]:
    """pytdx 静态候选表（原序）。只有这里允许读 `hq_hosts`，别的模块一律走本文件。"""
    return [(str(n), str(ip), int(port)) for n, ip, port in _HQ_HOSTS]


def reset_stats() -> None:
    _STATS.update({"reasons": Counter(), "tried": 0, "selected": None, "elapsed_sec": 0.0})


def last_stats() -> dict:
    """上一轮选择的可审计结果：每台机器为什么被跳过。"""
    return {"reasons": dict(_STATS["reasons"]), "tried": _STATS["tried"],
            "selected": _STATS["selected"], "elapsed_sec": round(_STATS["elapsed_sec"], 2),
            "swallowed": dict(_ERROR_COUNT)}


def _note(reason: str) -> None:
    _STATS["reasons"][reason] += 1


def _tcp_open(ip: str, port: int) -> bool:
    """第一档：TCP 握手。超时/拒绝分别计数，别都写成「连不上」。

    `_note(...)` 是按原因分桶，`_ERROR_COUNT` 是本仓 SEM002 口径的吞错计数
    （守卫只认「递增带 count/error 字样的名字」或调用 `note(...)` 这类函数，
    私有函数带下划线不算——所以两处都写，不靠守卫放过）。
    """
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_PROBE_TIMEOUT_SEC):
            return True
    except TimeoutError:                        # socket.timeout 是它的别名（py3.10+）
        _ERROR_COUNT["_tcp_open"] += 1
        _note("tcp_timeout")
    except ConnectionRefusedError:
        _ERROR_COUNT["_tcp_open"] += 1
        _note("tcp_refused")
    except OSError as e:                        # ENETUNREACH / EHOSTUNREACH 等
        _ERROR_COUNT["_tcp_open"] += 1
        _note(f"tcp_oserror_{type(e).__name__}")
    return False


def _channel_ok(api, want: str) -> bool:
    """第二档：按要用的通道验一次真实返回。

    `bars` 只有拿到**非空**数据才算数——2026-11-05 对前 14 台实测 connect 6/14、bars 0/14，
    这一档就是用来把「连上了但空帧」与「真拿不到」分开的。
    """
    if want == "connect":
        return True
    if want == "bars":
        for category in (9, 0):        # 9=5 分钟、0=日线；任一有数据即算通道活着
            try:
                rows = api.get_security_bars(category, PROBE_MARKET, PROBE_CODE, 0, 3)
            except Exception as e:
                _ERROR_COUNT["_channel_ok"] += 1
                _note(f"bars_error_{type(e).__name__}")
                return False
            if rows:
                return True
        _note("bars_empty")
        return False
    if want == "f10":
        try:
            cats = api.get_company_info_category(PROBE_MARKET, PROBE_CODE)
            if not cats:
                _note("f10_no_category")
                return False
            body = api.get_company_info_content(
                PROBE_MARKET, PROBE_CODE, cats[0]["filename"], cats[0]["start"], cats[0]["length"])
            if not body:
                _note("f10_empty_content")
                return False
            return True
        except Exception as e:
            _ERROR_COUNT["_channel_ok"] += 1
            _note(f"f10_error_{type(e).__name__}")
            return False
    _note(f"unknown_want_{want}")
    return False


def probe_host(ip: str, port: int, *, want: str = "connect") -> bool:
    """单台机器：TCP 通 + 协议握手 + （按档位）真实数据非空。"""
    if not _tcp_open(ip, port):
        return False
    api = hq_api(heartbeat=False)
    try:
        try:
            connected = api.connect(ip, port, time_out=DATA_PROBE_TIMEOUT_SEC)
        except Exception as e:
            _ERROR_COUNT["probe_host"] += 1
            _note(f"connect_error_{type(e).__name__}")
            return False
        if not connected:
            _note("connect_false")
            return False
        return _channel_ok(api, want)
    finally:
        try:
            api.disconnect()
        except Exception:
            _ERROR_COUNT["probe_host"] += 1
            _note("disconnect_failed")


def select_server(*, want: str = "bars", limit: int | None = None,
                  only: tuple[str, ...] | None = None,
                  on_note: Callable[[str, dict], None] | None = None) -> dict | None:
    """返回第一台「这一档真能用」的主机；全失败返回 None，原因留在 `last_stats()`。

    `want` 必须是 `connect/bars/f10` 之一——用哪条通道就探哪条，别拿 F10 的可达
    去承诺 K 线可用（这正是本环境会踩的坑）。
    """
    if want not in WANTS:
        raise ValueError(f"want 必须是 {WANTS} 之一，收到 {want!r}")
    if hq_api is None:
        _ERROR_COUNT["select_server"] += 1
        _note("pytdx_missing")
        return None
    reset_stats()
    t0 = time.perf_counter()
    hosts = raw_hosts()
    if only:
        hosts = [h for h in hosts if h[0] in set(only)]
    for name, ip, port in hosts[: (limit or DEFAULT_SCAN_LIMIT)]:
        _STATS["tried"] += 1
        ok = probe_host(ip, port, want=want)
        info = {"name": name, "ip": ip, "port": port, "want": want,
                "tried": _STATS["tried"]}
        if on_note:
            on_note("ok" if ok else "skip", info)
        if ok:
            _STATS["selected"] = info
            _STATS["elapsed_sec"] = time.perf_counter() - t0
            return info
    _STATS["elapsed_sec"] = time.perf_counter() - t0
    return None
