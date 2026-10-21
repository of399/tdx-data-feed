#!/usr/bin/env python
"""盘中打板扫描 CLI。

用法::

    # 单次扫描（09:30-10:00 盘中最有效）
    venv/bin/python scripts/daban_scan.py --top 20

    # 指定市场环境（影响仓位档位与打分）
    venv/bin/python scripts/daban_scan.py --phase 启动期

    # 持续盯盘（默认 3s 一轮），实时跟踪封板/炸板/回封 + 撤单速率
    venv/bin/python scripts/daban_scan.py --watch --interval 3

    # 量化初筛闭环：predict.py Top50 作候选池 + 盘口确认（计划书 §D.5）
    venv/bin/python scripts/daban_scan.py --pool predict --pool-top 50

    # 真实封单比：导入通达信导出的封单快照（附录 E 第 1 阶段）
    venv/bin/python scripts/daban_scan.py --seal-export data/daban/tdx_seal_20261020.csv

    # 生成信号单（落盘，人工复核后手工下单；不真实下单）
    venv/bin/python scripts/daban_scan.py --broker dryrun --cash 200000

产物：
    data/daban/scan_YYYYMMDD_HHMMSS.csv   全部候选明细
    data/daban/candidates_YYYYMMDD.blk    通达信"明日候选"板块（每行一个 sh/sz/bj 代码）
    data/signals/market_phase_YYYYMMDD.json  市场环境
    data/daban/orders_*.csv / .json       信号单（--broker dryrun）

对齐鲁《A股打板交易策略执行计划书》§2.6 / §3.2 / §A.1 / §A.2 / §C.4 / §D.5。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.auction_log import append_auction_rows  # noqa: E402
from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.order import DryRunBroker, build_intents, condition_order_text  # noqa: E402
from tdxfeed.daban.phase import PhaseStats, write_phase  # noqa: E402
from tdxfeed.daban.pool import load_pool  # noqa: E402
from tdxfeed.daban.realtime import fetch_quote_map  # noqa: E402
from tdxfeed.daban.scanner import Scanner  # noqa: E402
from tdxfeed.daban.seal_ratio import load_tdx_export  # noqa: E402

DATA_DIR = _ROOT / "data"
OUT_DIR = DATA_DIR / "daban"
SIGNAL_DIR = DATA_DIR / "signals"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A股打板实时扫描器")
    p.add_argument("--phase", default="震荡市", help="市场环境（退潮期/冰点转折/启动期/高潮期/上升期/熊市/震荡市）")
    p.add_argument("--min-pct", type=float, default=4.0, help="涨跌幅门槛%%，低于不进候选")
    p.add_argument("--top", type=int, default=20, help="打印前 N 条")
    p.add_argument("--out-dir", default=str(OUT_DIR), help="输出目录")
    p.add_argument("--no-blk", action="store_true", help="不写通达信 .blk")
    p.add_argument("--allow-st", action="store_true", help="保留 ST 股")
    p.add_argument("--float-shares", default="", help="流通股本 CSV（code,float_shares）")
    p.add_argument("--pool", default="", help="候选池：predict / predict:<path> / file:<path> / block:<path>")
    p.add_argument("--pool-top", type=int, default=0, help="候选池只取前 N 个（0=全部）")
    p.add_argument("--session", default="auto", choices=["auto", "auction", "continuous"], help="交易时段")
    p.add_argument("--seal-export", default="", help="通达信导出的封单快照 CSV（真实封单比）")
    p.add_argument("--no-sector", action="store_true", help="关闭板块强度 Sec 因子")
    p.add_argument(
        "--min-sector-sec",
        type=float,
        default=0.0,
        help="板块 Sec 门槛：低于此值不进候选（§1.1 只做最强主线）",
    )
    p.add_argument(
        "--min-sector-limitup",
        type=int,
        default=0,
        help="板块共振门槛：当日同板块涨停家数下限（实测 sec>=60 & 共振>=2 期望 +0.37pp/笔）",
    )
    p.add_argument("--watch", action="store_true", help="持续盯盘模式")
    p.add_argument("--interval", type=float, default=3.0, help="盯盘轮询间隔秒")
    p.add_argument("--broker", default="none", choices=["none", "dryrun"], help="下单通道（dryrun=落盘信号单）")
    p.add_argument("--cash", type=float, default=100000.0, help="可用资金（元）")
    p.add_argument("--max-single-pct", type=float, default=0.15, help="单票上限（占总资金）")
    p.add_argument("--max-total-pct", type=float, default=0.50, help="总仓上限（占总资金）")
    p.add_argument("--stop-loss", type=float, default=0.05, help="止损线（相对买入价）")
    p.add_argument("--take-profit", type=float, default=0.10, help="止盈线（相对买入价）")
    p.add_argument("--min-order-amount", type=float, default=10000.0, help="单笔最小金额（元），低于则不下单")
    return p.parse_args()


def load_float_shares(path: str) -> dict[str, float]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f"[warn] 流通股本文件不存在: {path}", file=sys.stderr)
        return {}
    out: dict[str, float] = {}
    for line in p.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            out[parts[0].strip().zfill(6)] = float(parts[1])
        except ValueError:
            continue
    return out


def print_table(result, top: int) -> None:
    if not result.candidates:
        print("（无候选：可能未开盘 / 已收盘 / 涨跌幅门槛过高）")
        return
    header = (
        f"{'代码':<8}{'名称':<10}{'状态':<8}{'板':<9}{'涨幅%':>7}"
        f"{'涨停价':>8}{'封单(万股)':>11}{'封单(万元)':>11}{'封单比%':>9}{'档位':>8}"
        f"{'换手%':>8}{'量比':>6}{'板块':<12}{'Sec':>5}{'题材':<12}{'题材强度':>8}"
        f"{'连板':>5}{'撤单/分':>8}{'得分':>7}"
    )
    print(header)
    print("-" * len(header.encode("gbk", errors="ignore")))
    for c in result.candidates[:top]:
        sector = (c.sector_name or c.sector_code or "-")[:12]
        ratio = (
            c.seal_ratio_amount * 100
            if c.seal_ratio_amount is not None
            else c.seal_vs_vol5 * 100
        )
        turn = f"{c.turnover * 100:.1f}" if c.turnover is not None else "-"
        concept = (c.concept_name or "-")[:10]
        print(
            f"{c.code:<8}{c.name:<10}{c.status:<8}{c.board:<9}{c.pct:>7.2f}"
            f"{c.limit_price:>8.2f}{c.seal_shares / 10000:>11.1f}"
            f"{c.seal_amount / 10000:>11.0f}{ratio:>9.2f}{c.seal_grade:<8}"
            f"{turn:>8}{c.vol_ratio:>6.1f}{sector:<12}{c.sector_sec:>5.0f}{concept:<12}"
            f"{c.concept_strength:>8.1f}{c.consecutive:>5}{c.cancel_rate:>8.0f}{c.score:>7.3f}"
        )
    print("-" * 40)
    print(
        f"环境={result.phase} 时段={result.session} 候选={len(result.candidates)} "
        f"统计={result.stats}"
    )


def print_concepts(board, top: int = 10) -> None:
    """输出今日题材（概念）强度榜 —— 主线判断（§2.1）。"""
    if not board:
        return
    print("\n=== 题材强度 Top（同花顺概念口径）===")
    print(f"{'概念':<16}{'涨幅%':>8}{'涨幅排名':>10}{'资金净流入亿':>13}{'强度':>7}")
    for c in board[:top]:
        print(
            f"{c.concept_name[:14]:<16}{c.pct:>8.2f}{c.rank:>10}"
            f"{c.net_inflow:>13.1f}{c.strength:>7.1f}"
        )


def print_sectors(result, top: int = 8) -> None:
    if not result.sector_stats:
        return
    print("\n=== 板块强度 Top（Sec = 40×涨幅排名 + 40×涨停家数 + 20×领涨）===")
    print(f"{'板块':<14}{'成分':>6}{'涨停':>6}{'中位涨幅%':>11}{'领涨%':>9}{'Sec':>7}")
    for s in result.sector_stats[:top]:
        name = (s["sector_name"] or s["sector_code"])[:12]
        print(
            f"{name:<14}{s['member_n']:>6}{s['limit_up_n']:>6}"
            f"{s['median_pct']:>11.2f}{s['leader_pct']:>9.2f}{s['sec']:>7.1f}"
        )


def write_outputs(result, out_dir: Path, top: int, write_blk: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{result.trade_date}_{time.strftime('%H%M%S')}"
    csv_path = out_dir / f"scan_{stamp}.csv"
    frame = result.to_frame()
    if not frame.empty:
        frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"[ok] 明细 CSV: {csv_path}")
    if write_blk and not frame.empty:
        blk_path = out_dir / f"candidates_{result.trade_date}.blk"
        codes = []
        for c in result.candidates[:top]:
            sym = f"{exchange_prefix(c.code)}{c.code}"
            if sym not in codes:
                codes.append(sym)
        blk_path.write_text("\n".join(codes) + "\n", encoding="utf-8")
        print(f"[ok] 通达信板块: {blk_path}（{len(codes)} 只）→ 导入 T0002/blocknew/")


def log_auction(result) -> None:
    """竞价期快照落盘（无历史源，前瞻性积累样本）。"""
    if result.session != "auction" or not result.candidates:
        return
    rows = []
    for c in result.candidates:
        rows.append(
            {
                "trade_date": result.trade_date,
                "time": result.scan_time,
                "code": c.code,
                "name": c.name,
                "price": c.price,
                "limit_price": c.limit_price,
                "seal_shares": c.seal_shares,
                "seal_amount": c.seal_amount,
                "pct": c.pct,
                "auction_sealed": c.status == "竞价封板",
            }
        )
    path = append_auction_rows(rows)
    print(f"[ok] 竞价快照 {len(rows)} 条 → {path}（回测请只用 09:20 之后的记录）")


def emit_orders(result, args, out_dir: Path) -> None:
    """生成信号单（不真实下单）。"""
    intents = build_intents(
        result.candidates,
        cash=args.cash,
        max_single_pct=args.max_single_pct,
        max_total_pct=args.max_total_pct,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        min_amount=args.min_order_amount,
    )
    if not intents:
        print("[warn] 无可下单候选（涨停价无效或资金不足）")
        return
    broker = DryRunBroker(out_dir / "orders")
    for intent in intents:
        res = broker.place(intent)
    print(f"[ok] 信号单 {len(intents)} 条 → {res['csv']}")
    print("     条件单文本（可粘进通达信/券商条件单）：")
    for intent in intents[:5]:
        print("       " + condition_order_text(intent))


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)

    pool_codes = load_pool(args.pool, args.pool_top) if args.pool else []
    seal_map = load_tdx_export(args.seal_export) if args.seal_export else {}

    scanner = Scanner(
        float_shares=load_float_shares(args.float_shares),
        phase=args.phase,
        min_pct=args.min_pct,
        allow_st=args.allow_st,
        use_sector=not args.no_sector,
        seal_ratio_map=seal_map,
        session_mode=args.session,
        min_sector_sec=args.min_sector_sec,
        min_sector_limitup=args.min_sector_limitup,
    )
    symbols = scanner.symbols()
    if pool_codes:
        keep = set(pool_codes)
        symbols = [s for s in symbols if s[2:] in keep]
    print(
        f"[info] 股票池 {len(symbols)} 只 · 环境={args.phase} · 门槛={args.min_pct}%"
        f" · 候选池={args.pool or '全市场'}({len(pool_codes)})"
        f" · 外部封单={len(seal_map)}"
    )

    if not args.watch:
        quotes = fetch_quote_map(symbols)
        result = scanner.scan(quotes)
        print_table(result, args.top)
        print_sectors(result)
        print_concepts(scanner.concept_board)
        log_auction(result)
        write_outputs(result, out_dir, args.top, not args.no_blk)
        write_phase(
            PhaseStats(
                limit_up_n=result.stats.get("sealed_candidates", 0),
                limit_down_n=result.stats.get("broken_candidates", 0),
                max_consecutive=result.stats.get("max_consecutive", 0),
                trade_date=result.trade_date,
            ),
            SIGNAL_DIR,
            phase=args.phase,
        )
        if args.broker == "dryrun":
            emit_orders(result, args, out_dir)
        return 0

    # 盯盘模式：轮询维护状态机，识别回封 + 跟踪撤单速率
    print(f"[watch] 进入盯盘模式，间隔 {args.interval}s，Ctrl-C 退出")
    try:
        while True:
            quotes = fetch_quote_map(symbols)
            result = scanner.scan(quotes)
            reseal = [c for c in result.candidates if c.status in ("回封", "竞价封板")]
            print(
                f"\n[{result.scan_time}] 时段={result.session} 候选 {len(result.candidates)} · "
                f"封板 {result.stats.get('sealed_candidates', 0)} · "
                f"炸板 {result.stats.get('broken_candidates', 0)} · "
                f"回封/竞价 {len(reseal)}"
            )
            for c in reseal[:10]:
                print(
                    f"  ★ {c.status} {c.code} {c.name} "
                    f"封单{c.seal_shares / 10000:.0f}万股 板块{c.sector_sec:.0f} 得分{c.score}"
                )
            risky = [c for c in result.candidates if c.cancel_rate > 200][:5]
            for c in risky:
                print(f"  ⚠ 撤单加速 {c.code} {c.name} {c.cancel_rate:.0f}万股/分")
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("\n[watch] 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
