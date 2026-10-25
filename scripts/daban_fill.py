#!/usr/bin/env python
"""daban 试验仓：人工成交回填台账（停止线的唯一数据源）。

用法::

    # 委托单挂出去、排到了：按券商回单的成交价/股数/费用回填
    venv/bin/python scripts/daban_fill.py --code 600001 --fill --price 10.00 --shares 1000 --fee 5.00

    # 没排到（封死、撤单、来不及）——这一行必须记，否则样本只剩「买到的那批」
    venv/bin/python scripts/daban_fill.py --code 600002 --no-fill --reason 封死未成交

    # 卖出（T+1 之后）；不给 --fee 就不算平仓，宁可让它不进均值
    venv/bin/python scripts/daban_fill.py --code 600001 --sell --price 11.00 --fee 6.00 --date 2026-10-27

    venv/bin/python scripts/daban_fill.py --report

口径（设计 §4.3 / §7）：净额 = 券商回单的真实费用之后的差额，缺任何一个费用字段就不置
`closed`；**不臆造费率**。未成交行只进成交率、不进均值。台账落在
`data/daban/tickets/fills_YYYYMMDD.csv`，交易日取自当日委托单里出现该 code 的那份文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban import trial  # noqa: E402

COLUMNS = [
    "code", "ticket_price", "ticket_shares", "filled", "fill_price", "shares",
    "fee_buy", "sell_date", "sell_price", "fee_sell", "net_pct", "net_yuan", "closed", "note",
]


def _find_ticket(root: Path, code: str) -> tuple[str, dict] | None:
    """在最新的往前的 `ticket_*.json` 里找该 code 的委托行，返回 (交易日, 行)。"""
    for fp in sorted(trial.tickets_dir(root).glob("ticket_*.json"), reverse=True):
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for t in payload.get("tickets", []):
            if str(t.get("code")) == code:
                return str(payload.get("trade_date")), t
    return None


def _codes_of_latest_day(root: Path) -> list[str]:
    for fp in sorted(trial.tickets_dir(root).glob("ticket_*.json"), reverse=True):
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        codes = [str(t.get("code")) for t in payload.get("tickets", [])]
        if codes:
            return codes
    return []


def _read_rows(root: Path, trade_date: str) -> dict[str, dict]:
    fp = trial.fills_path(root, trade_date)
    rows: dict[str, dict] = {}
    if fp.exists():
        with fp.open(encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                rows[str(rec["code"])] = rec
    return rows


def _write_rows(root: Path, trade_date: str, rows: dict[str, dict]) -> Path:
    fp = trial.fills_path(root, trade_date)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for rec in rows.values():
            writer.writerow({c: rec.get(c, "") for c in COLUMNS})
    tmp.replace(fp)
    return fp


def _net(row: dict) -> tuple[float, float] | None:
    """两边费用齐了才算净额；否则返回 None（该笔保持未平仓，不进均值）。"""
    try:
        fill_price = float(row["fill_price"])
        shares = float(row["shares"])
        fee_buy = float(row["fee_buy"])
        sell_price = float(row["sell_price"])
        fee_sell = float(row["fee_sell"])
    except (TypeError, ValueError, KeyError):
        return None
    cost = fill_price * shares + fee_buy
    principal = fill_price * shares
    if principal <= 0:
        return None
    proceeds = sell_price * shares - fee_sell
    net_yuan = proceeds - cost
    return net_yuan, net_yuan / principal * 100.0


def _blank(ticket: dict) -> dict:
    row = {c: "" for c in COLUMNS}
    row.update(
        code=str(ticket["code"]),
        ticket_price=f"{float(ticket.get('price', 0.0)):.2f}",
        ticket_shares=str(int(ticket.get("shares", 0))),
        filled="0",
        closed="0",
    )
    return row


def record(root: Path, code: str, *, fill=False, no_fill=False, sell=False, price=None,
           fee=None, shares=None, sell_date=None, reason=None) -> str:
    """改一笔台账并落盘，返回交易日。找不到对应委托就 sys.exit。"""
    found = _find_ticket(root, code)
    if found is None:
        codes = _codes_of_latest_day(root)
        sys.exit(
            f"❌ {code} 不在任何一份委托单里，拒绝凭空造一笔台账。\n"
            f"   最近一份委托单的 code：{', '.join(codes) if codes else '（当日还没有委托单）'}"
        )
    trade_date, ticket = found
    rows = _read_rows(root, trade_date)
    row = rows.get(code) or _blank(ticket)

    if no_fill:
        row["filled"] = "0"
        row["note"] = reason or "未成交"
    if fill:
        row["filled"] = "1"
        row["fill_price"] = f"{float(price):.2f}"
        row["shares"] = str(int(shares)) if shares else row["ticket_shares"]
        row["fee_buy"] = f"{float(fee):.2f}" if fee is not None else ""
    if sell:
        row["sell_price"] = f"{float(price):.2f}"
        row["sell_date"] = sell_date or ""
        row["fee_sell"] = f"{float(fee):.2f}" if fee is not None else ""
        net = _net(row)
        if net is None:
            row["closed"] = "0"
            row["net_pct"] = ""
            row["net_yuan"] = ""
        else:
            row["closed"] = "1"
            row["net_yuan"] = f"{net[0]:.2f}"
            row["net_pct"] = f"{net[1]:.4f}"

    rows[code] = row
    _write_rows(root, trade_date, rows)
    return trade_date


def _ticket_rows(root: Path) -> list[dict]:
    out = []
    for fp in sorted(trial.tickets_dir(root).glob("ticket_*.json")):
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for t in payload.get("tickets", []):
            out.append({"trade_date": str(payload.get("trade_date")), "code": str(t.get("code"))})
    return out


def report(root: Path) -> int:
    tickets = _ticket_rows(root)
    fills = trial.load_fills(root)
    by_code = {str(r["code"]): r for r in fills}

    missing = [t for t in tickets if t["code"] not in by_code]
    open_sells = [
        r for r in fills
        if str(r.get("sell_price") or "").strip() and int(r.get("closed") or 0) == 0
    ]
    rows_for_stop = [
        {**r, "net_yuan": (r.get("net_yuan") if r.get("net_yuan") is not None else 0.0)}
        for r in fills
    ]
    v = trial.evaluate_stop(rows_for_stop)

    print("=== daban 试验仓 回填报告 ===")
    print(f"委托单：{len(tickets)} 张 / {len({t['trade_date'] for t in tickets})} 个交易日")
    print(f"已回填：{len(fills)} 笔；未回填：{len(missing)} 笔"
          + (f"（{', '.join(t['code'] for t in missing[:10])}）" if missing else ""))
    if missing:
        print("   ⚠️ 未回填的笔不算样本，也不进成交率——每天收盘后补记，统计才可信。")
    print(f"成交 {v.n_backfilled} 笔 / 未成交 {len(fills) - v.n_backfilled} 笔"
          f"（未成交占比 {v.miss_ratio if v.miss_ratio is not None else '—'}）")
    print(f"已平仓 pair：{v.n_closed} 笔；净均值 "
          f"{f'{v.mean_pct:.3f}%/笔' if v.mean_pct is not None else '样本不足'}；"
          f"累计已实现 {v.realized_yuan:.0f} 元")
    if open_sells:
        print(f"缺费用未计入均值：{len(open_sells)} 笔（{', '.join(str(r['code']) for r in open_sells)}）")
        print("   规则：券商回单费用没填全就不置 closed——不臆造费率。")
    print("对照口径：纸盘规则 §8.2 的停止线是 +0.20%/笔；本试验仓有意放宽到 −0.20%/笔"
          "（设计 §7 第 3 条）。"
          + ("按纸盘口径当前均值已低于那条线。" if v.mean_pct is not None and v.mean_pct < 0.20 else
             "按纸盘口径当前均值不低于那条线。" if v.mean_pct is not None else
             "样本不足，两个口径都还不便判断。"))
    if v.triggered:
        print(f"🛑 停止线：{v.rule}｜{v.detail}")
        fp = trial.write_marker(root, v.marker_text)
        print(f"   已写 marker：{fp}")
    else:
        print(f"停止线：未触发（{v.detail}）")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="daban 试验仓成交回填（净额只扣真实回单费用）")
    p.add_argument("--root", default=str(_ROOT))
    p.add_argument("--code")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--fill", action="store_true", help="回填买入成交（配 --price）")
    mode.add_argument("--no-fill", action="store_true", help="挂了没排到")
    mode.add_argument("--sell", action="store_true", help="回填卖出（配 --price）")
    mode.add_argument("--report", action="store_true", help="汇总并判停止线")
    p.add_argument("--price", type=float, help="成交价或卖出价（券商回单）")
    p.add_argument("--shares", type=int, help="成交股数，默认取委托单里的股数")
    p.add_argument("--fee", type=float, help="该笔动作对应的真实费用（买入费或卖出费）")
    p.add_argument("--date", help="卖出日期")
    p.add_argument("--reason", help="未成交原因")
    args = p.parse_args(argv)

    root = Path(args.root)
    if args.report:
        return report(root)
    if not args.code:
        p.error("--report 之外必须给 --code")
    if not (args.fill or args.sell or args.no_fill):
        p.error("要指定 --fill / --sell / --no-fill 之一")
    if (args.fill or args.sell) and args.price is None:
        p.error("--fill / --sell 必须配 --price（券商回单价格）")

    trade_date = record(
        root, args.code, fill=args.fill, no_fill=args.no_fill, sell=args.sell,
        price=args.price, fee=args.fee, shares=args.shares, sell_date=args.date, reason=args.reason,
    )
    verb = "未成交" if args.no_fill else ("买入回填" if args.fill else "卖出回填")
    print(f"[ok] {verb} 已记入 {trade_date} 台账：{args.code}")
    if args.sell:
        rows = _read_rows(root, trade_date)
        closed = int(rows[args.code].get("closed") or 0)
        print("     该笔已平仓，净额已计入均值。" if closed else
              "     费用不齐，该笔保持未平仓、不进均值（--report 会点名）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
