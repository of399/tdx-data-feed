"""daban 试验仓的**额度、幂等与停止线**（设计 docs/superpowers/specs/2026-10-25-daban-daily-ticket-design.md）。

本模块是纯逻辑：不联网、不读行情、不 import 行情层。可测的部分全在这里，
`scripts/daban_entry.py` 只负责按顺序调用它。

为什么需要这一层：`tdxfeed/daban/order.py` 的 `build_intents` 已经会把候选切成订单意图，
但它的入参是**比例**，而试验仓的约束是**当日绝对额**（总仓 2 万 / 单票 1 万）。
timer 每个交易日命中约 48 轮，每轮各自按满仓预算出票，日累计就会冲到近百只——
所以每轮先把「当日还剩多少钱」算出来，再把比例参数反算成绝对额交给它。

设计边界：本模块**不做**下单、不接 `QmtBroker`、不进 `DABAN_LIVE_BLOCK` 那条实盘账本通道，
产出的委托单文本由人工在券商端执行。
"""
from __future__ import annotations

import copy
import csv
import json
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path

from tdxfeed.daban.order import OrderIntent, build_intents, condition_order_text

__all__ = [
    "BANNER",
    "BANNER_HEAD",
    "CASH",
    "LOT",
    "MIN_ORDER_AMOUNT",
    "SINGLE_CAP",
    "STOP_LOSS_PCT",
    "TAKE_PROFIT_PCT",
    "TOTAL_CAP",
    "UpsertResult",
    "add_tickets",
    "build_kwargs",
    "committed_amount",
    "evaluate_stop",
    "intents_for_poll",
    "load_day",
    "marker_exists",
    "marker_path",
    "read_marker",
    "remaining_budget",
    "stop_lines_text",
    "tickets_dir",
    "upsert_tickets",
    "write_marker",
]

# ============== 试验仓参数（2026-10-25 与用户逐条确认） ==============
CASH = 200000.0  # 本金
TOTAL_CAP = 20000.0  # 总仓上限 = 本金 × 10%
SINGLE_CAP = 10000.0  # 单票上限 = 本金 × 5%
MIN_ORDER_AMOUNT = 5000.0  # 必须 < SINGLE_CAP，否则一笔都出不来
STOP_LOSS_PCT = 0.05
TAKE_PROFIT_PCT = 0.10
LOT = 100

# 停止线（五条，任一命中即停）
STOP_N_FINAL = 30  # 1. 样本量到即停：试验仓的期限本身
STOP_N_MIN = 10  # 2. 前 10 笔不判均值
STOP_MEAN_PCT = -0.20  # 3. 已平仓净均值（%/笔）低于此值即停
STOP_LOSS_YUAN = 0.20 * TOTAL_CAP  # 4. 真金止损：累计已实现净亏 ≥ 4000 元
STOP_FILL_MISS_RATIO = 0.70  # 5. 挂了买不到（未成交占比）即停

STOP_MARKER = "STOP"


def tickets_dir(root: Path) -> Path:
    return Path(root) / "data" / "daban" / "tickets"


def _day_file(root: Path, trade_date: str, suffix: str) -> Path:
    return tickets_dir(root) / f"ticket_{trade_date}{suffix}"


# ============== 横幅：每张委托单顶部，写死在这里 ==============
BANNER_HEAD = "⚠️ 这张榜按诚实成交口径没有正期望，本文件是排序研究到人工委托的过渡试验，不是买入指令。"

BANNER = (
    f"{BANNER_HEAD}\n"
    "\n"
    "   · docs/DABAN_TOPK_PNL_20261022.md：K=5 成交 1509 笔，均净收益 −0.78%/笔，累计 −94.5%；\n"
    "     未计成本 −0.58%，负号来自 T+1 开盘入场时点，不是手续费。\n"
    "   · docs/DABAN_PAPER_TRADE_RULES_20261022.md：Book B −0.903%/笔（1,017 笔 / t=−1.82）。\n"
    "   · docs/DABAN_EXEC_RESEARCH_20261022.md：成交到的那批封板率只有 39.7%（未成交 95.4%）\n"
    "     —— 你能买到的，恰恰是更容易输的那批。\n"
    f"   试验仓额度：总 {TOTAL_CAP:.0f} 元 / 单票 {SINGLE_CAP:.0f} 元（本金 {CASH:.0f} 元的 10% / 5%），"
    f"单笔最小 {MIN_ORDER_AMOUNT:.0f} 元，整手 {LOT} 股。\n"
)


def stop_lines_text() -> str:
    """把五条停止线写成一段文本（委托单横幅和 STOP marker 共用，避免两处各写一遍）。"""
    return (
        "   自动停止线：\n"
        f"     1) 已平仓样本满 {STOP_N_FINAL} 笔即停（这是试验仓的期限，不是统计判据）；\n"
        f"     2) 前 {STOP_N_MIN} 笔不判均值——早期噪声既不构成停的依据，也不构成继续的依据；\n"
        f"     3) 已平仓净均值 < {STOP_MEAN_PCT:.2f}%/笔 即停（纸盘规则 §8.2 那条停止线是 +0.20%，"
        "本试验仓有意放宽到负侧，因为它的目的是重测当前市场的期望，勉强持平不值得继续占额度；"
        "放宽这件事本身在 --report 里对照打印）；\n"
        f"     4) 累计已实现净亏 ≥ {STOP_LOSS_YUAN:.0f} 元（总仓的 20%）即停——不等统计量；\n"
        f"     5) 回填满 {STOP_N_MIN} 笔后未成交占比 ≥ {STOP_FILL_MISS_RATIO:.0%} 即停（买不到就没有样本）。\n"
        "   人工执行：本程序不下单，文本抄进券商 App 由你按下确认。"
    )


# ============== 当日账本（额度 + 幂等） ==============
def _empty_day(trade_date: str) -> dict:
    return {"trade_date": trade_date, "tickets": [], "updated_at": ""}


def load_day(root: Path, trade_date: str) -> dict:
    """读当日委托单账本；文件不存在就是空账本（首笔）。"""
    fp = _day_file(root, trade_date, ".json")
    if not fp.exists():
        return _empty_day(trade_date)
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 半写状态（理论上不会：写盘走 os.replace）。宁可当空账本重出，也不要把坏文件当已用额度。
        return _empty_day(trade_date)
    payload.setdefault("trade_date", trade_date)
    payload.setdefault("tickets", [])
    return payload


def committed_amount(day: dict) -> float:
    return float(sum(float(t.get("amount", 0.0)) for t in day.get("tickets", [])))


def remaining_budget(day: dict, total_cap: float = TOTAL_CAP) -> float:
    return max(0.0, total_cap - committed_amount(day))


def build_kwargs(remaining: float) -> dict:
    """把「当日剩余额度」翻译成 `build_intents` 需要的比例参数。

    `build_intents` 的 total_cap 只在**本次调用内**累计，所以总仓上限必须取
    `min(TOTAL_CAP, remaining)`；写成 TOTAL_CAP 会让一轮花掉比剩余更多的钱。
    """
    r = float(remaining)
    if r <= 0:
        raise ValueError("剩余额度为非正数，不该调用 build_kwargs")
    return {
        "cash": r,
        "max_single_pct": min(SINGLE_CAP, r) / r,
        "max_total_pct": min(TOTAL_CAP, r) / r,
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "lot": LOT,
        "min_amount": MIN_ORDER_AMOUNT,
    }


def intents_for_poll(candidates, day: dict) -> list[OrderIntent]:
    """本轮该出的订单意图：只看当日剩余额度，额度不足最小单笔就整轮零票。"""
    remaining = remaining_budget(day)
    if remaining < MIN_ORDER_AMOUNT:
        return []
    return build_intents(candidates, **build_kwargs(remaining))


def _ticket_dict(intent, limit_src: str) -> dict:
    row = dict(intent.to_dict())
    row["limit_src"] = limit_src
    return row


def add_tickets(day: dict, intents, limit_src_map: dict[str, str] | None = None) -> dict:
    """按 (交易日, code) 首次触发生效去重，返回新账本（不改动入参）。"""
    out = copy.deepcopy(day)
    seen = {str(t.get("code")) for t in out["tickets"]}
    for it in intents:
        code = str(getattr(it, "code", "") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        out["tickets"].append(_ticket_dict(it, (limit_src_map or {}).get(code, "-")))
    return out


@dataclass
class UpsertResult:
    new: list
    dropped: list
    day: dict


def _atomic_write(fp: Path, text: str, keep_backup: bool) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup and fp.exists():
        # data/ 在 .gitignore 里：就地覆写前留带日期备份，git 救不回来。
        bak = fp.with_name(f"{fp.name}.bak-{time.strftime('%Y%m%d_%H%M%S')}")
        bak.write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = fp.with_name(f"{fp.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, fp)


def _render_md(day: dict, banner: str) -> str:
    rows = [
        "| 代码 | 名称 | 板块代码 | 委托价 | 股数 | 金额 | 止损 | 止盈 | 涨停价来源 | 评分 | 首次触发 | 理由 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in day["tickets"]:
        rows.append(
            f"| {t['code']} | {t['name']} | {t.get('sector', '')} | {float(t['price']):.2f} "
            f"| {int(t['shares'])} | {float(t['amount']):.0f} | {float(t['stop_loss']):.2f} "
            f"| {float(t['take_profit']):.2f} | {t.get('limit_src', '-')} | {float(t.get('score', 0.0)):.3f} "
            f"| {t.get('created_at', '')} | {t.get('reason', '')} |"
        )
    used = committed_amount(day)
    lines = [
        f"# daban 试验仓委托单 · {day['trade_date']}",
        "",
        banner,
        stop_lines_text(),
        "",
        f"当日已用额度：{used:.0f} 元 / 上限 {TOTAL_CAP:.0f} 元；剩余 {remaining_budget(day):.0f} 元。",
        f"在列 {len(day['tickets'])} 只（同一交易日同一代码只保留首次触发）。",
        "",
        "\n".join(rows),
        "",
        "## 条件单文本（可直接粘进券商条件单/预警）",
        "",
    ]
    for t in day["tickets"]:
        lines.append(f"- `{condition_order_text(_intent_from_row(t))}`")
    return "\n".join(lines) + "\n"


def _intent_from_row(row: dict) -> OrderIntent:
    known = {f.name for f in fields(OrderIntent)}
    return OrderIntent(**{k: v for k, v in row.items() if k in known})


def upsert_tickets(root: Path, trade_date: str, intents, day: dict | None = None,
                   banner: str = BANNER, limit_src_map: dict[str, str] | None = None) -> UpsertResult:
    """把本轮意图并进当日账本并落盘（md + json），同 code 重复的丢弃。"""
    base = load_day(root, trade_date) if day is None else day
    seen = {str(t.get("code")) for t in base["tickets"]}
    new, dropped = [], []
    for it in intents:
        code = str(getattr(it, "code", "") or "")
        if not code or code in seen:
            dropped.append(it)
            continue
        seen.add(code)
        new.append(it)

    merged = add_tickets(base, new, limit_src_map=limit_src_map)
    merged["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    json_path = _day_file(root, trade_date, ".json")
    md_path = _day_file(root, trade_date, ".md")
    first_write = not json_path.exists()
    _atomic_write(json_path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n", keep_backup=not first_write)
    _atomic_write(md_path, _render_md(merged, banner), keep_backup=not first_write)
    return UpsertResult(new=new, dropped=dropped, day=merged)


# ============== STOP marker：停止的唯一权威 ==============
def marker_path(root: Path) -> Path:
    return tickets_dir(root) / STOP_MARKER


def marker_exists(root: Path) -> bool:
    return marker_path(root).exists()


def read_marker(root: Path) -> str:
    fp = marker_path(root)
    return fp.read_text(encoding="utf-8") if fp.exists() else ""


def write_marker(root: Path, text: str) -> Path:
    fp = marker_path(root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    body = text.rstrip() + f"\n\n写于 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    fp.write_text(body, encoding="utf-8")
    return fp


# ============== 停止线 ==============
@dataclass
class StopVerdict:
    triggered: bool
    rule: str
    detail: str
    n_closed: int
    n_backfilled: int
    mean_pct: float | None
    miss_ratio: float | None
    realized_yuan: float

    @property
    def marker_text(self) -> str:
        return (
            "🛑 daban 试验仓自动停止线已触发。\n"
            f"   命中：{self.rule}\n"
            f"   判据：{self.detail}\n"
            f"   已平仓 {self.n_closed} 笔 / 已回填 {self.n_backfilled} 笔；"
            f"净均值 {f'{self.mean_pct:.3f}%/笔' if self.mean_pct is not None else '样本不足'}；"
            f"未成交占比 {f'{self.miss_ratio:.1%}' if self.miss_ratio is not None else '样本不足'}；"
            f"累计已实现 {self.realized_yuan:.0f} 元。\n"
            "   恢复出票：删掉本文件（data/daban/tickets/STOP），并确认你是否真的要继续这条负期望的线。\n"
            "   运维收口另有一步：systemctl --user disable --now daban-ticket.timer。"
        )


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def fills_path(root: Path, trade_date: str) -> Path:
    return tickets_dir(root) / f"fills_{trade_date}.csv"


def load_fills(root: Path) -> list[dict]:
    """读全部 `fills_*.csv`（回填台账），供停止线判定。缺文件就是空台账。

    `net_pct` / `net_yuan` 允许为空：费用没回填的笔不能冒充样本，`evaluate_stop` 会把它们
    从均值里剔除（但仍然计入成交率）。
    """
    rows: list[dict] = []
    for fp in sorted(tickets_dir(root).glob("fills_*.csv")):
        with fp.open(encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                for key in ("net_pct", "net_yuan"):
                    raw = (rec.get(key) or "").strip()
                    rec[key] = float(raw) if raw else None
                for key in ("filled", "closed"):
                    rec[key] = int(float(rec.get(key) or 0))
                rows.append(rec)
    return rows


def evaluate_stop(fills) -> StopVerdict:
    """只看已平仓 pair 的净收益判停；未成交笔只进成交率，不进均值。

    `fills` 是 `scripts/daban_fill.py` 的行（dict 或同名字段对象）：
    `filled`（0/1）、`closed`（0/1）、`net_pct`（%/笔，扣真实回单费用）、`net_yuan`（元）。
    缺 `net_pct` 的行不参与统计——费用字段没回填的笔不能冒充样本。
    """
    rows = [f if isinstance(f, dict) else f.__dict__ for f in fills]
    backfilled = [r for r in rows if r.get("filled") in (1, "1", True)]
    closed = [r for r in rows if int(r.get("closed") or 0) == 1 and r.get("net_pct") is not None]

    n_closed = len(closed)
    n_back = len(backfilled)
    pcts = [float(r["net_pct"]) for r in closed]
    yuan = [float(r.get("net_yuan") or 0.0) for r in closed]
    mean_pct = _mean(pcts)
    realized = sum(yuan)
    miss_ratio = (1.0 - n_back / len(rows)) if rows else None
    common = dict(n_closed=n_closed, n_backfilled=n_back, mean_pct=mean_pct,
                  miss_ratio=miss_ratio, realized_yuan=realized)

    if n_closed >= STOP_N_FINAL:
        return StopVerdict(True, f"第 1 条：已平仓样本满 {STOP_N_FINAL} 笔",
                           f"n={n_closed}，试验仓到期出报告", **common)
    if realized <= -STOP_LOSS_YUAN:
        return StopVerdict(True, f"第 4 条：累计已实现净亏 ≥ {STOP_LOSS_YUAN:.0f} 元",
                           f"已实现 {realized:.0f} 元", **common)
    if n_closed >= STOP_N_MIN and mean_pct is not None and mean_pct < STOP_MEAN_PCT:
        return StopVerdict(True, f"第 3 条：已平仓净均值 < {STOP_MEAN_PCT:.2f}%/笔",
                           f"n={n_closed}，均值 {mean_pct:.3f}%/笔", **common)
    if len(rows) >= STOP_N_MIN and miss_ratio is not None and miss_ratio >= STOP_FILL_MISS_RATIO:
        return StopVerdict(True, f"第 5 条：未成交占比 ≥ {STOP_FILL_MISS_RATIO:.0%}",
                           f"{len(rows)} 笔里只回填到 {n_back} 笔成交，miss={miss_ratio:.1%}", **common)
    return StopVerdict(False, "", f"已平仓 {n_closed} 笔（判停门槛 {STOP_N_MIN} 笔）", **common)
