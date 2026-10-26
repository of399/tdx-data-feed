"""DABAN cb≥3 接力纸盘（规则：docs/DABAN_PAPER_TRADE_RULES_20261022.md）。

数据流::

    T 日收盘封死涨停且 cb>=3
        → 信号（全部入统计账本，前 3 入执行账本）
        → T+1 开盘买入（开盘涨停则买不进）
        → T+1 收盘卖出（封死跌停则顺延 T+2/T+3）

所有价格、状态、两本账统计都在本模块内完成；CLI 只负责读写 parquet/CSV。
"""
from __future__ import annotations

import json
import math
import os
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date as _date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tdxfeed.daban.limit_price import (
    MAIN_ST_RULE_CHANGE_DATE,
    MARKET_BSE,
    MARKET_GEM,
    MARKET_MAIN,
    as_date,
    is_st,
    limit_down_price,
    limit_ratio,
    limit_up_price,
    market_of,
    normalize_code,
)
from tdxfeed.daban.scanner import UNIVERSE_PATH, daily_path, load_universe
from tdxfeed.date_utils import parse_date, parse_date_series

__all__ = [
    "JOURNAL_FIELDS",
    "PaperConfig",
    "backfill",
    "board_tag",
    "calendar_of",
    "daily_report",
    "empty_journal",
    "enrich_history",
    "execute_pending",
    "execute_signal",
    "generate_signals",
    "init_paper",
    "load_config",
    "load_histories",
    "load_journal",
    "normalize_date",
    "save_config",
    "save_journal",
    "settle_deferred",
    "settle_journal",
    "upsert_rows",
]

JOURNAL_FIELDS: list[str] = [
    "signal_date", "entry_date", "code", "name", "cb", "is_st", "board",
    "close_T", "limit_T", "amount_T", "avg_amount_5d", "rank", "selected",
    "status", "open_T1", "limit_up_T1", "limit_dn_T1", "fill_price",
    "fill_time", "shares", "open_limit_down", "slippage_open_bps",
    "close_T1", "sell_price", "sell_date", "deferred", "defer_count",
    "slippage_close_bps", "gross_ret", "net_fixed", "net_actual", "notes",
]

PENDING = "pending"
FILLED = "filled"
DEFERRED_UNSOLD = "deferred_unsold"
BOOK_B_EXCLUDED = {
    PENDING, "unbuyable_open_limit", "suspended", "new_stock", "corp_action",
    "risk_warning", "missed", "data_error", DEFERRED_UNSOLD,
}


@dataclass(frozen=True)
class PaperConfig:
    version: str = "v1.0"
    created: str = ""
    start_date: str = ""
    capital: float = 1_000_000.0
    min_cb: int = 3
    max_new_per_day: int = 3
    position_pct: float = 0.10
    cost_fixed: float = 0.003
    fee_bps: float = 2.5
    stamp_bps: float = 5.0
    slip_bps: float = 0.0
    min_commission: float = 5.0
    limit_tol: float = 0.005
    new_stock_days: int = 60
    max_defer_days: int = 2

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PaperConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_date(value: Any) -> str:
    """Journal 日期口径：YYYYMMDD。"""
    ts = parse_date(value)
    if ts is None or pd.isna(ts):
        raise ValueError(f"无法解析日期: {value!r}")
    return ts.strftime("%Y%m%d")


def _iso_date(value: Any) -> str:
    """parquet/内存日线日期口径：YYYY-MM-DD。"""
    ts = parse_date(value)
    if ts is None or pd.isna(ts):
        raise ValueError(f"无法解析日期: {value!r}")
    return ts.strftime("%Y-%m-%d")


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def board_tag(code: str, name: str = "", on_date: Any = None) -> str:
    """板块标签。主板风险警示票 2026-07-06 起由 5% 并轨 10%，标签跟着日期走。"""
    market = market_of(code)
    if market == MARKET_BSE:
        return "bse30"
    if market == MARKET_GEM:
        return "gem20"
    if not is_st(name):
        return "main10"
    d = as_date(on_date)
    return "st5" if d is not None and d < MAIN_ST_RULE_CHANGE_DATE else "st10"


def empty_journal() -> pd.DataFrame:
    return pd.DataFrame({f: pd.Series(dtype=object) for f in JOURNAL_FIELDS})


def enrich_history(
    df: pd.DataFrame,
    code: str,
    name: str = "",
    *,
    new_stock_days: int = 60,
    limit_tol: float = 0.005,
) -> pd.DataFrame:
    """为单股日线补齐 prev_close/涨跌停价/sealed/cb/hist_n/5 日均额。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=[
            "date", "open", "high", "low", "close", "volume", "amount",
            "code", "name", "ratio", "prev_close", "suspended", "hist_n",
            "limit_up", "limit_down", "sealed", "cb", "avg_amount_5d",
            "suspect",
        ])
    out = df.copy().reset_index(drop=True)
    out["date"] = parse_date_series(out["date"]).dt.strftime("%Y-%m-%d")
    out = (out.sort_values("date", kind="mergesort")
              .drop_duplicates("date", keep="last")
              .reset_index(drop=True))
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce")

    code = normalize_code(code)
    name = "" if name is None else str(name)
    ratio = limit_ratio(code, name)
    out["code"] = code
    out["name"] = name

    # 主板风险警示票的涨跌幅在 2026-07-06 从 5% 并轨 10%，同一只票跨规则日要按行取比例。
    ratio_arr = np.full(len(out), float(ratio), dtype=float)
    if market_of(code) == MARKET_MAIN and is_st(name):
        ratio_arr = np.array([
            float(limit_ratio(code, name, on_date=d)) for d in out["date"]
        ], dtype=float)
    out["ratio"] = ratio_arr
    out["prev_close"] = out["close"].shift(1)

    out["suspended"] = (
        (out["volume"].fillna(0) <= 0)
        | (out["open"].fillna(0) <= 0)
        | (out["close"].fillna(0) <= 0)
    )
    out["hist_n"] = (~out["suspended"]).cumsum().astype(int)

    prev = out["prev_close"].to_numpy(dtype=float)
    valid_prev = np.isfinite(prev) & (prev > 0)
    up = np.zeros(len(out), dtype=float)
    down = np.zeros(len(out), dtype=float)
    up[valid_prev] = np.floor(
        prev[valid_prev] * (1.0 + ratio_arr[valid_prev]) * 100.0 + 0.5 + 1e-7) / 100.0
    down[valid_prev] = np.floor(
        prev[valid_prev] * (1.0 - ratio_arr[valid_prev]) * 100.0 + 0.5 + 1e-7) / 100.0
    out["limit_up"] = up
    out["limit_down"] = down
    out["sealed"] = (
        (out["close"].to_numpy(dtype=float) >= up - limit_tol)
        & (up > 0)
        & (~out["suspended"].to_numpy(dtype=bool))
    )

    # 断板归零；停牌日既不计入也不重置（group 不变，sealed=0 保持原值）
    breaks = (~out["suspended"]) & (~out["sealed"])
    groups = breaks.cumsum()
    out["cb"] = out["sealed"].groupby(groups).cumsum().astype(int)

    amount = out["amount"].where(out["amount"] > 0)
    out["avg_amount_5d"] = amount.rolling(5, min_periods=1).mean().fillna(0.0)

    prev = out["prev_close"].where(out["prev_close"] > 0)
    open_jump = (out["open"] / prev - 1.0).abs()
    amplitude = (out["high"] - out["low"]) / prev
    out["suspect"] = (
        (open_jump > out["ratio"] + 0.05) | (amplitude > 2.5 * out["ratio"])
    ).fillna(False).astype(bool)

    if new_stock_days >= 0:
        out.attrs["new_stock_days"] = new_stock_days
    if limit_tol >= 0:
        out.attrs["limit_tol"] = limit_tol
    return out


def generate_signals(
    histories: Mapping[str, pd.DataFrame],
    date: Any,
    config: PaperConfig,
) -> list[dict[str, Any]]:
    """生成某交易日的 cb≥3 信号，已按规则排序并标记执行账本入选。"""
    day = _iso_date(date)
    signal_date = normalize_date(date)
    rows: list[dict[str, Any]] = []
    for code, frame in histories.items():
        if frame is None or len(frame) == 0 or "date" not in frame:
            continue
        dates = frame["date"].to_numpy()
        pos = int(np.searchsorted(dates, day))
        if pos >= len(dates) or dates[pos] != day:
            continue
        rec = frame.iloc[pos]
        cb = int(_num(rec.get("cb")))
        if not bool(rec.get("sealed")) or cb < config.min_cb:
            continue

        name = str(rec.get("name") or "")
        close_t = _num(rec.get("close"))
        limit_t = _num(rec.get("limit_up"))
        notes: list[str] = []
        status = PENDING
        if close_t <= 0 or limit_t <= 0:
            status = "data_error"
            notes.append("bad_close")
        elif bool(rec.get("suspended")):
            status = "suspended"
        elif int(_num(rec.get("hist_n"))) < config.new_stock_days:
            status = "new_stock"
            notes.append(f"hist_n={int(_num(rec.get('hist_n')))}")
        elif "退" in name:
            status = "risk_warning"
        elif bool(rec.get("suspect")):
            status = "corp_action"
            notes.append("price_jump")

        ratio = limit_ratio(code, name)
        row = {f: "" for f in JOURNAL_FIELDS}
        row.update({
            "signal_date": signal_date,
            "code": code,
            "name": name,
            "cb": cb,
            "is_st": int(is_st(name)),
            "board": board_tag(code, name),
            "close_T": close_t,
            "limit_T": limit_t,
            "amount_T": _num(rec.get("amount")),
            "avg_amount_5d": _num(rec.get("avg_amount_5d")),
            "rank": 0,
            "selected": 0,
            "status": status,
            "limit_up_T1": limit_up_price(close_t, ratio) if close_t > 0 else 0.0,
            "limit_dn_T1": limit_down_price(close_t, ratio) if close_t > 0 else 0.0,
            "deferred": 0,
            "defer_count": 0,
            "notes": "|".join(notes),
        })
        rows.append(row)

    eligible = [r for r in rows if r["status"] == PENDING]
    eligible.sort(key=lambda r: (-int(r["cb"]), -float(r["amount_T"]), r["code"]))
    for i, row in enumerate(eligible, start=1):
        row["rank"] = i
        row["selected"] = int(i <= config.max_new_per_day)

    rows.sort(key=lambda r: (r["rank"] == 0, r["rank"], r["code"]))
    return rows


def _get(bar: Any, key: str) -> Any:
    if bar is None:
        return None
    if isinstance(bar, pd.Series):
        return bar.get(key)
    return bar.get(key)


def _net_actual(fill: float, sell: float, shares: int, config: PaperConfig) -> float:
    """按固定 10% 名义本金估算实际成本净收益（纸盘成交价即官方价）。"""
    notional = config.position_pct * config.capital
    if fill <= 0 or sell <= 0 or notional <= 0:
        return float("nan")
    if shares >= 100:
        buy_amt = fill * shares
        sell_amt = sell * shares
        base = buy_amt
    else:
        base = notional
        buy_amt = notional
        sell_amt = notional * sell / fill
    fee = config.fee_bps / 1e4
    stamp = config.stamp_bps / 1e4
    slip = config.slip_bps / 1e4
    commission = max(config.min_commission, buy_amt * fee) + max(
        config.min_commission, sell_amt * fee)
    tax = sell_amt * stamp
    slippage = buy_amt * slip + sell_amt * slip
    return (sell_amt - buy_amt - commission - tax - slippage) / base


def _actual_pnl(fill: float, sell: float, shares: int, config: PaperConfig) -> float:
    buy_amt = fill * shares
    sell_amt = sell * shares
    fee = config.fee_bps / 1e4
    stamp = config.stamp_bps / 1e4
    slip = config.slip_bps / 1e4
    commission = max(config.min_commission, buy_amt * fee) + max(
        config.min_commission, sell_amt * fee)
    return (sell_amt - buy_amt - commission - sell_amt * stamp
            - buy_amt * slip - sell_amt * slip)


def _append_note(row: dict[str, Any], note: str) -> None:
    old = str(row.get("notes") or "").strip()
    row["notes"] = f"{old}|{note}" if old else note


def _apply_sale(
    row: dict[str, Any],
    sell: float,
    sell_date: str,
    ref_price: float,
    config: PaperConfig,
    note: str = "",
) -> None:
    fill = _num(row.get("fill_price"))
    if fill <= 0 or sell <= 0:
        row["status"] = "data_error"
        _append_note(row, "bad_sell")
        return
    gross = sell / fill - 1.0
    shares = int(_num(row.get("shares")))
    row["sell_price"] = round(sell, 4)
    row["sell_date"] = sell_date
    row["gross_ret"] = round(gross, 8)
    row["net_fixed"] = round(gross - config.cost_fixed, 8)
    row["net_actual"] = round(_net_actual(fill, sell, shares, config), 8)
    row["slippage_close_bps"] = (
        round((sell / ref_price - 1.0) * 1e4, 2) if ref_price > 0 else 0.0
    )
    if note:
        _append_note(row, note)


def execute_signal(
    row: Mapping[str, Any],
    bar_t1: Any,
    config: PaperConfig,
) -> dict[str, Any]:
    """执行单条 pending 信号（T+1 开盘买入 / 收盘卖出或顺延）。"""
    out = {f: row.get(f, "") for f in JOURNAL_FIELDS}
    if str(out.get("status")) != PENDING:
        return out

    if bar_t1 is None:
        out["status"] = "suspended"
        out["shares"] = 0
        _append_note(out, "no_t1_bar")
        return out

    entry_date = normalize_date(_get(bar_t1, "date"))
    open_p = _num(_get(bar_t1, "open"))
    close_p = _num(_get(bar_t1, "close"))
    volume = _num(_get(bar_t1, "volume"))
    out["entry_date"] = entry_date
    out["open_T1"] = round(open_p, 4)

    if open_p <= 0 or volume <= 0:
        out["status"] = "suspended"
        _append_note(out, "t1_suspended")
        return out

    limit_up_t1 = _num(out.get("limit_up_T1"))
    limit_dn_t1 = _num(out.get("limit_dn_T1"))
    tol = config.limit_tol
    ratio = float(limit_ratio(str(out.get("code")), str(out.get("name") or "")))
    close_t = _num(out.get("close_T"))
    if close_t > 0 and (
        abs(open_p / close_t - 1.0) > ratio + 0.05
        or (close_p > 0 and abs(close_p / close_t - 1.0) > ratio + 0.05)
    ):
        out["status"] = "corp_action"
        _append_note(out, "t1_price_jump")
        return out
    out["open_limit_down"] = int(
        limit_dn_t1 > 0 and open_p <= limit_dn_t1 + tol)
    if limit_up_t1 > 0 and open_p >= limit_up_t1 - tol:
        out["status"] = "unbuyable_open_limit"
        out["shares"] = 0
        return out

    out["fill_price"] = round(open_p, 4)
    out["fill_time"] = "09:25"
    out["slippage_open_bps"] = 0.0

    shares = 0
    if int(_num(out.get("selected"))) == 1:
        budget = config.position_pct * config.capital
        shares = int(math.floor(budget / open_p / 100.0) * 100)
        if shares < 100:
            out["status"] = "size_too_small"
            shares = 0
        else:
            out["status"] = FILLED
    else:
        out["status"] = "not_selected"
    out["shares"] = shares

    if close_p <= 0:
        out["status"] = "data_error"
        _append_note(out, "no_t1_close")
        return out
    out["close_T1"] = round(close_p, 4)

    if limit_dn_t1 > 0 and close_p <= limit_dn_t1 + tol:
        out["deferred"] = 1
        out["defer_count"] = 0
        _append_note(out, "t1_close_limit_down")
        return out

    _apply_sale(out, close_p, entry_date, close_p, config)
    return out


def settle_deferred(
    row: Mapping[str, Any],
    bars_after: Sequence[Any] | None,
    config: PaperConfig,
) -> dict[str, Any]:
    """顺延持仓：T+2 开盘/收盘，T+3 开盘/收盘（最迟 T+3 收盘）。"""
    out = {f: row.get(f, "") for f in JOURNAL_FIELDS}
    if int(_num(out.get("deferred"))) != 1 or not _is_blank(out.get("sell_price")):
        return out
    if str(out.get("status")) == DEFERRED_UNSOLD:
        return out

    bars = list(bars_after or [])
    max_days = max(int(config.max_defer_days), 1)
    if not bars:
        return out

    fill = _num(out.get("fill_price"))
    if fill <= 0:
        out["status"] = "data_error"
        _append_note(out, "bad_fill")
        return out

    for i, bar in enumerate(bars[:max_days]):
        day_no = i + 1                       # 1=T+2, 2=T+3
        if bar is None:
            continue
        open_p = _num(_get(bar, "open"))
        close_p = _num(_get(bar, "close"))
        volume = _num(_get(bar, "volume"))
        if open_p <= 0 or close_p <= 0 or volume <= 0:
            continue
        limit_dn = _num(_get(bar, "limit_down"))
        if limit_dn <= 0:
            prev = _num(_get(bar, "prev_close"))
            if prev > 0:
                limit_dn = limit_down_price(
                    prev, limit_ratio(str(out.get("code")),
                                      str(out.get("name") or "")))
        tol = config.limit_tol
        is_last = day_no >= max_days
        bar_date = normalize_date(_get(bar, "date"))

        if limit_dn <= 0 or open_p > limit_dn + tol:
            out["defer_count"] = day_no
            _apply_sale(out, open_p, bar_date, open_p, config,
                        f"t{day_no + 1}_open")
            return out
        if close_p > limit_dn + tol:
            out["defer_count"] = day_no
            _apply_sale(out, close_p, bar_date, open_p, config,
                        f"t{day_no + 1}_close")
            return out
        if is_last:
            out["defer_count"] = day_no
            _apply_sale(out, close_p, bar_date, open_p, config,
                        f"t{day_no + 1}_close_forced")
            return out
        out["defer_count"] = day_no

    if len(bars) < max_days:
        return out
    out["status"] = DEFERRED_UNSOLD
    _append_note(out, "no_executable_bar")
    return out


def _bar_at(frame: pd.DataFrame | None, day: str) -> dict[str, Any] | None:
    if frame is None or len(frame) == 0 or "date" not in frame:
        return None
    hit = frame[frame["date"] == day]
    if hit.empty:
        return None
    return hit.iloc[-1].to_dict()


def _calendar(calendar: Iterable[Any]) -> list[str]:
    return sorted({_iso_date(d) for d in calendar})


def _next_date(calendar: Sequence[str], day: str) -> str | None:
    pos = bisect_right(calendar, day)
    return calendar[pos] if pos < len(calendar) else None


def _assign(journal: pd.DataFrame, idx: Any, row: Mapping[str, Any]) -> None:
    for f in JOURNAL_FIELDS:
        journal.at[idx, f] = row.get(f, "")


def execute_pending(
    journal: pd.DataFrame,
    histories: Mapping[str, pd.DataFrame],
    calendar: Sequence[Any],
    day: Any,
    config: PaperConfig,
) -> pd.DataFrame:
    """在 day 开盘执行 signal_date 的下一交易日为 day 的所有 pending 信号。"""
    out = journal.copy()
    if out.empty:
        return out
    day = _iso_date(day)
    cal = _calendar(calendar)
    pending = out[out["status"].astype(str) == PENDING]
    for idx, row in pending.iterrows():
        sig_date = _iso_date(row["signal_date"])
        if _next_date(cal, sig_date) != day:
            continue
        code = str(row["code"]).zfill(6)
        bar = _bar_at(histories.get(code), day)
        _assign(out, idx, execute_signal(row, bar, config))
    return out


def settle_journal(
    journal: pd.DataFrame,
    histories: Mapping[str, pd.DataFrame],
    calendar: Sequence[Any],
    as_of: Any,
    config: PaperConfig,
) -> pd.DataFrame:
    """把截至 as_of 已可结算的顺延持仓按规则卖出。"""
    out = journal.copy()
    if out.empty:
        return out
    as_of = _iso_date(as_of)
    cal = _calendar(calendar)
    mask = (
        (out["deferred"].map(lambda v: int(_num(v)) == 1))
        & (out["sell_price"].map(_is_blank))
        & (out["status"].astype(str) != DEFERRED_UNSOLD)
    )
    for idx, row in out[mask].iterrows():
        code = str(row["code"]).zfill(6)
        entry = row.get("entry_date")
        if _is_blank(entry):
            continue
        entry = _iso_date(entry)
        d2 = _next_date(cal, entry)
        if d2 is None or d2 > as_of:
            continue
        d3 = _next_date(cal, d2)
        bars: list[Any] = []
        for d in (d2, d3):
            if d is None or d > as_of:
                break
            bars.append(_bar_at(histories.get(code), d))
        if not bars:
            continue
        _assign(out, idx, settle_deferred(row, bars, config))
    return out


def daily_report(journal: pd.DataFrame, config: PaperConfig) -> dict[str, Any]:
    j = journal.copy() if journal is not None else empty_journal()
    status_counts = (
        {str(k): int(v) for k, v in j["status"].astype(str).value_counts().items()}
        if not j.empty else {}
    )

    book_b = j[~j["status"].astype(str).isin(BOOK_B_EXCLUDED)].copy()
    if not book_b.empty:
        book_b["_fill"] = book_b["fill_price"].map(_num)
        book_b["_sell"] = book_b["sell_price"].map(_num)
        book_b["_net_fixed"] = book_b["net_fixed"].map(_num)
        book_b["_gross"] = book_b["gross_ret"].map(_num)
        book_b["_net_actual"] = book_b["net_actual"].map(_num)
        book_b = book_b[(book_b["_fill"] > 0) & (book_b["_sell"] > 0)]

    if book_b.empty:
        book_b_stats = {
            "n_trades": 0, "n_days": 0, "avg_gross": None,
            "avg_net_fixed": None, "avg_net_actual": None,
            "win_rate": None, "std": None, "t_stat": None,
        }
    else:
        daily = book_b.groupby("entry_date")["_net_fixed"].mean()
        n_days = len(daily)
        std_daily = float(daily.std(ddof=1)) if n_days > 1 else 0.0
        t_stat = (
            float(daily.mean() / (std_daily / math.sqrt(n_days)))
            if n_days > 1 and std_daily > 0 else None
        )
        book_b_stats = {
            "n_trades": len(book_b),
            "n_days": n_days,
            "avg_gross": float(book_b["_gross"].mean()),
            "avg_net_fixed": float(book_b["_net_fixed"].mean()),
            "avg_net_actual": float(book_b["_net_actual"].mean()),
            "win_rate": float((book_b["_net_fixed"] > 0).mean()),
            "std": float(book_b["_net_fixed"].std(ddof=1))
            if len(book_b) > 1 else 0.0,
            "t_stat": t_stat,
        }

    book_a = j[
        (j["status"].astype(str) == FILLED)
        & (j["selected"].map(lambda v: int(_num(v)) == 1))
    ].copy()
    if not book_a.empty:
        book_a["_fill"] = book_a["fill_price"].map(_num)
        book_a["_sell"] = book_a["sell_price"].map(_num)
        book_a["_shares"] = book_a["shares"].map(lambda v: int(_num(v)))
        book_a = book_a[
            (book_a["_fill"] > 0) & (book_a["_sell"] > 0)
            & (book_a["_shares"] >= 100)
        ]
    if book_a.empty:
        book_a_stats = {
            "n_trades": 0, "pnl": 0.0, "total_return": 0.0,
            "max_drawdown": 0.0, "win_rate": None, "avg_net_actual": None,
        }
    else:
        book_a["_pnl"] = [
            _actual_pnl(f, s, n, config)
            for f, s, n in zip(book_a["_fill"], book_a["_sell"], book_a["_shares"], strict=True)
        ]
        ordered = book_a.sort_values("sell_date")
        equity = config.capital + ordered["_pnl"].cumsum()
        curve = pd.concat([pd.Series([config.capital]), equity], ignore_index=True)
        drawdown = float((curve / curve.cummax() - 1.0).min())
        book_a_stats = {
            "n_trades": len(book_a),
            "pnl": float(book_a["_pnl"].sum()),
            "total_return": float(book_a["_pnl"].sum() / config.capital),
            "max_drawdown": drawdown,
            "win_rate": float((book_a["_pnl"] > 0).mean()),
            "avg_net_actual": float(book_a["net_actual"].map(_num).mean()),
        }

    return {
        "book_a": book_a_stats,
        "book_b": book_b_stats,
        "status_counts": status_counts,
        "n_signals": len(j),
        "capital": float(config.capital),
    }


def upsert_rows(
    journal: pd.DataFrame,
    rows: Sequence[Mapping[str, Any]],
    *,
    replace_pending: bool = True,
) -> pd.DataFrame:
    """按 (signal_date, code) 去重；默认只覆盖仍为 pending 的旧行。"""
    out = journal.copy() if journal is not None else empty_journal()
    if not rows:
        return out[list(JOURNAL_FIELDS)]
    for row in rows:
        key_date = normalize_date(row.get("signal_date"))
        key_code = normalize_code(str(row.get("code")))
        mask = (
            (out["signal_date"].astype(str).str.replace("-", "") == key_date)
            & (out["code"].astype(str).map(lambda c: normalize_code(c)) == key_code)
        )
        if mask.any():
            idx = out.index[mask][0]
            if (replace_pending and str(out.at[idx, "status"]) == PENDING
                    and str(row.get("status") or PENDING) == PENDING):
                _assign(out, idx, row)
            continue
        if out.empty:
            out = pd.DataFrame(
                [{f: row.get(f, "") for f in JOURNAL_FIELDS}],
                columns=JOURNAL_FIELDS,
            )
        else:
            out = pd.concat(
                [out, pd.DataFrame([{f: row.get(f, "") for f in JOURNAL_FIELDS}])],
                ignore_index=True,
            )
    return out[list(JOURNAL_FIELDS)]


def backfill(
    histories: Mapping[str, pd.DataFrame],
    calendar: Sequence[Any],
    start: Any,
    end: Any,
    config: PaperConfig,
) -> pd.DataFrame:
    """按交易日回放：开盘执行 → 顺延结算 → 收盘生成信号。"""
    start_day, end_day = _iso_date(start), _iso_date(end)
    days = [d for d in _calendar(calendar) if start_day <= d <= end_day]
    journal = empty_journal()
    for day in days:
        journal = execute_pending(journal, histories, calendar, day, config)
        journal = settle_journal(journal, histories, calendar, day, config)
        rows = generate_signals(histories, day, config)
        journal = upsert_rows(journal, rows, replace_pending=False)
    return journal


def calendar_of(histories: Mapping[str, pd.DataFrame]) -> list[str]:
    dates: set[str] = set()
    for frame in histories.values():
        if frame is not None and len(frame) and "date" in frame:
            dates.update(frame["date"].astype(str))
    return sorted(dates)


def load_histories(
    daily_dir: str | Path | None = None,
    universe_path: str | Path | None = None,
    *,
    codes: Iterable[str] | None = None,
    min_date: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """载入股票池日线并 enrich；min_date 只裁剪结果，不裁剪计算历史。"""
    universe = load_universe(universe_path or UNIVERSE_PATH)
    if codes is not None:
        wanted = {normalize_code(c) for c in codes}
        universe = {c: n for c, n in universe.items() if c in wanted}
    floor = _iso_date(min_date) if min_date else None
    out: dict[str, pd.DataFrame] = {}
    for code, name in universe.items():
        path = daily_path(code, daily_dir)
        if path is None or not Path(path).exists():
            continue
        try:
            raw = pd.read_parquet(path)
            frame = enrich_history(raw, code, name)
        except Exception:
            continue
        if frame.empty:
            continue
        if floor:
            frame = frame[frame["date"] >= floor].reset_index(drop=True)
            if frame.empty:
                continue
        out[code] = frame
    return out


def load_config(paper_dir: str | Path) -> PaperConfig:
    path = Path(paper_dir) / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少纸盘配置: {path}（先运行 init）")
    return PaperConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_config(config: PaperConfig, paper_dir: str | Path) -> Path:
    path = Path(paper_dir) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def init_paper(
    paper_dir: str | Path,
    start_date: Any,
    capital: float = 1_000_000.0,
    *,
    overwrite: bool = False,
) -> PaperConfig:
    root = Path(paper_dir)
    config_path = root / "config.json"
    if config_path.exists() and not overwrite:
        raise FileExistsError(f"纸盘已初始化: {config_path}")
    config = PaperConfig(
        created=_date.today().isoformat(),
        start_date=normalize_date(start_date),
        capital=float(capital),
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "signals").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    save_config(config, root)
    journal_path = root / "journal.csv"
    if overwrite or not journal_path.exists():
        save_journal(empty_journal(), root)
    return config


def load_journal(paper_dir: str | Path) -> pd.DataFrame:
    path = Path(paper_dir) / "journal.csv"
    if not path.exists():
        return empty_journal()
    df = pd.read_csv(
        path,
        dtype={"code": str, "signal_date": str, "entry_date": str,
               "sell_date": str},
        keep_default_na=False,
    )
    for f in JOURNAL_FIELDS:
        if f not in df.columns:
            df[f] = ""
    return df[list(JOURNAL_FIELDS)]


def save_journal(journal: pd.DataFrame, paper_dir: str | Path) -> Path:
    path = Path(paper_dir) / "journal.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    journal.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path
