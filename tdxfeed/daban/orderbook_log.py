"""预警盘口快照记录器：补齐历史数据里**根本没有**的排队/封单维度。

**为什么需要**：纸盘账本（`paper.py`）和日线 parquet 只有"今天封没封住"这个
日频结果，没有封单额曲线、卖一是否被砸、是不是一字。这些恰恰是 Stage 2 测出
逆向选择（成交样本封板率仅 39.7%）的根因维度，且新浪 L1 只在实时给，
所以只能盘中前瞻记录。接口能力见 `realtime.py`：买一量=封单股数、
买一量×买一价=封单金额、五档齐全、8/9 段还是当日成交量/成交额。

**写入策略**（否则 3 秒轮询 × 全天 ≈ 百万行，没法用）：事件驱动 + 兜底心跳。
每轮轮询对每个命中预警的票最多写一行，优先级如下::

    SEAL / RESEAL / BREAK / NEAR   状态跃迁（必写）
    ASK_OPEN                       卖一量从 0 变 ≥1（封死开始被砸，必写）
    SEAL_GROW / SEAL_SHRINK        封单额相对上次写入变化 ≥ change_pct（必写）
    HEARTBEAT                      仍封板且距上次写入 ≥ heartbeat_sec

落盘：``data/daban/orderbook/orderbook_YYYYMMDD.csv``（utf-8-sig，当日追加，
表头只在创建时写一次），追加方式与 `auction_log.py` 一致。

主键是 ``(trade_date, quote_time, code)``；与纸盘账本按 ``(signal_date, code)`` join。

⚠ 三个口径注意：
- 涨停价本模块按 ``limit_ratio(code, name, on_date=trade_date)`` 自己算，
  主板风险警示票在 2026-07-06 前后分别是 5% / 10%，与 `limit_price.py` 同源。
- ``trade_date`` 与文件名统一紧凑成 ``YYYYMMDD``，好跟纸盘账本的 ``signal_date`` 直接 join。
- ``seal_vs_amount``（封单额/当日成交额）是本接口能算的真排队强度；
  ``turnover`` 要靠流通股本（腾讯源或 ``--float-shares``），拿不到时留空，
  此时 ``seal_ratio_src`` 标 ``本地代理``（封单量/近 5 日均量），两种口径别混在一张表里回测。
"""
from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tdxfeed.daban.limit_price import (
    as_date,
    board_label,
    is_sealed,
    is_st,
    limit_ratio,
    limit_up_price,
)
from tdxfeed.daban.realtime import Quote, fetch_quote_map
from tdxfeed.daban.scanner import (
    STATUS_AUCTION_SEAL,
    STATUS_BROKEN,
    STATUS_NEAR,
    STATUS_RESEAL,
    STATUS_SEALED,
    ScanResult,
)

__all__ = [
    "EVENTS",
    "FIELDS",
    "ORDERBOOK_DIR",
    "OrderbookTracker",
    "append_orderbook_rows",
    "compact_date",
    "orderbook_path",
    "poll_once",
    "read_orderbook_rows",
]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ORDERBOOK_DIR = DATA_DIR / "daban" / "orderbook"

_EPS = 0.001

EVENT_SEAL = "SEAL"
EVENT_RESEAL = "RESEAL"
EVENT_BREAK = "BREAK"
EVENT_NEAR = "NEAR"
EVENT_SEAL_GROW = "SEAL_GROW"
EVENT_SEAL_SHRINK = "SEAL_SHRINK"
EVENT_ASK_OPEN = "ASK_OPEN"
EVENT_HEARTBEAT = "HEARTBEAT"
EVENTS = frozenset({
    EVENT_SEAL, EVENT_RESEAL, EVENT_BREAK, EVENT_NEAR,
    EVENT_SEAL_GROW, EVENT_SEAL_SHRINK, EVENT_ASK_OPEN, EVENT_HEARTBEAT,
})

_SEALED_STATUSES = frozenset({STATUS_SEALED, STATUS_RESEAL, STATUS_AUCTION_SEAL})

FIELDS: list[str] = [
    "trade_date", "poll_ts", "quote_time", "code", "name", "board", "is_st", "is_new",
    "prev_close", "ratio", "limit_price", "open", "high", "low", "price", "pct",
    "touched", "sealed", "is_yizi", "seal_dead", "ask_empty",
    "status", "event",
    "seal_shares", "seal_amount", "volume", "amount", "seal_vs_amount",
    "bid1_price", "bid1_vol", "bid2_price", "bid2_vol", "bid3_price", "bid3_vol",
    "bid4_price", "bid4_vol", "bid5_price", "bid5_vol",
    "ask1_price", "ask1_vol", "ask2_price", "ask2_vol", "ask3_price", "ask3_vol",
    "ask4_price", "ask4_vol", "ask5_price", "ask5_vol",
    "consecutive", "cb", "daily_last_date", "first_seal_time", "break_count", "cancel_rate",
    "seal_vs_vol5", "seal_ratio_amount", "seal_ratio_src", "turnover", "vol_ratio", "score",
]


@dataclass(slots=True)
class _Last:
    """上一次为该票写行的内容摘要，用来判断"有没有新事件"。"""

    status: str = ""
    seal_amount: float = 0.0
    ask1_vol: float = 0.0
    ts: float = 0.0


@dataclass(slots=True)
class OrderbookTracker:
    """按代码维护"上次写了什么"，把每轮扫描结果压成事件行。

    ``last_daily_date``：code → 本地日线最后交易日（YYYY-MM-DD / YYYYMMDD / 空串）。
    本地日线有的票当天已入库、有的还停在昨天，``consecutive`` 因此会少算一天；
    给了这个回调才能算出与交易所/东财同口径的 ``cb``。不传则 ``cb = consecutive``。
    """

    heartbeat_sec: float = 30.0
    change_pct: float = 0.2
    last_daily_date: Callable[[str], str] | None = None
    _last: dict[str, _Last] = field(default_factory=dict)

    def observe(
        self,
        result: ScanResult,
        quotes: dict[str, Quote],
        now: float | None = None,
    ) -> list[dict]:
        now = time.time() if now is None else now
        by_code = {_bare(q.code): q for q in quotes.values()}
        rows: list[dict] = []
        for cand in result.candidates:
            row = self._row(cand, by_code.get(cand.code), result.trade_date, now)
            if row is not None:
                rows.append(row)
        return rows

    def _row(self, cand, q: Quote | None, trade_date: str, now: float) -> dict | None:
        if q is None or q.suspended or q.prev_close <= 0:
            return None
        if cand.status not in (_SEALED_STATUSES | {STATUS_NEAR, STATUS_BROKEN}):
            return None

        on_date = as_date(trade_date)
        ratio = limit_ratio(cand.code, cand.name, on_date=on_date)
        lp = limit_up_price(q.prev_close, ratio)
        sealed = 1 if is_sealed(q.price, lp) else 0
        ask1_vol = q.asks[0][1] if q.asks else 0.0
        last = self._last.get(cand.code)
        event = self._event(cand.status, sealed, ask1_vol, q.seal_amount, last, now)
        if event is None:
            return None
        self._last[cand.code] = _Last(
            status=cand.status, seal_amount=q.seal_amount, ask1_vol=ask1_vol, ts=now
        )
        day = compact_date(trade_date)
        daily_last = ""
        if self.last_daily_date is not None:
            daily_last = compact_date(self.last_daily_date(cand.code) or "")
            # 本地日线没有当天的 bar（甚至整只票没下载过）→ 今日封板没被算进 consecutive
            if sealed and day and (not daily_last or daily_last < day):
                cb = int(cand.consecutive or 0) + 1
            else:
                cb = int(cand.consecutive or 0)
        else:
            cb = int(cand.consecutive or 0)
        return _build_row(cand, q, event=event, trade_date=trade_date, now=now,
                          ratio=float(ratio), limit_price=lp, sealed=sealed,
                          cb=cb, daily_last_date=daily_last)

    def _event(self, status, sealed, ask1_vol, seal_amount, last: _Last | None, now):
        if last is None:
            if status == STATUS_RESEAL:
                return EVENT_RESEAL
            if status in _SEALED_STATUSES:
                return EVENT_SEAL
            if status == STATUS_BROKEN:
                return EVENT_BREAK
            return EVENT_NEAR
        if status != last.status:
            if status == STATUS_RESEAL:
                return EVENT_RESEAL
            if status in _SEALED_STATUSES:
                return EVENT_SEAL
            if status == STATUS_BROKEN:
                return EVENT_BREAK
            if status == STATUS_NEAR:
                return EVENT_NEAR
            return None
        if sealed and last.ask1_vol < 1.0 and ask1_vol >= 1.0:
            return EVENT_ASK_OPEN
        if last.seal_amount > 0 and seal_amount > 0:
            delta = (seal_amount - last.seal_amount) / last.seal_amount
            if delta >= self.change_pct:
                return EVENT_SEAL_GROW
            if delta <= -self.change_pct:
                return EVENT_SEAL_SHRINK
        if status in _SEALED_STATUSES and now - last.ts >= self.heartbeat_sec:
            return EVENT_HEARTBEAT
        return None


def _bare(symbol: str) -> str:
    return symbol[-6:] if len(symbol) > 6 else symbol


def compact_date(value: str) -> str:
    """YYYY-MM-DD / YYYYMMDD / datetime64 文本 → YYYYMMDD；不像日期就返回空串。

    空串必须是空串：调用方靠它区分"本地没这根日线"和"今天已经入库"。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _levels(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(float(p), float(v)) for p, v in pairs[:5]]


def _round(value: float, digits: int) -> float:
    return round(float(value), digits)


def _build_row(
    cand,
    q: Quote,
    *,
    event: str,
    trade_date: str,
    now: float,
    ratio: float,
    limit_price: float,
    sealed: int,
    cb: int,
    daily_last_date: str,
) -> dict:
    name = q.name or cand.name
    bids = _levels(q.bids)
    asks = _levels(q.asks)
    ask1_vol = asks[0][1] if asks else 0.0
    price = q.price
    yizi = 1 if (q.open >= limit_price - _EPS and q.low >= limit_price - _EPS) else 0
    row = {
        "trade_date": compact_date(trade_date),
        "poll_ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "quote_time": q.time,
        "code": cand.code,
        "name": name,
        "board": board_label(cand.code, name, on_date=as_date(trade_date)),
        "is_st": int(is_st(name)),
        "is_new": int(name.startswith(("N", "C"))),
        "prev_close": _round(q.prev_close, 3),
        "ratio": round(ratio, 4),
        "limit_price": _round(limit_price, 3),
        "open": _round(q.open, 3),
        "high": _round(q.high, 3),
        "low": _round(q.low, 3),
        "price": _round(price, 3),
        "pct": _round(cand.pct, 3),
        "touched": 1 if q.high >= limit_price - _EPS else 0,
        "sealed": sealed,
        "is_yizi": yizi if sealed else 0,
        "seal_dead": 1 if (sealed and ask1_vol < 1.0) else 0,
        "ask_empty": 1 if not asks or all(v <= 0 for _, v in asks) else 0,
        "status": cand.status,
        "event": event,
        "seal_shares": _round(q.seal_shares, 0),
        "seal_amount": _round(q.seal_amount, 0),
        "volume": _round(q.volume, 0),
        "amount": _round(q.amount, 0),
        "seal_vs_amount": _round(q.seal_amount / q.amount, 6) if q.amount > 0 else "",
        "consecutive": int(cand.consecutive),
        "cb": int(cb),
        "daily_last_date": daily_last_date,
        "first_seal_time": cand.first_seal_time,
        "break_count": int(cand.break_count),
        "cancel_rate": _round(cand.cancel_rate, 2),
        "seal_vs_vol5": _round(cand.seal_vs_vol5, 4),
        "seal_ratio_amount": "" if cand.seal_ratio_amount is None else _round(cand.seal_ratio_amount, 6),
        "seal_ratio_src": "金额法" if cand.seal_ratio_amount is not None else "本地代理",
        "turnover": "" if cand.turnover is None else _round(cand.turnover, 6),
        "vol_ratio": _round(cand.vol_ratio, 3),
        "score": _round(cand.score, 4),
    }
    for i in range(5):
        bp, bv = bids[i] if i < len(bids) else ("", "")
        ap, av = asks[i] if i < len(asks) else ("", "")
        row[f"bid{i + 1}_price"] = _round(bp, 3) if bp != "" else ""
        row[f"bid{i + 1}_vol"] = _round(bv, 0) if bv != "" else ""
        row[f"ask{i + 1}_price"] = _round(ap, 3) if ap != "" else ""
        row[f"ask{i + 1}_vol"] = _round(av, 0) if av != "" else ""
    return {k: row.get(k, "") for k in FIELDS}


def orderbook_path(trade_date: str, out_dir: Path | str = ORDERBOOK_DIR) -> Path:
    return Path(out_dir) / f"orderbook_{trade_date!s}.csv"


def append_orderbook_rows(
    rows: list[dict],
    out_dir: Path | str = ORDERBOOK_DIR,
    trade_date: str = "",
) -> Path | None:
    """把一批盘口快照追加到当日 CSV；无数据不建文件。"""
    if not rows:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = compact_date(trade_date or rows[0].get("trade_date")) or time.strftime("%Y%m%d")
    path = orderbook_path(date, out_dir)
    write_header = not (path.exists() and path.stat().st_size > 0)
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})
    return path


def read_orderbook_rows(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def poll_once(
    symbols: list[str],
    *,
    scan,
    out_dir: Path | str = ORDERBOOK_DIR,
    fetch=fetch_quote_map,
    tracker: OrderbookTracker | None = None,
    now: float | None = None,
    trade_date: str = "",
) -> dict:
    """一轮"抓行情 → 扫描 → 落盘"，供盯盘循环调用。

    ``scan`` 必传（``Scanner.scan``），把网络与本地状态机留在调用方，
    这样这一层可以离线测试。
    """
    tracker = tracker or OrderbookTracker()
    now = time.time() if now is None else now
    quotes = fetch(symbols)
    result = scan(quotes)
    rows = tracker.observe(result, quotes, now=now)
    date = compact_date(trade_date or result.trade_date) or time.strftime("%Y%m%d")
    _path = append_orderbook_rows(rows, out_dir=out_dir, trade_date=date)
    return {
        "path": orderbook_path(date, out_dir),
        "trade_date": date,
        "rows": len(rows),
        "records": rows,
        "candidates": len(result.candidates),
        "stats": result.stats,
        "session": result.session,
        "scan_time": result.scan_time,
        "tracker": tracker,
        "result": result,
    }
