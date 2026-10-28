#!/usr/bin/env python
"""打板回测：T 日封死涨停 → T+1 开盘买入 → 止盈/止损/超时卖出。

口径说明（与计划书附录 D.3 方案 E 区分）：
- 方案 E 是"事后确认版"（T+1 收盘后看涨幅+量比），本脚本是**打板口径**：
  信号日 T = 收盘封死涨停，买点 = T+1 开盘价（open），卖出按日内高低触发止盈/止损
- 成本：双边 0.30%（0.05% 印花税 + 0.025% 佣金 + 0.1% 滑点），与计划书 §D.3 一致
- 一字板剔除：T+1 开盘已达涨停价 → 实盘买不进，剔除
- 同日双触（最高触及止盈且最低触及止损）→ 保守按**止损**计

用法::

    venv/bin/python scripts/daban_backtest.py --start 20200101 --end 20261019
    venv/bin/python scripts/daban_backtest.py --tp 0.30 --sl 0.10 --hold 5
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.filter import SectorFilter, load_sector_features  # noqa: E402
from tdxfeed.daban.limit_price import (  # noqa: E402
    is_sealed,
    limit_ratio,
    limit_up_price,
)
from tdxfeed.daban.scanner import daily_path, load_universe  # noqa: E402
from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.date_utils import parse_date_series  # noqa: E402
from tdxfeed.execution import single as _single  # noqa: E402
from tdxfeed.execution.costs import CostModel  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import adj_factor_dir, daily_dir  # noqa: E402

DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
OUT_DIR = _ROOT / "data" / "daban"
COLS = ["date", "open", "high", "low", "close", "volume"]


@dataclass(slots=True)
class Trade:
    code: str
    name: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    ret_gross: float
    ret_net: float
    reason: str
    consecutive: int


# 成交成本由调用方以 `cost`（单边合计）另扣，内核按零成本跑，避免扣两遍。
_ZERO_COSTS = CostModel(commission=0.0, slippage=0.0)


def load_adj(code: str) -> pd.Series | None:
    """读复权因子（data/adj_factor/{code}.parquet），返回 {YYYYMMDD: adj_factor}。"""
    p = ADJ_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "adj_factor"])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
    return df.set_index("date")["adj_factor"]


def load_daily(code: str) -> pd.DataFrame | None:
    p = daily_path(code, DAILY_DIR)
    if p is None:
        return None
    try:
        df = pd.read_parquet(p, columns=COLS)
    except Exception:
        try:
            df = pd.read_parquet(p)
        except Exception:
            return None
    if df is None or df.empty:
        return None
    # date 列存在 int64(YYYYMMDD) / date32 两种 schema 漂移，统一走 parse_date_series
    df["date"] = parse_date_series(df["date"]).dt.strftime("%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)
    # 合并复权因子：消除除权除息日的跳空（否则 T+1 开盘价断崖会被误判为巨亏）
    adj = load_adj(code)
    if adj is not None:
        df = df.merge(adj, left_on="date", right_index=True, how="left")
        df["adj_factor"] = df["adj_factor"].ffill().fillna(1.0)
    else:
        df["adj_factor"] = 1.0
    return df


def limit_flags(df: pd.DataFrame, code: str, name: str) -> list[bool]:
    """逐日"是否收盘封死涨停"。"""
    ratio = limit_ratio(code, name)
    close = df["close"].to_numpy()
    prev = df["close"].shift(1).to_numpy()
    flags = []
    for i in range(len(df)):
        if i == 0 or prev[i] is None or prev[i] != prev[i] or prev[i] <= 0:
            flags.append(False)
            continue
        lp = limit_up_price(float(prev[i]), ratio)
        flags.append(is_sealed(float(close[i]), lp))
    return flags


def backtest_one(
    code: str,
    name: str,
    df: pd.DataFrame,
    tp: float,
    sl: float,
    hold: int,
    cost: float,
    mode: str = "limit",
    allow_open_limit: bool = False,
) -> list[Trade]:
    """打板回测单股。

    mode="limit"（默认，实盘打板口径）：
        T 日收盘封死涨停 → 以 T 日涨停价买入 → T+1 开盘卖出（隔夜溢价）
    mode="chase"（接力/追高口径）：
        T 日收盘封死涨停 → T+1 开盘买入 → 持有 ≤ hold 日，按日内高低触发止盈/止损
    """
    if mode == "limit":
        return _backtest_limit(code, name, df, cost, allow_open_limit)
    return _backtest_chase(code, name, df, tp, sl, hold, cost)


def _plausible_range(code: str, name: str) -> float:
    """该股的单日涨跌幅限制（正数），用于剔除异常跳空样本。"""
    return float(limit_ratio(code, name))


def _factors_of(df: pd.DataFrame) -> np.ndarray:
    if "adj_factor" not in df.columns:
        return np.ones(len(df))
    f = df["adj_factor"].to_numpy(dtype=float)
    f[~np.isfinite(f) | (f <= 0)] = 1.0
    return f


def _backtest_limit(
    code: str,
    name: str,
    df: pd.DataFrame,
    cost: float,
    allow_open_limit: bool,
) -> list[Trade]:
    """T 日涨停价买入 → T+1 开盘卖出（打板隔夜溢价，实盘口径）。"""
    flags = limit_flags(df, code, name)
    dates = df["date"].tolist()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    factors = _factors_of(df)
    ratio = limit_ratio(code, name)
    ratio_f = _plausible_range(code, name)
    trades: list[Trade] = []
    skipped = 0
    consecutive = 0

    for i in range(len(df) - 1):
        if not flags[i]:
            consecutive = 0
            continue
        consecutive += 1
        # 买入价 = T 日的涨停价 = 由 T-1 收盘价推算（closes[i] 本身已是涨停价，不能再乘一次）
        entry_raw = limit_up_price(float(closes[i - 1]), ratio)
        if entry_raw <= 0:
            continue
        # T 日开盘即涨停（一字/准一字）→ 排板买不进，剔除
        if not allow_open_limit and is_sealed(float(opens[i]), entry_raw):
            continue
        k = i + 1
        while k < len(df) and float(opens[k]) <= 0:  # T+1 停牌则顺延
            k += 1
        if k >= len(df):
            break
        exit_raw = float(opens[k])
        if exit_raw <= 0:
            continue
        entry = entry_raw * float(factors[i])
        exit_price = exit_raw * float(factors[k])
        if entry <= 0:
            continue
        chg = exit_price / entry - 1.0
        # 合理性闸门：T+1 的涨跌幅基准 = T 日收盘价（即涨停价 = 买入价），
        # 故 T+1 开盘相对买入价落在 [-limit, +limit] 内属正常（高开打板溢价）；
        # 超出即除权未被复权因子补偿 / 数据异常（如 -27% 的非理性亏损），剔除
        if chg < -ratio_f - 0.01 or chg > ratio_f + 0.01:
            skipped += 1
            continue
        gross = chg
        trades.append(
            Trade(
                code=code,
                name=name,
                signal_date=dates[i],
                entry_date=dates[i],
                exit_date=dates[k],
                entry=entry_raw,
                exit=exit_raw,
                ret_gross=round(gross, 6),
                ret_net=round(gross - cost, 6),
                reason="T+1开盘卖",
                consecutive=consecutive,
            )
        )
    return trades, skipped


def _backtest_chase(
    code: str,
    name: str,
    df: pd.DataFrame,
    tp: float,
    sl: float,
    hold: int,
    cost: float,
) -> list[Trade]:
    """T+1 开盘买入 → 持有止盈/止损（接力口径）。"""
    flags = limit_flags(df, code, name)
    dates = df["date"].tolist()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    vols = df["volume"].to_numpy()
    factors = _factors_of(df)
    ratio_f = _plausible_range(code, name)
    trades: list[Trade] = []
    skipped = 0
    consecutive = 0

    for i in range(len(df) - 1):
        if not flags[i]:
            consecutive = 0
            continue
        consecutive += 1
        j = i + 1  # T+1
        if j >= len(df):
            break
        entry_raw = float(opens[j])
        if entry_raw <= 0:
            continue
        # T+1 一字/开盘即涨停 → 买不进，剔除（raw 口径）
        ratio = limit_ratio(code, name)
        if is_sealed(entry_raw, limit_up_price(float(closes[i]), ratio)):
            continue
        f_j = float(factors[j])
        entry = entry_raw * f_j  # 复权口径买入价
        if entry <= 0:
            continue

        n_win = min(hold, len(df) - j)
        win = pd.DataFrame({
            "date": dates[j:j + n_win],
            "open": opens[j:j + n_win] * factors[j:j + n_win],
            "high": highs[j:j + n_win] * factors[j:j + n_win],
            "low": lows[j:j + n_win] * factors[j:j + n_win],
            "close": closes[j:j + n_win] * factors[j:j + n_win],
            "volume": vols[j:j + n_win],
        })
        plan = _single.SingleTradePlan(tp=tp, sl=-sl, hold_days=hold, sl_first=True)

        # 入场当日也要判（旧实现从 k=j 起）：内核的时间窗固定从 entry+1 起（plan_e 口径），
        # 所以这一根在调用侧用同一阶梯判。开盘价即入场价，跳空条款在这一根不会生效。
        # 触发比较是价位（low <= entry*(1-sl)）而非收益比例，恰好相等的 K 线旧代码会因
        # 浮点噪声不触发：2026-09 切片的 957 笔里有 1 笔因此把出场日提前 4 天（同价同因）。
        hit = _single.check_exit(entry, open_=entry,
                                 high=float(win["high"].iloc[0]),
                                 low=float(win["low"].iloc[0]),
                                 close=float(win["close"].iloc[0]), plan=plan)
        if hit is not None:
            k_rel = 0
            reason = "止损" if hit.reason == "SL" else "止盈"
            exit_price = hit.price
        elif n_win == 1:
            # 数据在入场日就断了：内核窗口没有可判根会返回 None，这里按当日收盘超时
            k_rel, reason, exit_price = 0, "超时", float(win["close"].iloc[0])
        else:
            r = _single.settle_single_trade(win, 0, plan, _ZERO_COSTS,
                                            insufficient="last_close")
            if r is None:
                continue
            k_rel = r.exit_idx
            reason = {"SL": "止损", "TP": "止盈"}.get(r.reason, "超时")
            exit_price = r.exit_price
        exit_date = dates[j + k_rel]
        if exit_price <= 0:
            continue
        gross = exit_price / entry - 1.0
        # 合理性闸门：买入价 = T+1 开盘（可能高于前收），此后连跌两根可超过一个 limit；
        # 下界跟随跳空放开，否则真跳空亏损笔会被当「除权未补偿」删掉。
        if gross < -2 * ratio_f - 0.01 or gross > 2 * ratio_f + 0.01:
            skipped += 1
            continue
        trades.append(
            Trade(
                code=code,
                name=name,
                signal_date=dates[i],
                entry_date=dates[j],
                exit_date=exit_date,
                entry=entry_raw,
                exit=exit_price / float(factors[j]) if factors[j] else exit_price,
                ret_gross=round(gross, 6),
                ret_net=round(gross - cost, 6),
                reason=reason,
                consecutive=consecutive,
            )
        )
    return trades, skipped


def summarize(trades: list[Trade], daily_cap: int = 0) -> dict:
    """统计。

    daily_cap > 0 时按"每日最多打 daily_cap 只"做资金容量约束：
    同日优先取连板高度更高的（打板只做最强），用于给出更接近实盘的资金曲线。
    daily_cap = 0 时为"全市场等权"上限口径，仅作参照，不可当实盘预期。
    """
    if not trades:
        return {"样本数": 0}
    df = pd.DataFrame([asdict(t) for t in trades])
    net = df["ret_net"]
    wins = df[df["ret_net"] > 0]
    losses = df[df["ret_net"] <= 0]
    # 权益曲线按**日频等权**构建：实盘是每天一批建仓、次日卖，逐笔复利在 9 万笔
    # 样本上会数值下溢（回撤显示成 -100%），日频更贴近真实资金曲线
    if daily_cap and daily_cap > 0:
        picked = df.sort_values(["entry_date", "consecutive"], ascending=[True, False])
        daily = (
            picked.groupby("entry_date").head(daily_cap).groupby("entry_date")["ret_net"].mean().sort_index()
        )
    else:
        daily = df.groupby("entry_date")["ret_net"].mean().sort_index()
    dlog = np.log1p(daily).cumsum()
    dd = dlog - dlog.cummax()
    drawdown = float(np.expm1(float(dd.min())))
    total_log = float(dlog.iloc[-1]) if len(dlog) else 0.0
    years = max(len(daily) / 243.0, 1e-9)
    ann = float(np.exp(total_log / years) - 1)
    avg_win = float(wins["ret_net"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["ret_net"].mean()) if len(losses) else 0.0
    return {
        "样本数": len(df),
        "交易日数": len(daily),
        "胜率%": round(float((net > 0).mean()) * 100, 1),
        "平均收益%": round(float(net.mean()) * 100, 2),
        "平均盈利%": round(avg_win * 100, 2),
        "平均亏损%": round(avg_loss * 100, 2),
        "盈亏比": round(avg_win / abs(avg_loss), 2) if avg_loss else 0.0,
        "日均收益%": round(float(daily.mean()) * 100, 3),
        "年化收益%": round(ann * 100, 1),
        "最大回撤%": round(drawdown * 100, 2),
        "Calmar": round(ann / abs(drawdown), 2) if drawdown else 0.0,
        "止盈": int((df["reason"] == "止盈").sum()),
        "止损": int((df["reason"] == "止损").sum()),
        "超时": int((df["reason"] == "超时").sum()),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="打板策略回测")
    p.add_argument("--start", default="20200101", help="起始日 YYYYMMDD")
    p.add_argument("--end", default="99999999", help="结束日 YYYYMMDD")
    p.add_argument("--tp", type=float, default=0.30, help="止盈比例（默认 30%%，对齐方案 E）")
    p.add_argument("--sl", type=float, default=0.10, help="止损比例（默认 10%%，对齐方案 E）")
    p.add_argument("--hold", type=int, default=5, help="最大持有交易日（chase 模式）")
    p.add_argument(
        "--mode",
        choices=["limit", "chase"],
        default="limit",
        help="limit=T日涨停价买入/T+1开盘卖（实盘打板口径，默认）；chase=T+1开盘买入/止盈止损",
    )
    p.add_argument(
        "--allow-open-limit",
        action="store_true",
        help="保留T日开盘即涨停的样本（默认剔除：排板买不进）",
    )
    p.add_argument("--cost", type=float, default=0.0030, help="双边成本（默认 0.30%%）")
    p.add_argument(
        "--daily-cap",
        type=int,
        default=20,
        help="每日最多打板只数（资金容量约束，默认 20；0 = 不限，仅作上限参照）",
    )
    p.add_argument(
        "--min-sector-sec",
        type=float,
        default=0.0,
        help="板块 Sec 门槛（阶段0 口径；0=不过滤）",
    )
    p.add_argument(
        "--min-sector-limitup",
        type=int,
        default=0,
        help="板块当日涨停家数下限（阶段0 口径；0=不过滤）",
    )
    p.add_argument(
        "--filter-config",
        default="",
        help="过滤配置文件（data/daban/filter_config.json）；给了就以文件为准",
    )
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    universe = load_universe()
    codes = list(universe.keys())
    mode_desc = (
        "T日涨停价买入→T+1开盘卖（实盘打板口径）"
        if args.mode == "limit"
        else f"T+1开盘买入→止盈{args.tp:.0%}/止损{args.sl:.0%}，持有≤{args.hold}日"
    )
    print(f"[info] 股票池 {len(codes)} 只 · 模式={mode_desc} · 成本={args.cost:.2%}")

    def work(code: str):
        df = load_daily(code)
        if df is None:
            return []
        df = df[(df["date"] >= args.start) & (df["date"] <= args.end)]
        if len(df) < 3:
            return []
        return backtest_one(
            code,
            universe.get(code, ""),
            df,
            args.tp,
            args.sl,
            args.hold,
            args.cost,
            mode=args.mode,
            allow_open_limit=args.allow_open_limit,
        )

    trades: list[Trade] = []
    skipped = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for part, sk in pool.map(work, codes):
            trades.extend(part)
            skipped += sk
    if skipped:
        print(f"[info] 剔除异常样本（除权未补偿/数据越界）: {skipped} 笔")

    if not trades:
        print("[warn] 无交易样本")
        return 1

    # 阶段 0 口径：板块过滤（统一事实来源，后续实验都在过滤后子集上做）
    flt = (
        SectorFilter.load(args.filter_config)
        if args.filter_config
        else SectorFilter(min_sec=args.min_sector_sec, min_limitup=args.min_sector_limitup)
    )
    if flt.enabled:
        before = len(trades)
        sector = load_sector_features()
        df_trades = pd.DataFrame([asdict(t) for t in trades])
        df_trades["industry_code"] = df_trades["code"].map(load_industry_map())
        kept = flt.apply(df_trades, sector)
        trade_fields = {f.name for f in fields(Trade)}
        trades = [
            Trade(**{k: v for k, v in row.items() if k in trade_fields})
            for row in kept.to_dict("records")
        ]
        print(
            f"[filter] Sec>={flt.min_sec} & 共振>={flt.min_limitup}: "
            f"{before} → {len(trades)} 笔（保留 {len(trades) / max(before, 1) * 100:.1f}%）"
        )

    trades.sort(key=lambda t: t.entry_date)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    detail = out_dir / f"daban_backtest_{args.start}_{args.end}.csv"
    pd.DataFrame([asdict(t) for t in trades]).to_csv(detail, index=False, encoding="utf-8-sig")

    print("\n=== 总体（逐笔） ===")
    for k, v in summarize(trades).items():
        print(f"{k:<10}{v}")

    print("\n=== 组合口径（日频资金曲线） ===")
    print(f"{'口径':<22}{'日均%':>9}{'年化%':>11}{'最大回撤%':>11}{'Calmar':>9}")
    for label, cap in (
        (f"每日≤{args.daily_cap}只(容量约束)", args.daily_cap),
        ("全市场等权(上限参照)", 0),
    ):
        s = summarize(trades, daily_cap=cap)
        print(
            f"{label:<22}{s.get('日均收益%', 0):>9}{s.get('年化收益%', 0):>11}"
            f"{s.get('最大回撤%', 0):>11}{s.get('Calmar', 0):>9}"
        )

    print("\n=== 分年 ===")
    by_year: dict[str, list[Trade]] = {}
    for t in trades:
        by_year.setdefault(t.entry_date[:4], []).append(t)
    print(f"{'年份':<8}{'样本':>6}{'胜率%':>8}{'均收益%':>10}{'均盈%':>9}{'均亏%':>9}{'盈亏比':>8}")
    for year in sorted(by_year):
        s = summarize(by_year[year])
        print(
            f"{year:<8}{s.get('样本数', 0):>6}{s.get('胜率%', 0):>8}"
            f"{s.get('平均收益%', 0):>10}{s.get('平均盈利%', 0):>9}"
            f"{s.get('平均亏损%', 0):>9}{s.get('盈亏比', 0):>8}"
        )

    print("\n=== 出场原因 ===")
    for reason in sorted({t.reason for t in trades}):
        sub = [t for t in trades if t.reason == reason]
        s = summarize(sub)
        print(
            f"{reason:<8} 样本{s.get('样本数', 0):>5}  "
            f"均收益{s.get('平均收益%', 0):>8}%  胜率{s.get('胜率%', 0):>6}%"
        )

    print(f"\n[ok] 明细: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
