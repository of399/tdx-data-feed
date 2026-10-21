"""腾讯实时行情（qt.gtimg.cn）——补充新浪 L1 缺失的市值/量/换手字段。

**为什么需要第二个源**：新浪 L1 的委买一量/额给出了封单，但新版接口**不返回
当日成交量、流通市值、换手率、量比**，导致封单比只能用"近5日均量"近似、
换手率无法计算。腾讯接口正好补齐这些：

===========  ==========================  ==========================================
字段下标      含义                       用途
===========  ==========================  ==========================================
3             现价                       —
4             昨收                       涨跌幅基准
5             今开                       —
6/36          成交量(手)                  股数法封单比的分母
37            成交额(万元)                —
38            换手率(%)                   §A.1 因子 T（换手率）
39            市盈率                     —
43            振幅(%)                    —
44            流通市值(亿元)              金额法封单比的分母
45            总市值(亿元)                —
46            市净率                     —
47            **涨停价**                  交叉校验自算涨停价
48            **跌停价**                  交叉校验
49            量比                       §2.6 上板放量判定
30            时间戳 YYYYMMDDHHMMSS        —
===========  ==========================  ==========================================

⚠ 腾讯的 47/48 是交易所口径的涨停/跌停价，可用于校验
`tdxfeed.daban.limit_price.limit_up_price` 的四舍五入实现。
"""
from __future__ import annotations

import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

__all__ = ["TQuote", "fetch_tencent_map"]

_TENCENT_URL = "http://qt.gtimg.cn/q="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}
_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="(.*)"')


@dataclass(slots=True)
class TQuote:
    """腾讯行情快照。"""

    code: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0  # 成交量（手）
    amount: float = 0.0  # 成交额（万元）
    turnover: float = 0.0  # 换手率（%）
    float_mcap: float = 0.0  # 流通市值（亿元）
    total_mcap: float = 0.0  # 总市值（亿元）
    limit_up: float = 0.0  # 交易所涨停价
    limit_down: float = 0.0  # 交易所跌停价
    vol_ratio: float = 0.0  # 量比
    amplitude: float = 0.0  # 振幅（%）
    time: str = ""

    @property
    def volume_shares(self) -> float:
        """成交量（股）——股数法封单比的分母。"""
        return self.volume * 100.0

    @property
    def float_mcap_yuan(self) -> float:
        """流通市值（元）——金额法封单比的分母。"""
        return self.float_mcap * 1e8

    @property
    def amount_yuan(self) -> float:
        return self.amount * 1e4

    def pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.price / self.prev_close - 1.0) * 100.0


def _f(parts: list[str], idx: int) -> float:
    if idx >= len(parts):
        return 0.0
    try:
        return float(parts[idx])
    except (TypeError, ValueError):
        return 0.0


def _parse(symbol: str, body: str) -> TQuote | None:
    f = body.split("~")
    if len(f) < 40:
        return None
    q = TQuote(code=symbol, name=f[1].strip())
    q.price = _f(f, 3)
    q.prev_close = _f(f, 4)
    q.open = _f(f, 5)
    q.volume = _f(f, 6)
    q.time = f[30].strip() if len(f) > 30 else ""
    q.high = _f(f, 33)
    q.low = _f(f, 34)
    q.volume = _f(f, 36) or q.volume
    q.amount = _f(f, 37)
    q.turnover = _f(f, 38)
    q.amplitude = _f(f, 43)
    q.float_mcap = _f(f, 44)
    q.total_mcap = _f(f, 45)
    q.limit_up = _f(f, 47)
    q.limit_down = _f(f, 48)
    q.vol_ratio = _f(f, 49)
    return q


def _fetch_batch(symbols: list[str], retries: int = 3) -> dict[str, TQuote]:
    url = _TENCENT_URL + ",".join(symbols)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            out: dict[str, TQuote] = {}
            for line in text.splitlines():
                m = _LINE_RE.search(line)
                if not m:
                    continue
                q = _parse(m.group(1), m.group(2))
                if q is not None:
                    out[m.group(1)] = q
            if out:
                return out
            last_err = RuntimeError("腾讯返回空数据")
        except Exception as e:
            last_err = e
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"腾讯实时源失败: {last_err}")


def fetch_tencent_map(
    symbols: list[str],
    batch_size: int = 60,
    workers: int = 8,
) -> dict[str, TQuote]:
    """批量拉取腾讯快照。

    返回 {symbol: TQuote}
    """
    batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    result: dict[str, TQuote] = {}
    if not batches:
        return result
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for part in pool.map(_fetch_batch, batches):
            result.update(part)
    return result
