#!/usr/bin/env python
"""daban 试验仓：盘中**单次轮询**入口（由 `daban-ticket.timer` 每 5 分钟拉起，无常驻进程）。

用法::

    venv/bin/python scripts/daban_entry.py                 # 一轮（timer 用的就是这条）
    venv/bin/python scripts/daban_entry.py --phase 启动期  # 换市场环境标签
    venv/bin/python scripts/daban_entry.py --root /tmp/x    # 彩排用，不碰真实目录

设计：docs/superpowers/specs/2026-10-25-daban-daily-ticket-design.md

顺序是固定的，前四步都**早于任何网络调用**：

1. STOP marker 在 → 退出（停止线的唯一权威，见 `tdxfeed/daban/trial.py`）
2. 时段门：不在连续竞价 → 退出。timer 的 `OnCalendar` 只能粗放到手小时，精门在这里
3. 板块过滤：北交所不进试验仓（`build_intents` 只有一个全局 `lot=100`）
4. 交易日门：以行情自带的 `trade_date` 为准，不等于本地今天就整轮跳过
5. 扫描 → 按「当日剩余额度」出意图 → 首触发生效去重 → 落 `data/daban/tickets/`
6. 复核停止线（与 `daban_fill.py` 共用 `trial.evaluate_stop` 这一个实现）

产出的是**给人抄进券商 App 的委托单文本**。本脚本不下单、不接 `QmtBroker`、
不进 `plan_e_journal` 的实盘账本通道，`DABAN_LIVE_BLOCK` 那条决定不受本脚本影响。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban import trial  # noqa: E402
from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.realtime import fetch_quote_map  # noqa: E402
from tdxfeed.daban.scanner import Scanner  # noqa: E402
from tdxfeed.daban.session import SESSION_CONTINUOUS, current_session  # noqa: E402

# 试验仓参数（§3，与 trial.py 的额度常量配套；改这里要同时改设计文档）
PHASE = "震荡市"  # --phase 是市场环境标签，不是时刻
MIN_PCT = 4.0
ALLOW_ST = False
EXCLUDED_PREFIXES = ("bj",)  # §3 第 3 条：整手口径不适用的板块


def _default_build_scanner(**kwargs):
    return Scanner(**kwargs)


def _limit_src_map(scanner, intents) -> dict[str, str]:
    """逐票记录涨停价来源：腾讯源挂了会静默回退（scanner.py:394），人工得能看出来。"""
    tquotes = getattr(scanner, "tquotes", None) or {}
    out = {}
    for it in intents:
        code = str(getattr(it, "code", "") or "")
        out[code] = "腾讯官方" if f"{exchange_prefix(code)}{code}" in tquotes else "新浪L1推算"
    return out


def _concept_map(candidates, intents) -> dict[str, str]:
    """把候选的概念标签带到账本行上——跨轮限注读的是账本，不是本轮候选列表。

    概念来自 `Candidate.concept_name`（`OrderIntent` 没这个字段，故在这里注入，
    与 `limit_src` 同一注入点）。空标签写空串，`concept_counts` 会跳过它。
    """
    by_code = {str(getattr(c, "code", "") or ""): trial.concept_of(c) for c in candidates}
    return {str(it.code): by_code.get(str(it.code), "") for it in intents}


def poll_once(root: Path, now: datetime | None = None, *, phase: str = PHASE,
              build_scanner: Callable | None = None, fetch: Callable | None = None) -> int:
    """跑一轮。返回 0 = 本轮无事可做或正常出票；抛异常 = 真故障（让 OnFailure= 能叫）。"""
    root = Path(root)
    moment = now or datetime.now()

    if trial.marker_exists(root):
        print(f"[stop] 自动停止线已触发，本轮不扫描、不出票。\n{trial.read_marker(root)}")
        return 0

    session = current_session(moment)
    if session != SESSION_CONTINUOUS:
        print(f"[gate] 时段={session}，不是连续竞价，本轮不扫描（{moment:%H:%M}）")
        return 0

    scanner = (build_scanner or _default_build_scanner)(
        phase=phase, min_pct=MIN_PCT, allow_st=ALLOW_ST, use_sector=True, session_mode="auto"
    )
    symbols = [s for s in scanner.symbols() if not str(s).startswith(EXCLUDED_PREFIXES)]
    if not symbols:
        print("[gate] 过滤后股票池为空，本轮跳过")
        return 0

    fetcher = fetch or fetch_quote_map
    try:
        quotes = fetcher(symbols)
        result = scanner.scan(quotes)
    except Exception as exc:  # 行情源挂了是常态：本轮 0 票，但不算故障（§6）
        print(f"[warn] 行情层异常，本轮 0 票：{type(exc).__name__}: {exc}")
        return 0

    # `Scanner._trade_date` 有两种形状：新浪 `Quote.date` 是 `2026-10-26`，只有行情缺日期时
    # 才回退成 `%Y%m%d`。先归一化成紧凑形——既给交易日门比，也当 `ticket_YYYYMMDD` 的日文件键
    # （`daban_fill.py` 与停止线都按那个形状找对象）。
    trade_date = str(result.trade_date).replace("-", "").strip()
    if trade_date != moment.strftime("%Y%m%d"):
        print(f"[gate] 行情日期 {trade_date} 不等于本地今天 {moment:%Y%m%d}（休市或数据滞后），整轮跳过")
        return 0

    day = trial.load_day(root, trade_date)
    _kept, capped = trial.candidates_under_cap(result.candidates, day)
    labelled = sum(1 for c in result.candidates if trial.concept_of(c))
    if capped:
        print(
            f"[cap] 概念限注（每概念 {trial.CONCEPT_CAP} 笔/日）本轮跳过 {len(capped)} 只候选："
            f"{', '.join(sorted({trial.concept_of(c) for c in capped}))}"
        )
    elif not labelled:
        # 全榜无标签 = 限注一次都没生效。静默通过会被读成「没有超限」，所以这里必须明说。
        print(f"[cap] 概念快照无标签（0/{len(result.candidates)} 只），每概念 {trial.CONCEPT_CAP} 笔的限注未生效")

    intents = trial.intents_for_poll(result.candidates, day)
    if not intents:
        print(
            f"[ok] 候选 {len(getattr(result, 'candidates', []) or [])} 只，"
            f"本轮 0 票（已用额度 {trial.committed_amount(day):.0f}/{trial.TOTAL_CAP:.0f} 元，"
            f"剩 {trial.remaining_budget(day):.0f} 元）"
        )
        return 0

    res = trial.upsert_tickets(
        root,
        trade_date,
        intents,
        day=day,
        limit_src_map=_limit_src_map(scanner, intents),
        concept_map=_concept_map(result.candidates, intents),
    )
    used = trial.committed_amount(res.day)
    print(
        f"[ticket] {trade_date} 新增 {len(res.new)} 只 / 重复丢弃 {len(res.dropped)} 只；"
        f"当日已用 {used:.0f}/{trial.TOTAL_CAP:.0f} 元 → {trial.tickets_dir(root)}"
    )
    for it in res.new:
        print(f"    {it.code} {it.name} 委托价 {it.price:.2f} × {it.shares} 股 = {it.amount:.0f} 元")

    verdict = trial.evaluate_stop(trial.load_fills(root))
    if verdict.triggered:
        trial.write_marker(root, verdict.marker_text)
        print(f"[stop] 停止线触发，已写 marker：{verdict.rule}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="daban 试验仓单次轮询（产委托单文本，不下单）")
    p.add_argument("--phase", default=PHASE, help="市场环境标签（不是时刻），默认 震荡市")
    p.add_argument("--root", default=str(_ROOT), help="仓库根（彩排时可指到临时目录）")
    p.add_argument(
        "--at",
        help="假装现在是这个 ISO 时刻（非交易时段彩排用）。定时器不传，走系统时钟",
    )
    args = p.parse_args(argv)
    now = datetime.fromisoformat(args.at) if args.at else None
    return poll_once(Path(args.root), now=now, phase=args.phase)


if __name__ == "__main__":
    raise SystemExit(main())
