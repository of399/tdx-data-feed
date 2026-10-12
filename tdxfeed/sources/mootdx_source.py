"""mootdx 数据源（活跃维护的通达信通道，替代已归档的 pytdx 作为主通道；Sina 兜底在调用方）。

【阶段 0 数据层加固 · 2026-10-10】
- 个股日线：StdQuotes.bars（底层 get_security_bars，vol 单位=手）
- 指数日线：StdQuotes.index_bars（底层 get_index_bars，vol 单位=手）
- volume 单位与 Sina 兜底一致（均=手），落盘后由 writers.merge_write 统一合并；
  复用 2026-10-09 的 kind='mergesort' 去重修复，保证增量覆盖正确，无需 ÷100 转换
- 服务器选取：优先复用 pytdx hq_hosts 显式地址，避免 bestip 网络探测在本环境失败；
  连接失败 / 返回空时由调用方（full_sync.download_indices）回退 Sina
"""
from __future__ import annotations

import logging
import os
import re
import threading

from tdxfeed.sources.base import DataSource

log = logging.getLogger(__name__)

try:
    from mootdx.quotes import Quotes
    from pytdx.config.hosts import hq_hosts
except Exception:  # pragma: no cover
    Quotes = None  # type: ignore[assignment]
    hq_hosts = []  # type: ignore[assignment]


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
        except BaseException as e:  # noqa: BLE001
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
    """取 pytdx hq_hosts 首个地址作为 mootdx 显式 server，避免 bestip 探测。"""
    if hq_hosts:
        try:
            return (hq_hosts[0][1], int(hq_hosts[0][2]))
        except Exception:
            pass
    return None


def _norm_date(d) -> "datetime":  # noqa: F821
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
        try:
            if server:
                self.client = Quotes.factory(market="std", server=server, timeout=15)
            else:
                # 退路：依赖 mootdx bestip 配置（需先 `python -m mootdx bestip`）
                self.client = Quotes.factory(market="std", timeout=15)
        except Exception as e:
            raise RuntimeError("mootdx 客户端初始化失败: " + str(e))

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
        """一次性探测 mootdx 是否可用（连通且能取数），供 download_indices 循环前决断。

        避免本环境 TDX 不可达时，每个指数都白白等待网络超时。探测失败返回 False，
        调用方将全程走 Sina 兜底；探测本身受线程超时保护，不会挂死。
        """
        try:
            df = _run_with_timeout(
                self.client.index_bars, symbol=symbol, frequency=9, start=0, offset=10,
                timeout=timeout,
            )
            return df is not None and not getattr(df, "empty", True)
        except Exception:  # noqa: BLE001
            return False

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
