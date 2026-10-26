#!/usr/bin/env python
"""DABAN cb≥3 接力纸盘自动化 CLI。

规则：docs/DABAN_PAPER_TRADE_RULES_20261022.md

典型日更（收盘后）::

    venv/bin/python scripts/daban_paper.py daily --date 20261022

等价于：执行上一交易日 pending 信号（按今日开盘）→ 结算顺延 → 生成今日信号。

首次初始化::

    venv/bin/python scripts/daban_paper.py init --start-date 20261023 --capital 1000000

其他::

    scripts/daban_paper.py signals --date 20261022
    scripts/daban_paper.py execute --date 20261022
    scripts/daban_paper.py report
    scripts/daban_paper.py backfill --start 20250101 --end 20261021 --reset
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from tdxfeed.daban import paper  # noqa: E402
from tdxfeed.daban.scanner import DAILY_DIR, UNIVERSE_PATH  # noqa: E402

DEFAULT_PAPER_DIR = ROOT / "data/daban/paper"
SIGNAL_FIELDS = [
    "signal_date", "code", "name", "cb", "is_st", "board",
    "close_T", "limit_T", "amount_T", "avg_amount_5d",
    "limit_up_T1", "limit_dn_T1", "rank", "selected", "status", "notes",
]


def _write_signals(rows: list[dict], day: str, paper_dir: Path) -> Path:
    out_dir = paper_dir / "signals"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"signals_{paper.normalize_date(day)}.csv"
    df = pd.DataFrame(rows, columns=SIGNAL_FIELDS)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)
    return path


def _print_report(rep: dict, title: str = "纸盘报告") -> None:
    b = rep["book_b"]
    a = rep["book_a"]
    print(f"\n{title}")
    print(f"  信号总数: {rep['n_signals']}  状态: {rep['status_counts']}")
    if b["n_trades"]:
        line = (
            f"  Book B（全部可评估信号）: n={b['n_trades']} days={b['n_days']} "
            f"net_fixed={b['avg_net_fixed']:+.4%} win={b['win_rate']:.1%}"
        )
        if b["t_stat"] is not None:
            line += (f" gross={b['avg_gross']:+.4%}"
                     f" net_actual={b['avg_net_actual']:+.4%}"
                     f" t={b['t_stat']:.2f}")
        print(line)
    else:
        print("  Book B: 暂无可评估成交")
    if a["n_trades"]:
        print(
            f"  Book A（执行账本）: n={a['n_trades']} pnl={a['pnl']:+,.2f} "
            f"ret={a['total_return']:+.4%} maxDD={a['max_drawdown']:.2%}"
        )
    else:
        print("  Book A: 暂无成交")


def _load(paper_dir: Path, daily_dir: Path, universe: Path,
          min_date=None, codes=None):
    config = paper.load_config(paper_dir)
    journal = paper.load_journal(paper_dir)
    histories = paper.load_histories(daily_dir, universe, min_date=min_date,
                                     codes=codes)
    if not histories:
        raise SystemExit(f"未读到任何日线: {daily_dir}")
    return config, journal, histories, paper.calendar_of(histories)


def cmd_init(args: argparse.Namespace) -> int:
    try:
        config = paper.init_paper(args.paper_dir, args.start_date,
                                  args.capital, overwrite=args.force)
    except FileExistsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"纸盘已初始化: {args.paper_dir}")
    print(f"  start_date={config.start_date} capital={config.capital:,.2f}")
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    config, journal, histories, _ = _load(
        args.paper_dir, args.daily_dir, args.universe)
    rows = paper.generate_signals(histories, args.date, config)
    path = _write_signals(rows, args.date, args.paper_dir)
    journal = paper.upsert_rows(journal, rows)
    paper.save_journal(journal, args.paper_dir)
    selected = sum(int(r["selected"]) for r in rows)
    print(f"{paper.normalize_date(args.date)} 信号 {len(rows)} 条，"
          f"执行账本入选 {selected} 条 -> {path}")
    for r in rows:
        flag = "买" if int(r["selected"]) else "-"
        print(f"  {flag} {r['code']} {r['name']} cb={r['cb']} "
              f"close={r['close_T']:.2f} limit={r['limit_T']:.2f} "
              f"rank={r['rank']} {r['status']}")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    journal = paper.load_journal(args.paper_dir)
    needed = set(journal.loc[
        journal["status"].astype(str).eq(paper.PENDING)
        | journal["deferred"].map(lambda v: int(paper._num(v)) == 1),
        "code",
    ].astype(str))
    needed.add("600000")  # 用于补全交易日历
    config, journal, histories, calendar = _load(
        args.paper_dir, args.daily_dir, args.universe, codes=needed)
    day = paper.normalize_date(args.date)
    journal = paper.execute_pending(journal, histories, calendar, day, config)
    journal = paper.settle_journal(journal, histories, calendar, day, config)
    paper.save_journal(journal, args.paper_dir)
    today = journal[
        journal["entry_date"].astype(str).str.replace("-", "") == day
    ]
    counts = today["status"].astype(str).value_counts().to_dict()
    print(f"{day} 执行/结算完成: {counts or '无待执行信号'}")
    if len(today):
        for _, r in today.iterrows():
            print(f"  {r['code']} {r['name']} {r['status']} "
                  f"fill={r['fill_price']} sell={r['sell_price']} "
                  f"gross={r['gross_ret']}")
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    config, journal, histories, calendar = _load(
        args.paper_dir, args.daily_dir, args.universe)
    day = paper.normalize_date(args.date)
    journal = paper.execute_pending(journal, histories, calendar, day, config)
    journal = paper.settle_journal(journal, histories, calendar, day, config)
    rows = paper.generate_signals(histories, day, config)
    _write_signals(rows, day, args.paper_dir)
    journal = paper.upsert_rows(journal, rows)
    paper.save_journal(journal, args.paper_dir)
    today = journal[
        journal["entry_date"].astype(str).str.replace("-", "") == day
    ]
    counts = today["status"].astype(str).value_counts().to_dict()
    print(f"{day} 执行: {counts or '无待执行信号'}")
    print(f"{day} 新信号 {len(rows)} 条，入选 "
          f"{sum(int(r['selected']) for r in rows)} 条")
    for r in rows:
        flag = "买" if int(r["selected"]) else "-"
        print(f"  {flag} {r['code']} {r['name']} cb={r['cb']} "
              f"close={r['close_T']:.2f} limit={r['limit_T']:.2f} "
              f"rank={r['rank']} {r['status']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = paper.load_config(args.paper_dir)
    journal = paper.load_journal(args.paper_dir)
    rep = paper.daily_report(journal, config)
    _print_report(rep)
    out_dir = args.paper_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = paper.normalize_date(args.date) if args.date else "latest"
    path = out_dir / f"report_{stamp}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n报告已写入 {path}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    config = paper.load_config(args.paper_dir)
    journal_path = args.paper_dir / "journal.csv"
    if journal_path.exists() and not args.reset:
        print(f"错误: {journal_path} 已存在；回填会重建账本，需显式 --reset",
              file=sys.stderr)
        return 2
    start_dt = datetime.strptime(paper.normalize_date(args.start), "%Y%m%d")
    min_date = (start_dt - timedelta(days=120)).strftime("%Y%m%d")
    histories = paper.load_histories(args.daily_dir, args.universe,
                                     min_date=min_date)
    if not histories:
        print(f"错误: 未读到任何日线: {args.daily_dir}", file=sys.stderr)
        return 2
    calendar = paper.calendar_of(histories)
    journal = paper.backfill(histories, calendar, args.start, args.end, config)
    paper.save_journal(journal, args.paper_dir)
    for day, group in journal.groupby("signal_date", sort=True):
        _write_signals(group.to_dict("records"), day, args.paper_dir)
    rep = paper.daily_report(journal, config)
    _print_report(rep, f"回填报告 {paper.normalize_date(args.start)}"
                       f" ~ {paper.normalize_date(args.end)}")
    out_dir = args.paper_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (
        f"backfill_{paper.normalize_date(args.start)}_"
        f"{paper.normalize_date(args.end)}.json"
    )
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n账本 {len(journal)} 行 -> {journal_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DABAN cb≥3 接力纸盘")
    p.add_argument("--paper-dir", type=Path, default=DEFAULT_PAPER_DIR,
                   help=f"纸盘目录（默认 {DEFAULT_PAPER_DIR}）")
    p.add_argument("--daily-dir", type=Path, default=DAILY_DIR,
                   help="日线 parquet 目录")
    p.add_argument("--universe", type=Path, default=UNIVERSE_PATH,
                   help="股票池 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化纸盘配置与空账本")
    s.add_argument("--start-date", required=True, help="开始日期 YYYYMMDD")
    s.add_argument("--capital", type=float, default=1_000_000.0)
    s.add_argument("--force", action="store_true", help="覆盖已有配置与账本")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("signals", help="生成某交易日信号（收盘后）")
    s.add_argument("--date", required=True)
    s.set_defaults(func=cmd_signals)

    s = sub.add_parser("execute", help="执行 pending 信号并结算顺延")
    s.add_argument("--date", required=True, help="T+1 交易日")
    s.set_defaults(func=cmd_execute)

    s = sub.add_parser("daily", help="日更：执行 + 顺延结算 + 生成新信号")
    s.add_argument("--date", required=True, help="当日交易日（收盘后运行）")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("report", help="输出 Book A/B 统计")
    s.add_argument("--date", default=None)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("backfill", help="按历史日线回放验证（会重建账本）")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--reset", action="store_true")
    s.set_defaults(func=cmd_backfill)
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.paper_dir = Path(args.paper_dir).expanduser().resolve()
    args.daily_dir = Path(args.daily_dir).expanduser().resolve()
    args.universe = Path(args.universe).expanduser().resolve()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
