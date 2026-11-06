"""mootdx 数据源（活跃维护的通达信通道，替代已归档的 pytdx 作为主通道；Sina 兜底在调用方）。

【阶段 0 数据层加固 · 2026-10-10】
- 个股日线：StdQuotes.bars（底层 get_security_bars，vol 单位=手）
- 指数日线：StdQuotes.index_bars（底层 get_index_bars，vol 单位=手）
- volume 单位与 Sina 兜底一致（均=手），落盘后由 writers.merge_write 统一合并；
  复用 2026-10-09 的 kind='mergesort' 去重修复，保证增量覆盖正确，无需 ÷100 转换
- 服务器选取：改为**可达性驱动**（`tdxfeed.sources.tdx_hosts.select_server(want="bars")`），
  不再取 `hq_hosts[0]`、不再退回 bestip 静默探测；连接失败 / 返回空时由调用方
  （full_sync.download_indices）回退 Sina
- **本环境的真实能力分档（2026-11-05 对 `hq_hosts` 前 14 台逐台实测）**：
  connect 6/14、**bars 0/14**、f10 6/14 —— 连得上的那 6 台 F10 有正文但 K 线一律 0 根。
  所以**这个源在当前机器上取不到个股/指数日线**，日线与指数的实际权威通道仍是 Sina；
  选主这一修改善的是失败可见性与选主正确性，**不会**把通达信侧的 K 线接回来。
  `select_server(want="bars")` 返回 `None` 时本类直接抛 `RuntimeError` 并带原因统计，
  不再构造一个"看起来活着、每只票都空"的客户端。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter

from tdxfeed.sources.base import DataSource

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

# 探测失败的**形状**分桶（空帧 vs 抛错）。本仓约定 `_ERROR_COUNT` 的键必须是函数名，
# 所以这个区分放独立计数器，不塞进它里面——否则 'probe_empty' 这种键会被
# tests/test_error_counters.py 判「键不对应任何函数」。
_PROBE_REASONS: Counter[str] = Counter()

log = logging.getLogger(__name__)

try:
    from mootdx.quotes import Quotes
except Exception:  # pragma: no cover
    _ERROR_COUNT['<module>'] += 1
    Quotes = None  # type: ignore[assignment]

# 主机候选表与三个超时的唯一来源（见 tdx_hosts 模块 docstring 的实测：`hq_hosts[0]`
# 已死，取第一台会让构造白等 15 秒）
from tdxfeed.sources.tdx_hosts import (  # noqa: E402
    REQUEST_TIMEOUT_SEC,
    select_server,
)

MOOTDX_TIMEOUT = float(os.environ.get("MOOTDX_TIMEOUT", "10"))


def _run_with_timeout(func, *args, timeout=None, **kwargs):
    """守护线程包裹阻塞的 mootdx/pytdx 网络调用，防止 TDX 服务器不可达时挂死。

    超时或异常一律向上抛 RuntimeError，由调用方（full_sync）捕获并回退 Sina。
    守护线程在超时后随进程退出被回收，不阻塞主流程。
    """
    timeout = timeout or MOOTDX_TIMEOUT
    box: dict = {}

    def _target():
        try:
            box["r"] = func(*args, **kwargs)
        except BaseException as e:
            _ERROR_COUNT['_run_with_timeout'] += 1
            box["e"] = e

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise RuntimeError(f"mootdx 请求超时({timeout}s)，服务器可能不可达/被封锁")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _default_server() -> tuple[str, int] | None:
    """可达性驱动：取第一台**真能吐 K 线**的主机（不再看 `hq_hosts` 的第一条）。

    返回 None 时不是「没试过」，而是整张候选表都探过并记了原因——
    `tdx_hosts.last_stats()` 能回答「扫了几台、每台为什么被跳掉」。
    """
    try:
        sel = select_server(want="bars")
    except Exception as e:  # 选择器本身炸了也要留痕，不能退成静默 None
        _ERROR_COUNT['_default_server'] += 1
        log.warning("mootdx 主机选择异常: %s", e)
        return None
    if not sel:
        _ERROR_COUNT['_default_server'] += 1
        log.warning("mootdx 无可用主机，统计=%s", tdx_hosts_stats())
        return None
    return (sel["ip"], sel["port"])


def tdx_hosts_stats() -> dict:
    """把主机选择的原因暴露给调用方与状态输出（不做第二处统计）。"""
    from tdxfeed.sources.tdx_hosts import last_stats

    return last_stats()


def _norm_date(d) -> datetime:  # noqa: F821
    """mootdx 返回的 date 可能是 int(YYYYMMDD) 或字符串，统一转 datetime。"""
    from datetime import datetime

    if d is None:
        return None
    if isinstance(d, (int, float)):
        s = str(int(d))
    else:
        s = str(d)
    s = re.sub(r"\D", "", s)[:8]
    if len(s) != 8:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d")
    except Exception:
        _ERROR_COUNT['_norm_date'] += 1
        return None


class MootdxSource(DataSource):
    """mootdx 主通道（通达信日线 / 指数）。

    失败语义：连接失败或返回空均抛 RuntimeError，由 full_sync 调用方捕获并回退 Sina，
    从而支撑 verify_daily_freshness 的“源失败率”指标趋向于 0。
    """

    def __init__(self):
        if Quotes is None:
            raise RuntimeError("mootdx 未安装，无法启用 mootdx 主通道")
        server = _default_server()
        if server is None:
            # 不再静默退回 mootdx bestip（本环境 bestip 从来没通过，见模块 docstring 的实测）。
            # 直接抛：调用方 full_sync 已有 except → 计一次 download_indices → 全程 Sina，
            # 而"为什么无路"留在 tdx_hosts.last_stats() 里，不再是只有一个 False。
            _ERROR_COUNT['__init__'] += 1
            raise RuntimeError(
                f"通达信侧无可用主机（K 线档探活全失败），统计={tdx_hosts_stats()}")
        try:
            self.client = Quotes.factory(market="std", server=server,
                                         timeout=REQUEST_TIMEOUT_SEC)
        except Exception as e:
            _ERROR_COUNT['__init__'] += 1
            raise RuntimeError("mootdx 客户端初始化失败: " + str(e)) from e

    def _collect(self, df) -> list:
        """mootdx DataFrame → 归一化 bars [date, open, high, low, close, volume(手), amount]。"""
        if df is None or getattr(df, "empty", True):
            return []
        bars = []
        for _, row in df.iterrows():
            d = _norm_date(row.get("date"))
            if d is None:
                continue
            try:
                bars.append([
                    d,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),  # mootdx 已把 vol rename 为 volume，单位=手
                    float(row.get("amount") or 0),
                ])
            except (KeyError, ValueError, TypeError) as e:
                _ERROR_COUNT['_collect'] += 1
                log.warning("mootdx 行解析跳过: %s", e)
                continue
        return bars

    def fetch_bars(self, symbol, start_date=None, end_date=None):
        code = symbol[-6:]
        bars: list = []
        start = 0
        while True:
            df = _run_with_timeout(
                self.client.bars, symbol=code, frequency=9, start=start, offset=800,
            )
            chunk = self._collect(df)
            if not chunk:
                break
            bars.extend(chunk)
            if len(chunk) < 800:
                break
            start += 800
        if not bars:
            raise RuntimeError(f"mootdx 个股 {code} 返回空（可能服务器不可达）")
        return bars

    def probe(self, symbol: str = "000001", timeout: int = 8) -> bool:
        """一次性探测 mootdx 是否可用（连通且**真吐数据**），供 download_indices 循环前决断。

        探测失败返回 False，调用方全程走 Sina 兜底；探测受线程超时保护，不会挂死。

        两种失败要分开数（2026-11-05 对前 14 台逐台实测：connect 6/14、bars 0/14，
        失败形状全是「连上了但空帧」这一类，原先它连一个计数都不留）：
          `probe_error` = 抛异常（超时/断连）
          `probe_empty` = 连上了但 0 根（这一类**不是网络问题**，换主机也没用）
        两类都进 `_ERROR_COUNT['probe']`（键名按本仓约定必须是函数名），
        具体形状进 `_PROBE_REASONS`，状态输出与排查时能分开看。
        """
        try:
            df = _run_with_timeout(
                self.client.index_bars, symbol=symbol, frequency=9, start=0, offset=10,
                timeout=timeout,
            )
        except Exception:
            _ERROR_COUNT['probe'] += 1
            _PROBE_REASONS['probe_error'] += 1
            return False
        if df is None or getattr(df, "empty", True):
            _ERROR_COUNT['probe'] += 1
            _PROBE_REASONS['probe_empty'] += 1
            return False
        return True

    def fetch_index_bars(self, market: int, code: str, count: int = 1023) -> list:
        """通达信指数专用通道（与个股 fetch_bars 区分）。

        mootdx.index_bars 内部按代码前缀判定 market（00/88→沪, 99→深），
        volume 单位=手，与 Sina 兜底一致。market 参数保留以兼容调用方签名。
        """
        bars: list = []
        start = 0
        fetched = 0
        while fetched < count:
            df = _run_with_timeout(
                self.client.index_bars, symbol=code, frequency=9, start=start, offset=800,
            )
            chunk = self._collect(df)
            if not chunk:
                break
            bars.extend(chunk)
            fetched += len(chunk)
            if len(chunk) < 800:
                break
            start += 800
        if not bars:
            raise RuntimeError(f"mootdx 指数 {code} 返回空（可能服务器不可达）")
        return bars
