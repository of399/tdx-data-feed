"""实时行情（新浪 L1 五档 + 封单）——打板扫描的盘中数据源。

接口：``http://hq.sinajs.cn/list=sh600000,sz000001,...``
- 必需 ``Referer: https://finance.sina.com.cn/``，否则 403
- 响应 GBK 编码
- 单批建议 ≤ 80 个代码，超长易被截断

**字段布局（实测 34 段，2026-10-23 用腾讯源 qt.gtimg.cn 逐档交叉验证）**::

    0 名称  1 今开  2 昨收  3 现价  4 最高  5 最低
    6 买一价  7 卖一价  8 成交量(股)  9 成交额(元)
    10/11 买一量/价  12/13 买二  14/15 买三  16/17 买四  18/19 买五
    20/21 卖一量/价  22/23 卖二  24/25 卖三  26/27 卖四  28/29 卖五
    30 日期  31 时间  32 状态  33 保留（如 D|102700|1826006.000）

⚠ 档位顺序是**"量,价"**，不是"价,量"；且 **8/9 是成交量/成交额**（腾讯 481706 手
= 新浪 48170642 股，逐只对上）。历史教训：早期版本把 8/9 当成"委买一量/委买一额"，
于是"封单量"其实是当天成交量、"封单比"退化成量比、"撤单速率"是成交量下降速度——
所有依赖这些字段的研究结论在修正前都要重新核一遍。

本接口能给的打板要素（对应计划书 §2.6）：
- 买一量/买一量×买一价 → 封单股数与封单金额（真实排队队列仍需 L2，见附录 E）
- 卖五档全 0 → 涨停封死（卖压已尽）
- 成交量/成交额 → 当日换手、封单额/成交额比（排队强度）
- 现价/最高/最低 → 炸板判定

旧版布局仍做防御性兼容（6/7 起"价,量"五档 … 26 日期 27 时间 30 成交量 31 成交额）。
"""
from __future__ import annotations

import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

__all__ = ["Quote", "fetch_quote_map", "fetch_quotes"]

_SINA_URL = "http://hq.sinajs.cn/list="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "*/*",
}
_LINE_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="(.*)"')
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(slots=True)
class Quote:
    """单只股票的实时快照（L1 五档 + 封单）。"""

    code: str
    name: str = ""
    open: float = 0.0
    prev_close: float = 0.0
    price: float = 0.0
    high: float = 0.0
    low: float = 0.0
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(价, 量/股)]
    asks: list[tuple[float, float]] = field(default_factory=list)  # [(价, 量/股)]
    seal_shares: float = 0.0  # 封单股数（委买一量）
    seal_amount: float = 0.0  # 封单金额（委买一额，元）
    volume: float = 0.0  # 成交量（股，接口 8 段）
    amount: float = 0.0  # 成交额（元，接口 9 段）
    date: str = ""
    time: str = ""

    @property
    def bid1_vol(self) -> float:
        """买一量（股）——即委买一量，封单股数的来源。"""
        return self.bids[0][1] if self.bids else self.seal_shares

    @property
    def bid1_price(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def ask1_vol(self) -> float:
        return self.asks[0][1] if self.asks else 0.0

    @property
    def suspended(self) -> bool:
        """停牌/无报价：现价为 0。"""
        return self.price <= 0

    def pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.price / self.prev_close - 1.0) * 100.0


def _to_float(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _parse_line(code: str, payload: str) -> Quote | None:
    """解析新浪 hq_str 的一行（新版优先，兼容旧版）。"""
    parts = payload.split(",")
    if len(parts) < 6:
        return None
    q = Quote(code=code, name=parts[0].strip())
    q.open = _to_float(parts[1])
    q.prev_close = _to_float(parts[2])
    q.price = _to_float(parts[3])
    q.high = _to_float(parts[4])
    q.low = _to_float(parts[5])

    # 判定格式：末尾附近出现 YYYY-MM-DD 即为新版
    new_fmt = False
    for idx in range(len(parts) - 1, max(len(parts) - 6, 0), -1):
        if _DATE_RE.match(parts[idx].strip()):
            new_fmt = True
            q.date = parts[idx].strip()
            q.time = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
            break

    if new_fmt:
        # 34 段新版：档位是"量,价"顺序（腾讯源逐档交叉验证过，见
        # tests/test_daban_realtime_layout.py）。量在偶数位、价在奇数位。
        q.volume = _to_float(parts[8])
        q.amount = _to_float(parts[9])
        for i in range(5):  # 买一~买五：(10,11) (12,13) … (18,19)
            v = _to_float(parts[10 + i * 2]) if len(parts) > 10 + i * 2 else 0.0
            p = _to_float(parts[11 + i * 2]) if len(parts) > 11 + i * 2 else 0.0
            if p > 0:
                q.bids.append((p, v))
        for i in range(5):  # 卖一~卖五：(20,21) (22,23) … (28,29)
            v = _to_float(parts[20 + i * 2]) if len(parts) > 20 + i * 2 else 0.0
            p = _to_float(parts[21 + i * 2]) if len(parts) > 21 + i * 2 else 0.0
            if p > 0:
                q.asks.append((p, v))
        if q.bids:
            q.seal_shares = q.bids[0][1]
            q.seal_amount = q.bids[0][0] * q.bids[0][1]
    else:
        for i in range(5):  # 买一~买五
            p = _to_float(parts[6 + i * 2]) if len(parts) > 6 + i * 2 else 0.0
            v = _to_float(parts[7 + i * 2]) if len(parts) > 7 + i * 2 else 0.0
            if p > 0:
                q.bids.append((p, v))
        for i in range(5):  # 卖一~卖五
            p = _to_float(parts[16 + i * 2]) if len(parts) > 16 + i * 2 else 0.0
            v = _to_float(parts[17 + i * 2]) if len(parts) > 17 + i * 2 else 0.0
            if p > 0:
                q.asks.append((p, v))
        if len(parts) > 26:
            q.date = parts[26].strip()
        if len(parts) > 27:
            q.time = parts[27].strip()
        if len(parts) > 30:
            q.volume = _to_float(parts[30])
        if len(parts) > 31:
            q.amount = _to_float(parts[31])
        if q.bids:
            q.seal_shares = q.bids[0][1]
            q.seal_amount = q.bids[0][0] * q.bids[0][1]
    return q


def _fetch_batch(symbols: list[str], retries: int = 3) -> dict[str, Quote]:
    url = _SINA_URL + ",".join(symbols)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
            text = raw.decode("gbk", errors="ignore")
            out: dict[str, Quote] = {}
            for line in text.splitlines():
                m = _LINE_RE.search(line)
                if not m:
                    continue
                q = _parse_line(m.group(1), m.group(2))
                if q is not None:
                    out[m.group(1)] = q
            if out:
                return out
            last_err = RuntimeError("新浪返回空数据")
        except Exception as e:
            last_err = e
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"新浪实时源失败: {last_err}")


def fetch_quote_map(
    symbols: list[str],
    batch_size: int = 80,
    workers: int = 8,
) -> dict[str, Quote]:
    """批量拉取实时快照。

    参数:
        symbols:    ['sh600000', 'sz000001', ...]（带交易所前缀）
        batch_size: 单批代码数
        workers:    并发线程数

    返回:
        {symbol: Quote}
    """
    batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    result: dict[str, Quote] = {}
    if not batches:
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for part in pool.map(_fetch_batch, batches):
            result.update(part)
    return result


def fetch_quotes(symbols: list[str], **kwargs) -> list[Quote]:
    """批量拉取实时快照，返回列表。"""
    return list(fetch_quote_map(symbols, **kwargs).values())
