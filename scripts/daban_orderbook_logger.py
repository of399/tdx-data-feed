#!/usr/bin/env python
"""打板预警盘口盯盘器：把盘中"排队/封单"维度落成历史里根本没有的数据。

为什么要这个脚本：纸盘账本和日线只有"今天封没封住"的日频结果，看不到封单额曲线、
卖一什么时候被砸、是不是一字。Stage 2 测出的逆向选择（成交样本封板率仅 39.7%）
正是缺这一维；新浪 L1 只在实时给，所以只能盘中前瞻记录。

用法::

    # 盘中全市场盯盘（默认 3 秒一轮，事件驱动 + 30 秒心跳落盘）
    venv/bin/python scripts/daban_orderbook_logger.py

    # 只盯自选池（通达信板块文件 / CSV），节流更宽，文件更小
    venv/bin/python scripts/daban_orderbook_logger.py --pool block:~/blk/me.blk --pool-top 50

    # 早盘触板门槛放宽（默认 5%；触板→封板的整个过程都要留痕）
    venv/bin/python scripts/daban_orderbook_logger.py --min-pct 3 --heartbeat 20

    # 盘后冒烟：抓一次新浪最后一帧，验证落盘格式
    venv/bin/python scripts/daban_orderbook_logger.py --once

产物：
    data/daban/orderbook/orderbook_YYYYMMDD.csv   盘口快照（当日追加，含五档/封单额/成交额）

⚠ 与 ``scripts/daban_scan.py --watch`` **二选一**跑：两者都是全市场轮询，同时跑
等于把同一个 5000 只的代码列表打两遍新浪，容易触发限流且时间戳对不齐。

对齐鲁《A股打板交易策略执行计划书》§2.6（盘口要素）/ 附录 E（前瞻积累）。
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.orderbook_log import (  # noqa: E402
    ORDERBOOK_DIR,
    OrderbookTracker,
    compact_date,
    poll_once,
)
from tdxfeed.daban.pool import load_pool  # noqa: E402
from tdxfeed.daban.scanner import Scanner  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="打板预警盘口快照记录器")
    p.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒（最小 1）")
    p.add_argument("--min-pct", type=float, default=5.0, help="涨跌幅门槛%%，低于不进候选")
    p.add_argument("--heartbeat", type=float, default=30.0,
                   help="封板状态下兜底心跳间隔秒（0=只在状态/封单变化时写）")
    p.add_argument("--change-pct", type=float, default=0.2,
                   help="封单额相对上次写入变化比例阈值，触发 SEAL_GROW/SHRINK")
    p.add_argument("--phase", default="震荡市", help="市场环境标签（只影响打分）")
    p.add_argument("--pool", default="", help="候选池：predict / predict:<path> / file:<path> / block:<path>")
    p.add_argument("--pool-top", type=int, default=0, help="候选池只取前 N 个（0=全部）")
    p.add_argument("--float-shares", default="", help="流通股本 CSV（code,float_shares），金额法封单比")
    p.add_argument("--seal-export", default="", help="通达信导出的封单快照 CSV，优先于 L1")
    p.add_argument("--no-tencent", action="store_true", help="不查腾讯源（少一路网络，但换手/量比会缺）")
    p.add_argument("--out-dir", default=str(ORDERBOOK_DIR), help="盘口快照输出目录")
    p.add_argument("--once", action="store_true", help="只抓一轮就退出（盘后冒烟/格式核对）")
    p.add_argument("--max-polls", type=int, default=0, help="最多轮询 N 次后退出（0=不限）")
    return p.parse_args()


def filter_symbols(symbols: Sequence[str], pool: Iterable[str] | None) -> list[str]:
    """按候选池（6 位代码）过滤轮询列表，保持原顺序。"""
    codes = {str(c).zfill(6) for c in (pool or [])}
    if not codes:
        return list(symbols)
    return [s for s in symbols if s[-6:] in codes]


def make_daily_last_date_reader(daily_dir: Path):
    """返回 code → 本地日线最后交易日（紧凑 YYYYMMDD），拿不到给空串。

    连板数必须用它校正：当天的日线有的票已入库、有的还停在昨天，scanner 的
    ``consecutive`` 因此可能少算一天（实测 2026-10-23 有 33 只与东财口径差 1）。
    """
    cache: dict[str, str] = {}

    def read(code: str) -> str:
        if code in cache:
            return cache[code]
        path = Path(daily_dir) / f"{exchange_prefix(code)}{code}.parquet"
        last = ""
        if path.exists():
            try:
                import pandas as pd

                dates = pd.read_parquet(path, columns=["date"])["date"].astype(str)
                if len(dates):
                    last = compact_date(dates.max())
            except Exception:
                last = ""
        cache[code] = last
        return last

    return read


def _board_hint(row: dict) -> str:
    cb = int(row.get("cb") or row.get("consecutive") or 0)
    if cb <= 1:
        return "首板"
    if cb == 2:
        return "2板"
    return "3板+"


def format_event(row: dict) -> str:
    """一行预警文本（盯盘时人眼要能在 3 秒内扫完）。"""
    price = float(row.get("price") or 0.0)
    limit = float(row.get("limit_price") or 0.0)
    seal_amount = float(row.get("seal_amount") or 0.0)
    seal_shares = float(row.get("seal_shares") or 0.0)
    ask1 = float(row.get("ask1_vol") or 0.0)
    cancel = float(row.get("cancel_rate") or 0.0)
    marks = [_board_hint(row)]
    if int(row.get("is_yizi") or 0):
        marks.append("一字")
    if int(row.get("seal_dead") or 0):
        marks.append("封死")
    breaks = int(row.get("break_count") or 0)
    if breaks:
        marks.append(f"炸{breaks}次")
    return (
        f"{row.get('quote_time', '')} {row.get('event', '')} "
        f"{row.get('code', '')} {row.get('name', '')} {row.get('board', '')} "
        f"{price:.2f}/{limit:.2f} "
        f"封单{seal_amount / 1e8:.2f}亿/{seal_shares / 1e4:.1f}万股 "
        f"卖一{ask1:.0f} "
        f"{' '.join(marks)} "
        f"撤单{cancel:g}万股/分 分{float(row.get('score') or 0.0):.2f}"
    )


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


def main() -> int:
    from tdxfeed.daban.seal_ratio import load_tdx_export

    args = parse_args()
    pool_codes = load_pool(args.pool, args.pool_top) if args.pool else []
    scanner = Scanner(
        float_shares=load_float_shares(args.float_shares),
        phase=args.phase,
        min_pct=args.min_pct,
        allow_st=True,
        use_sector=False,
        seal_ratio_map=load_tdx_export(args.seal_export) if args.seal_export else {},
        use_tencent=not args.no_tencent,
    )
    symbols = filter_symbols(scanner.symbols(), pool_codes)
    if not symbols:
        print("[err] 股票池为空（检查 --pool 路径）", file=sys.stderr)
        return 1

    tracker = OrderbookTracker(
        heartbeat_sec=args.heartbeat,
        change_pct=args.change_pct,
        last_daily_date=make_daily_last_date_reader(scanner.daily_dir),
    )
    print(
        f"[info] 轮询 {len(symbols)} 只 · 门槛 {args.min_pct}% · 心跳 {args.heartbeat:g}s · "
        f"封单变化阈值 {args.change_pct * 100:.0f}% · 候选池={args.pool or '全市场'}({len(pool_codes)})"
    )
    print(f"[info] 落盘 {Path(args.out_dir) / 'orderbook_YYYYMMDD.csv'}（当日追加）")

    polls = 0
    total_rows = 0
    last_path = None
    try:
        while True:
            polls += 1
            try:
                summary = poll_once(
                    symbols,
                    scan=scanner.scan,
                    out_dir=Path(args.out_dir),
                    tracker=tracker,
                )
            except Exception as err:  # 网络抖动不能打断整天的记录
                print(f"[warn] 第 {polls} 轮失败：{err}", file=sys.stderr)
                if args.once:
                    return 1
                time.sleep(max(1.0, args.interval))
                continue

            total_rows += summary["rows"]
            last_path = summary["path"]
            stats = summary["stats"]
            print(
                f"\n[{summary['scan_time']}] 时段={summary['session']} 候选 {summary['candidates']} · "
                f"封板 {stats.get('sealed_candidates', 0)} · 炸板 {stats.get('broken_candidates', 0)} · "
                f"写行 {summary['rows']} · 累计 {total_rows}"
            )
            for rec in summary["records"]:
                print("  ★", format_event(rec))
            if args.once:
                print(f"\n[ok] {last_path}")
                return 0
            if args.max_polls and polls >= args.max_polls:
                print(f"\n[ok] 跑了 {polls} 轮，{total_rows} 行 → {last_path}")
                return 0
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print(f"\n[watch] 退出：{polls} 轮 / {total_rows} 行 → {last_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
