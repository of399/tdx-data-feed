#!/usr/bin/env python3
"""方案 E 实盘监控 · 真实盈亏(PnL)跟踪与偏差分析 (v1)
===================================================

【目标】
  对方案 E 实盘（plan_e_journal.py 手动 buy/sell → journal.csv）进行**持续监控**：
    1. 实盘交易的实际盈亏（每日 / 每周 / 累计 PnL，含已实现与未实现 MTMark）
    2. 与回测 / 模拟基线的偏差对比
    3. 关键风险指标在真实市场的表现（最大回撤 / 夏普 / 索提诺 / 胜率 / 盈亏比）
    4. 滑点、手续费、流动性（北交所）对实际收益的侵蚀
    5. 异常记录与触发条件（连续亏损、回撤超限、胜率骤降…）
    6. 观察周期与评估节点（10/20/30 笔、2/4 周、3 个月）
    7. 显著偏离预期时的**调整 / 终止条件**（GO / WATCH / HALT 三级状态）

【方法学与可追溯性】
  - 真实收益口径：journal 记录**实际成交价**（买价 / 卖价），本脚本据其重算毛收益；
    成本（佣金 + 滑点，北交所叠加额外滑点）作为**显式、可配置、可复现**的成本模型
    叠加，输出毛 PnL 与净 PnL 双口径，二者之差即为成本侵蚀（可追溯）。
  - 风险指标公式与 plan_e_backtest.py::compute_metrics **完全一致**（日收益 mu/sd×√252、
    权益曲线 cumprod、回撤取峰谷、索提诺/卡玛同口径），保证实盘与回测可比。
  - 每日权益曲线 = 现金 + Σ持仓市值（cash+holdings 模型），买入日扣买入成本、卖出日扣
    卖出成本，持仓逐日 MTMark；与回测"交易日对齐资金曲线"一致。
  - 每次运行写入：
      data/plan_e/live/observation_reports/plan_e_live_YYYY-MM-DD.md   （日/周观察报告）
      data/plan_e/live/monitor_history.csv                              （逐次运行历史，可复现时序）
      data/plan_e/live/monitor_snapshot_<ts>.json                       （完整指标 + 输入指纹，可复现）
  - 输入指纹：journal.csv 的 sha256 + mtime、脚本版本、成本假设、初始资金、基线来源，
    全部落盘到快照，确保任一历史报告可溯源、可复现。

【状态机】
  GO    ：在正常阈值内，可正常建仓。
  WATCH ：触发预警阈值（回撤/胜率/累计收益偏离），缩减新仓或加强复核。
  HALT  ：触发终止阈值（深回撤 / 累计大幅亏损 / 连亏超限），暂停新开仓并复盘。

【运行】
  venv/bin/python scripts/monitor_plan_e_live.py                      # 自动(周五=周报，其余=日报)
  venv/bin/python scripts/monitor_plan_e_live.py --emit weekly         # 强制周报
  venv/bin/python scripts/monitor_plan_e_live.py --emit snapshot       # 仅写历史+快照，不出报告(供 canary)
  venv/bin/python scripts/monitor_plan_e_live.py --baseline my_base.json
  venv/bin/python scripts/monitor_plan_e_live.py --no-freshness-check  # 跳过数据新鲜度守卫
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 显式把项目根加入 sys.path，确保 `import tdxfeed`（如 freshness）在直接运行时可用。
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from _common import daily_dir, parse_date

ROOT = Path(__file__).parent.parent
LIVE_DIR = ROOT / "data" / "plan_e" / "live"
JOURNAL = LIVE_DIR / "journal.csv"
POSITIONS = LIVE_DIR / "positions.json"

PARQUET = daily_dir()
OBS_DIR = LIVE_DIR / "observation_reports"
HISTORY = LIVE_DIR / "monitor_history.csv"

__version__ = "v1"
SIGNAL_MODEL = "vpnl_huber"  # 当前部署的信号模型（predict.py 默认）

# ---------------- 成本模型（假设，显式可配置、可复现）----------------
FEE_RATE = 0.0003   # 单边佣金 0.03%
SLIP_RATE = 0.001   # 单边滑点 0.10%
BJ_SLIP = 0.003     # 北交所额外单边滑点 0.30%（流动性补偿）

# ---------------- 默认基线（方案 E v2 回测·含成本，n=136）----------------
# 来源：plan_e_backtest.py v2（含成本）汇总；与实盘对比的"预期"锚。
DEFAULT_BASELINE: dict[str, float] = {
    "n": 136.0,
    "win_rate": 0.353,
    "avg_ret": 0.0316,
    "sharpe": 0.58,
    "max_dd": -0.182,
    "ann_ret": 0.0443,
    "pf": 3.00,
    "note": "方案E v2 回测(含成本, n=136)",
}

# ---------------- 状态阈值（WATCH / HALT）----------------
MAX_DD_HALT, MAX_DD_WATCH = -0.20, -0.12
CUM_HALT, CUM_WATCH = -0.15, -0.08
STREAK_HALT, STREAK_WATCH = 6, 4
RECENT_N = 10
RECENT_WR_HALT, RECENT_WR_WATCH = 0.20, 0.35
SINGLE_LOSS_HALT = -0.20
STALE_OPEN_DAYS = 10  # 持仓超过此天数未出场 → 异常（应已时间止损）

# 观察节点（评估节点）
CHECKPOINTS = [
    (10, "早期信号：样本过少，仅观察，不据此下统计结论"),
    (20, "中期观察：对比基线偏离方向"),
    (30, "统计显著性闸口（n≥30）：可首次作统计推断"),
    (60, "季度复盘：决定是否调参/调仓"),
]


# ============== 工具 ==============

def _exchange_of(code: str) -> str:
    """代码 → 交易所（规范化前缀后判定）。"""
    c = str(code).lower().lstrip("bj").lstrip("sh").lstrip("sz")
    c = c.zfill(6)[-6:]
    if c.startswith("920") or c[0] == "8":
        return "北交所"
    if c.startswith("60") or c.startswith("68"):
        return "上海"
    if c.startswith("00") or c.startswith("30"):
        return "深圳"
    return "其他"


def _cost_fraction(exch: str) -> float:
    """单笔**往返**成本占比（佣金 + 滑点，北交所叠加额外滑点）。"""
    base = (FEE_RATE + SLIP_RATE) * 2
    if exch == "北交所":
        base += BJ_SLIP * 2
    return base


def _load_price_series(code: str) -> pd.Series | None:
    """加载某代码日 K 收盘价序列（索引 date），失败返回 None。"""
    cc = str(code).zfill(6).lstrip("bj")
    for cand in [f"{cc}.parquet", f"sh{cc}.parquet", f"sz{cc}.parquet", f"bj{cc}.parquet"]:
        f = PARQUET / cand
        if f.exists():
            try:
                df = pd.read_parquet(f)
            except Exception:
                return None
            df["date"] = parse_date(df["date"])
            s = df.set_index("date")["close"]
            s = s[~s.index.duplicated(keep="last")].sort_index()
            return s
    return None


def _journal_sha(path: Path) -> tuple[str | None, float | None]:
    """journal 指纹：sha256 + mtime（供可追溯）。"""
    if not path.exists():
        return None, None
    try:
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest()[:16], round(path.stat().st_mtime, 1)
    except Exception:
        return None, None


# ============== 构建交易 ==============

def load_journal() -> pd.DataFrame:
    """读取实盘 journal（仅 source=='real' 的买卖，与模拟完全分离）。"""
    if not JOURNAL.exists():
        return pd.DataFrame()
    df = pd.read_csv(JOURNAL)
    if "source" in df.columns:
        df = df[df["source"] == "real"]
    else:
        df = df[df["reason"] == "MANUAL"]
    if "date" in df.columns:
        df["date"] = parse_date(df["date"])
    return df


def build_trades(journal: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """由 journal + positions 构建已实现(closed)与未实现(open)交易清单。

    closed 字段：code, entry_date, exit_date, hold_days, qty, entry_price, exit_price,
                  gross_pct, gross_pnl, exch, reason, cost_frac, net_pct, net_pnl
    open   字段：同上但 exit_price=最新收盘(MTMark)，reason='OPEN'，net 为估算(含往返成本)。
    """
    buys = journal[journal["action"] == "BUY"] if not journal.empty else pd.DataFrame()
    sells = journal[journal["action"] == "SELL"] if not journal.empty else pd.DataFrame()

    buy_by_key: dict[tuple[str, str], dict] = {}
    for _, b in buys.iterrows():
        key = (str(b["code"]).zfill(6), str(b.get("entry_date", "")))
        if key not in buy_by_key:
            buy_by_key[key] = b.to_dict()

    closed: list[dict] = []
    for _, s in sells.iterrows():
        code = str(s["code"]).zfill(6)
        entry_date = str(s.get("entry_date", ""))
        b = buy_by_key.get((code, entry_date))
        entry_price = float(b["price"]) if b is not None and pd.notna(b.get("price")) else float(s.get("price", 0)) if "price" in s and pd.notna(s.get("price")) else 0.0
        qty = int(s["qty"]) if pd.notna(s.get("qty")) else 0
        exit_price = float(s["price"]) if pd.notna(s.get("price")) else 0.0
        gross_pct = float(s["pnl_pct"]) if pd.notna(s.get("pnl_pct")) else 0.0
        exch = _exchange_of(code)
        cf = _cost_fraction(exch)
        gross_pnl = gross_pct * entry_price * qty
        net_pct = gross_pct - cf
        net_pnl = gross_pnl - cf * entry_price * qty
        closed.append({
            "code": code, "entry_date": entry_date,
            "exit_date": str(s["date"].date()) if hasattr(s["date"], "date") else str(s["date"]),
            "hold_days": int(s["hold_days"]) if pd.notna(s.get("hold_days")) else 0,
            "qty": qty, "entry_price": entry_price, "exit_price": exit_price,
            "gross_pct": gross_pct, "gross_pnl": gross_pnl, "exch": exch,
            "reason": str(s.get("reason", "")), "cost_frac": cf,
            "net_pct": net_pct, "net_pnl": net_pnl,
        })

    # open：优先 positions.json；并补充 journal 中未平仓买（双重保险）
    open_map: dict[str, dict] = {}
    if POSITIONS.exists():
        try:
            pos = json.loads(POSITIONS.read_text(encoding="utf-8"))
            for code, p in pos.get("positions", {}).items():
                c = str(code).zfill(6)
                open_map[c] = {
                    "code": c, "entry_date": str(p.get("entry_date", "")),
                    "qty": int(p.get("qty", 0)), "entry_price": float(p.get("entry_price", 0)),
                }
        except Exception as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
            print(f"[WARN] swallowed: {e!r}")
    for _, b in buys.iterrows():
        c = str(b["code"]).zfill(6)
        ed = str(b.get("entry_date", ""))
        if c not in open_map or open_map[c].get("entry_date") != ed:
            # 仅当该买无对应卖时才视为 open
            if (c, ed) not in buy_by_key:
                continue
            matched_sell = any(
                (str(s["code"]).zfill(6) == c and str(s.get("entry_date", "")) == ed)
                for _, s in sells.iterrows()
            )
            if not matched_sell:
                open_map[c] = {"code": c, "entry_date": ed,
                               "qty": int(b["qty"]) if pd.notna(b.get("qty")) else 0,
                               "entry_price": float(b["price"]) if pd.notna(b.get("price")) else 0.0}

    open_l: list[dict] = []
    for c, o in open_map.items():
        if o.get("qty", 0) <= 0 or o.get("entry_price", 0) <= 0:
            continue
        ps = _load_price_series(c)
        last_close = float(ps.iloc[-1]) if ps is not None and not ps.empty else o["entry_price"]
        exch = _exchange_of(c)
        cf = _cost_fraction(exch)
        gross_pct = (last_close / o["entry_price"] - 1) if o["entry_price"] > 0 else 0.0
        gross_pnl = gross_pct * o["entry_price"] * o["qty"]
        net_pct = gross_pct - cf  # 估算：若现在平仓的净收益
        net_pnl = gross_pnl - cf * o["entry_price"] * o["qty"]
        open_l.append({
            "code": c, "entry_date": o["entry_date"], "exit_date": "OPEN",
            "hold_days": 0, "qty": o["qty"], "entry_price": o["entry_price"],
            "exit_price": last_close, "gross_pct": gross_pct, "gross_pnl": gross_pnl,
            "exch": exch, "reason": "OPEN", "cost_frac": cf,
            "net_pct": net_pct, "net_pnl": net_pnl, "mkt_price": last_close,
        })
    return closed, open_l


# ============== 权益曲线 / 风险指标 ==============

def daily_equity_and_returns(closed: list[dict], open_l: list[dict],
                             capital: float) -> tuple[pd.Series, pd.Series]:
    """构造逐日权益曲线与日收益（cash+holdings 模型，含未实现 MTMark 与成本）。

    返回 (equity 序列, daily_return 序列)，索引为日历日（无交易日收益记 0，与回测口径一致）。
    """
    segs: list[dict] = []
    for t in closed:
        segs.append({
            "code": t["code"], "qty": t["qty"], "entry_price": t["entry_price"],
            "exit_price": t["exit_price"], "entry_date": pd.to_datetime(t["entry_date"]),
            "exit_date": pd.to_datetime(t["exit_date"]), "closed": True,
            "exch": t["exch"], "cost_frac": t["cost_frac"],
        })
    today = pd.Timestamp(datetime.now().date())
    for t in open_l:
        segs.append({
            "code": t["code"], "qty": t["qty"], "entry_price": t["entry_price"],
            "exit_price": t["exit_price"], "entry_date": pd.to_datetime(t["entry_date"]),
            "exit_date": today, "closed": False, "exch": t["exch"], "cost_frac": t["cost_frac"],
        })
    if not segs:
        yesterday = datetime.now().date() - timedelta(days=1)
        idx = pd.DatetimeIndex([pd.Timestamp(yesterday), today])
        equity = pd.Series(capital, index=idx)
        return equity, equity.pct_change().fillna(0.0)

    d0 = min(s["entry_date"] for s in segs)
    d1 = min(today, max(s["exit_date"] for s in segs))
    idx = pd.date_range(d0, d1, freq="D")

    # 预载价格
    price_cache: dict[str, pd.Series] = {}
    for s in segs:
        if s["code"] not in price_cache:
            price_cache[s["code"]] = _load_price_series(s["code"])

    equity_vals: list[float] = []
    for d in idx:
        dd = d.date()
        cum_cash = capital
        holdings_val = 0.0
        for s in segs:
            ed = s["entry_date"].date()
            xd = s["exit_date"].date()
            if ed > dd:
                continue
            side = (FEE_RATE + SLIP_RATE + (BJ_SLIP if s["exch"] == "北交所" else 0.0))
            # 累计现金（阶梯函数）：买入一次性扣本金+费；卖出一次性加回款-费
            cum_cash -= s["qty"] * s["entry_price"] * (1.0 + side)        # 本金 + 买入费
            if s["closed"] and xd <= dd:
                cum_cash += s["qty"] * s["exit_price"] * (1.0 - side)     # 回款 - 卖出费
            # 持仓市值（买入日~退出前一日；平仓日已现金结算，剔除避免双重计数）
            held = (dd < xd) if s["closed"] else (dd <= xd)
            if held:
                ps = price_cache.get(s["code"])
                if ps is not None and not ps.empty:
                    px = ps.asof(d)
                    if pd.notna(px):
                        holdings_val += s["qty"] * float(px)
        equity_vals.append(cum_cash + holdings_val)
    equity = pd.Series(equity_vals, index=idx)
    daily_ret = equity.pct_change().fillna(0.0)
    return equity, daily_ret


def risk_metrics(daily_ret: pd.Series, equity: pd.Series) -> dict[str, float]:
    """由日收益序列计算风险指标（与 compute_metrics 同口径）。"""
    mu = float(daily_ret.mean())
    sd = float(daily_ret.std())
    sharpe = float(mu / sd * np.sqrt(252)) if sd > 0 else 0.0
    down = daily_ret[daily_ret < 0]
    dsd = float(down.std()) if len(down) > 1 else 0.0
    sortino = float(mu / dsd * np.sqrt(252)) if dsd > 0 else 0.0
    peak = equity.cummax()
    max_dd = float(((equity - peak) / peak).min()) if len(equity) else 0.0
    span_days = (equity.index.max() - equity.index.min()).days
    n_trading = max(int(span_days * 252 / 365), len(equity))
    ann_ret = float((equity.iloc[-1] / equity.iloc[0]) ** (252 / n_trading) - 1) if equity.iloc[0] > 0 and n_trading > 0 and len(equity) > 1 else 0.0
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else 0.0
    return {"sharpe": sharpe, "sortino": sortino, "max_dd": max_dd,
            "ann_ret": ann_ret, "calmar": calmar, "n_days": len(daily_ret)}


def trade_stats(trades: list[dict], key: str) -> dict[str, float]:
    """由交易清单计算胜率/均收益/盈亏比/期望等。key='net_pct' 或 'gross_pct'。"""
    if not trades:
        return {"n": 0.0, "win_rate": 0.0, "avg_ret": 0.0, "pf": 0.0, "expectancy": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0}
    vals = np.array([t[key] for t in trades], dtype=float)
    n = len(vals)
    wins = vals[vals > 0]
    losses = vals[vals <= 0]
    wr = len(wins) / n if n else 0.0
    avg = float(vals.mean())
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    exp = wr * avg_win + (1 - wr) * avg_loss
    return {"n": float(n), "win_rate": wr, "avg_ret": avg, "pf": pf,
            "expectancy": exp, "avg_win": avg_win, "avg_loss": avg_loss}


# ============== 异常 / 状态 ==============

def detect_anomalies(closed: list[dict], open_l: list[dict], metrics: dict[str, float],
                     equity: pd.Series, capital: float) -> tuple[str, list[dict]]:
    """返回 (status, anomalies[])。status ∈ {GO, WATCH, HALT}。"""
    anoms: list[dict] = []

    # 累计净收益（含未实现）
    cum_net_pct = (equity.iloc[-1] / capital - 1) if capital > 0 and len(equity) else 0.0
    if cum_net_pct <= CUM_HALT:
        anoms.append({"level": "CRITICAL", "code": "CUM_HALT",
                      "msg": f"累计净收益 {cum_net_pct*100:.1f}% ≤ 终止阈值 {CUM_HALT*100:.0f}%"})
    elif cum_net_pct <= CUM_WATCH:
        anoms.append({"level": "WARNING", "code": "CUM_WATCH",
                      "msg": f"累计净收益 {cum_net_pct*100:.1f}% ≤ 预警阈值 {CUM_WATCH*100:.0f}%"})

    dd = metrics.get("max_dd", 0.0)
    if dd <= MAX_DD_HALT:
        anoms.append({"level": "CRITICAL", "code": "DD_HALT",
                      "msg": f"最大回撤 {dd*100:.1f}% ≤ 终止阈值 {MAX_DD_HALT*100:.0f}%"})
    elif dd <= MAX_DD_WATCH:
        anoms.append({"level": "WARNING", "code": "DD_WATCH",
                      "msg": f"最大回撤 {dd*100:.1f}% ≤ 预警阈值 {MAX_DD_WATCH*100:.0f}%"})

    # 连亏（按出场序）
    if closed:
        order = sorted(closed, key=lambda t: t["exit_date"])
        streak = 0
        best = 0
        for t in order:
            if t["net_pct"] < 0:
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        if best >= STREAK_HALT:
            anoms.append({"level": "CRITICAL", "code": "STREAK_HALT",
                          "msg": f"最大连续亏损 {best} 笔 ≥ 终止阈值 {STREAK_HALT}"})
        elif best >= STREAK_WATCH:
            anoms.append({"level": "WARNING", "code": "STREAK_WATCH",
                          "msg": f"最大连续亏损 {best} 笔 ≥ 预警阈值 {STREAK_WATCH}"})

    # 近 RECENT_N 笔胜率
    if len(closed) >= RECENT_N:
        recent = order[-RECENT_N:]
        rwr = sum(1 for t in recent if t["net_pct"] > 0) / RECENT_N
        if rwr <= RECENT_WR_HALT:
            anoms.append({"level": "CRITICAL", "code": "RECENT_WR_HALT",
                          "msg": f"近 {RECENT_N} 笔胜率 {rwr*100:.0f}% ≤ 终止阈值 {RECENT_WR_HALT*100:.0f}%"})
        elif rwr <= RECENT_WR_WATCH:
            anoms.append({"level": "WARNING", "code": "RECENT_WR_WATCH",
                          "msg": f"近 {RECENT_N} 笔胜率 {rwr*100:.0f}% ≤ 预警阈值 {RECENT_WR_WATCH*100:.0f}%"})

    # 单笔巨亏
    for t in closed:
        if t["net_pct"] <= SINGLE_LOSS_HALT:
            anoms.append({"level": "WARNING", "code": "SINGLE_LOSS",
                          "msg": f"{t['code']} 单笔净收益 {t['net_pct']*100:.1f}% ≤ 阈值 {SINGLE_LOSS_HALT*100:.0f}%"})

    # 未实现持仓超时（应已时间止损）
    today = pd.Timestamp(datetime.now().date())
    for t in open_l:
        hd = (today - pd.to_datetime(t["entry_date"])).days
        if hd > STALE_OPEN_DAYS:
            anoms.append({"level": "WARNING", "code": "STALE_OPEN",
                          "msg": f"{t['code']} 持仓 {hd} 天 > 时间止损 {STALE_OPEN_DAYS} 天，需复核出场"})

    if any(a["level"] == "CRITICAL" for a in anoms):
        return "HALT", anoms
    if any(a["level"] == "WARNING" for a in anoms):
        return "WATCH", anoms
    return "GO", anoms


def next_checkpoint(n_closed: int) -> tuple[int, str]:
    for cp, desc in CHECKPOINTS:
        if n_closed < cp:
            return cp, desc
    return CHECKPOINTS[-1][0], "已完成所有预设观察节点，进入持续监控"


# ============== 报告 ==============

def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%" if x is not None else "—"


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}" if x is not None else "—"


def emit_report(emit: str, closed: list[dict], open_l: list[dict],
                equity: pd.Series, daily_ret: pd.Series, rmet: dict[str, float],
                tstat_net: dict[str, float], tstat_gross: dict[str, float],
                metrics: dict[str, float], baseline: dict[str, float],
                sim_stat: dict[str, float] | None, status: str, anoms: list[dict],
                capital: float, journal_sha: str | None, journal_mtime: float | None,
                cost_info: dict[str, float]) -> str:
    now = datetime.now()
    today = now.date()
    n_closed = len(closed)
    n_open = len(open_l)
    cum_net_pct = (equity.iloc[-1] / capital - 1) if capital > 0 and len(equity) else 0.0
    cum_net_money = equity.iloc[-1] - capital

    # 每日 / 每周 PnL（净，已实现+未实现滚动）
    daily_net = daily_ret * equity.shift(1).fillna(capital)
    daily_net = daily_net[daily_net != 0]
    wk = daily_net.groupby(daily_net.index.to_series().dt.isocalendar().week).sum()
    recent_10 = sorted(closed, key=lambda t: t["exit_date"])[-RECENT_N:] if closed else []
    rwr = (sum(1 for t in recent_10 if t["net_pct"] > 0) / RECENT_N) if len(recent_10) >= RECENT_N else (tstat_net["win_rate"] if n_closed else 0.0)

    cp_n, cp_desc = next_checkpoint(n_closed)

    L: list[str] = []
    title = "方案 E 实盘观察报告"
    L.append(f"# {title} ({now.strftime('%Y-%m-%d %H:%M')})\n")
    L.append(f"> 模式：{'周度复盘' if emit=='weekly' else '每日监控'} ｜ 状态机：**{status}** ｜ "
             f"信号模型：{SIGNAL_MODEL} ｜ 报告版本：{__version__}\n")

    # 核心指标
    L.append("## 📊 核心指标\n")
    L.append("| 指标 | 值 |")
    L.append("|---|---:|")
    L.append(f"| 已平仓笔数 | {n_closed} |")
    L.append(f"| 未平仓笔数 | {n_open} |")
    L.append(f"| 累计净收益(含未实现) | {_fmt_money(cum_net_money)} ({_fmt_pct(cum_net_pct)}) |")
    L.append(f"| 已实现毛 PnL | {_fmt_money(sum(t['gross_pnl'] for t in closed))} |")
    L.append(f"| 净 PnL(扣成本) | {_fmt_money(sum(t['net_pnl'] for t in closed))} |")
    L.append(f"| 胜率(净) | {tstat_net['win_rate']*100:.1f}% |")
    L.append(f"| 平均收益(净) | {_fmt_pct(tstat_net['avg_ret'])} |")
    L.append(f"| 期望收益(净) | {_fmt_pct(tstat_net['expectancy'])} |")
    L.append(f"| **夏普比率** | {metrics['sharpe']:.2f} |")
    L.append(f"| **最大回撤** | {_fmt_pct(metrics['max_dd'])} |")
    L.append(f"| **索提诺** | {metrics['sortino']:.2f} |")
    L.append(f"| 年化收益 | {_fmt_pct(metrics['ann_ret'])} |")
    L.append(f"| 盈亏比 | {tstat_net['pf']:.2f} |")
    L.append(f"| 近 {RECENT_N} 笔胜率 | {rwr*100:.1f}% |")
    L.append("")

    # Top N 明细（已平仓）
    L.append("## 🎯 已平仓明细\n")
    if closed:
        order = sorted(closed, key=lambda t: t["exit_date"], reverse=True)
        L.append("| 代码 | 交易所 | 入场 | 出场 | 持有 | 数量 | 买价 | 卖价 | 毛% | 净% | 净PnL¥ | 出场 |")
        L.append("|---|:---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for t in order[:30]:
            L.append(f"| {t['code']} | {t['exch']} | {t['entry_date']} | {t['exit_date']} | "
                     f"{t['hold_days']} | {t['qty']:,} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | "
                     f"{_fmt_pct(t['gross_pct'])} | **{_fmt_pct(t['net_pct'])}** | {_fmt_money(t['net_pnl'])} | {t['reason']} |")
        if len(order) > 30:
            L.append(f"\n（仅列最新 30 笔，共 {len(order)} 笔）")
    else:
        L.append("（尚无已平仓交易）")

    if open_l:
        L.append("\n### 未平仓（MTMark，净为估算）\n")
        L.append("| 代码 | 交易所 | 入场 | 数量 | 买价 | 现价 | 毛% | 净%(估算) | 未实现净PnL¥ |")
        L.append("|---|:---:|---|---:|---:|---:|---:|---:|---:|")
        for t in open_l:
            L.append(f"| {t['code']} | {t['exch']} | {t['entry_date']} | {t['qty']:,} | "
                     f"{t['entry_price']:.2f} | {t['exit_price']:.2f} | {_fmt_pct(t['gross_pct'])} | "
                     f"{_fmt_pct(t['net_pct'])} | {_fmt_money(t['net_pnl'])} |")

    # 统计分布
    L.append("\n## 📈 统计分布\n")
    if closed:
        bins = [-1.0, -0.10, -0.03, 0.0, 0.10, 0.30, 1.0]
        labels = ["≤-10%", "-10%~-3%", "-3%~0", "0~10%", "10%~30%", "≥30%"]
        cut = pd.cut(pd.Series([t["net_pct"] for t in closed]), bins=bins, labels=labels, include_lowest=True)
        dist = cut.value_counts().reindex(labels).fillna(0).astype(int)
        L.append("| 净收益区间 | 笔数 |")
        L.append("|---|---:|")
        for lab, v in dist.items():
            L.append(f"| {lab} | {int(v)} |")
    else:
        L.append("（尚无已平仓交易，分布待累积）")

    # 与基线偏差
    L.append("\n## 🔍 与回测/模拟偏差\n")
    L.append(f"基线：{baseline.get('note', '方案E v2 回测(含成本, n=136)')} ｜ "
             f"模拟：{'有(journal_sim)' if sim_stat else '无'}\n")
    L.append("| 指标 | 实盘(净) | 回测基线 | 偏差 | 模拟 |")
    L.append("|---|---:|---:|---:|---:|")

    def prow(name: str, lv: Any, bv: Any, sv: Any, is_pct: bool = True) -> None:
        def fmt(v: Any, pct: bool) -> str:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "—"
            return _fmt_pct(v) if pct else f"{v:.2f}"
        b = fmt(bv, is_pct)
        s = fmt(sv, is_pct)
        d = fmt(lv - bv, is_pct) if (isinstance(bv, float) and isinstance(lv, float)) else "—"
        L.append(f"| {name} | {fmt(lv, is_pct)} | {b} | {d} | {s} |")

    prow("胜率", tstat_net["win_rate"], baseline.get("win_rate"), sim_stat["win_rate"] if sim_stat else None, True)
    prow("平均收益", tstat_net["avg_ret"], baseline.get("avg_ret"), sim_stat["avg_ret"] if sim_stat else None, True)
    prow("夏普", metrics["sharpe"], baseline.get("sharpe"), None, False)
    prow("最大回撤", metrics["max_dd"], baseline.get("max_dd"), None, True)
    prow("年化收益", metrics["ann_ret"], baseline.get("ann_ret"), None, True)
    prow("盈亏比", tstat_net["pf"], baseline.get("pf"), sim_stat["pf"] if sim_stat else None, False)
    L.append("")

    # 成本 / 流动性影响
    L.append("## 💸 成本与流动性影响\n")
    gross_total = sum(t["gross_pnl"] for t in closed)
    net_total = sum(t["net_pnl"] for t in closed)
    cost_drag = gross_total - net_total
    L.append(f"- 已平仓毛 PnL：**{_fmt_money(gross_total)}** ｜ 净 PnL：**{_fmt_money(net_total)}**")
    L.append(f"- **成本侵蚀合计：{_fmt_money(cost_drag)}**（占毛 PnL 的 "
             f"{(cost_drag/gross_total*100 if gross_total else 0):.2f}%）")
    L.append(f"- 单边佣金 {FEE_RATE*100:.3f}% + 滑点 {SLIP_RATE*100:.3f}%（沪深，往返 "
             f"{(FEE_RATE+SLIP_RATE)*2*100:.3f}%）；北交所额外滑点 {BJ_SLIP*100:.3f}%/边。")
    # 北交所 vs 沪深
    bj = [t for t in closed if t["exch"] == "北交所"]
    hs = [t for t in closed if t["exch"] != "北交所"]
    if bj:
        L.append(f"- 北交所成交 {len(bj)} 笔，毛均 {_fmt_pct(tstat_avg(bj,'gross_pct'))}、净均 "
                 f"{_fmt_pct(tstat_avg(bj,'net_pct'))}（流动性低、滑点放大，重点监控）")
    if hs:
        L.append(f"- 沪深成交 {len(hs)} 笔，毛均 {_fmt_pct(tstat_avg(hs,'gross_pct'))}、净均 "
                 f"{_fmt_pct(tstat_avg(hs,'net_pct'))}")
    L.append("")

    # 异常 / 状态
    L.append("## ⚠️ 异常与状态机\n")
    L.append(f"**当前状态：{status}**")
    L.append("")
    L.append("### 触发阈值（WATCH→预警 / HALT→终止）")
    L.append(f"- 最大回撤：预警 {_fmt_pct(MAX_DD_WATCH)} / 终止 {_fmt_pct(MAX_DD_HALT)}")
    L.append(f"- 累计净收益：预警 {_fmt_pct(CUM_WATCH)} / 终止 {_fmt_pct(CUM_HALT)}")
    L.append(f"- 连续亏损：预警 {STREAK_WATCH} 笔 / 终止 {STREAK_HALT} 笔")
    L.append(f"- 近 {RECENT_N} 笔胜率：预警 {RECENT_WR_WATCH*100:.0f}% / 终止 {RECENT_WR_HALT*100:.0f}%")
    L.append(f"- 单笔净亏损终止阈值：{_fmt_pct(SINGLE_LOSS_HALT)}")
    L.append("")
    if anoms:
        L.append("### 本次触发\n")
        L.append("| 级别 | 代码 | 说明 |")
        L.append("|---|:---:|---|")
        for a in anoms:
            L.append(f"| {a['level']} | {a['code']} | {a['msg']} |")
    else:
        L.append("### 本次触发\n（无异常，未触及任何阈值）")
    L.append("")

    # 观察节点
    L.append("## 🗓️ 观察周期与评估节点\n")
    L.append(f"当前进度：已平仓 **{n_closed}** 笔 ｜ 下一节点：第 **{cp_n}** 笔（{cp_desc}）\n")
    L.append("| 节点(笔数) | 评估内容 | 当前 |")
    L.append("|---|---|---|")
    for cp, desc in CHECKPOINTS:
        mark = "✓" if n_closed >= cp else "待"
        L.append(f"| {cp} | {desc} | {mark} |")
    L.append("")
    L.append("### 偏离预期的调整 / 终止条件\n")
    L.append(f"- **WATCH**：缩减新仓仓位（10%→5%）并加强人工复核；若累计净收益持续 ≤ {_fmt_pct(CUM_WATCH)} "
             f"或回撤 ≤ {_fmt_pct(MAX_DD_WATCH)} 超过 5 个交易日，暂停新开仓。")
    L.append(f"- **HALT**：立即暂停新开仓并复盘；若累计净收益 ≤ {_fmt_pct(CUM_HALT)} 或回撤 ≤ {_fmt_pct(MAX_DD_HALT)} "
             f"或连亏 ≥ {STREAK_HALT} 笔，启动**终止评估**（清仓 / 停用信号模型 / 回退分类器）。")
    L.append("- 实盘净胜率 < 基线 15pp 或平均收益 < 基线 50%，视为策略失效信号，优先降低暴露。")
    L.append("")

    # 风险提示
    L.append("## ⚠️ 风险提示\n")
    L.append("1. 历史回测不代表未来收益；实盘样本极小时（<30 笔）所有指标为参考性，不构成统计结论。")
    L.append("2. 翻倍妖股策略暴露于极端波动与流动性风险；北交所（920XXX）流动性低、滑点可能高于假设。")
    L.append("3. 成本模型（佣金/滑点）为**可配置假设**，实际成交成本以券商对账单为准；本报告净收益据此估算。")
    L.append("4. 当前基线取自 v2 回测（含成本），实盘为观测值；二者口径一致但样本分布不同，偏差需结合样本量解读。")
    L.append("5. 严格止损（sl=-10% / 时间止损 10 日）；本监控 HALT 仅给出信号，最终交易决策由人工执行。")
    L.append("")

    # 下一步
    L.append("## 🔧 下一步建议\n")
    L.append("1. 持续记录每笔成交到 journal（`plan_e_journal.py --mode buy/sell`），确保可追溯。")
    L.append("2. 每周日 20:00 自动产出周报；每日 18:40 自动产出监控报告与历史快照。")
    L.append("3. 待实盘累积 ≥30 笔后，用 `plan_e_backtest.py --real` 做按年 / walk-forward 观测分析（n≥30 闸口）。")
    L.append("4. 若触发 WATCH/HALT，按上节条件调整仓位或启动终止评估。")
    L.append("")

    # 可追溯指纹
    L.append("---\n")
    L.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n")
    L.append(f"生成者：monitor_plan_e_live.py {__version__}\n")
    L.append(f"信号模型：{SIGNAL_MODEL}\n")
    L.append(f"数据日期：{today.isoformat()}\n")
    L.append(f"输入指纹：journal_sha256={journal_sha} journal_mtime={journal_mtime} "
             f"capital={capital:,.0f} cost(fee={FEE_RATE},slip={SLIP_RATE},bj={BJ_SLIP})\n")
    return "\n".join(L)


def tstat_avg(trades: list[dict], key: str) -> float:
    if not trades:
        return 0.0
    return float(np.mean([t[key] for t in trades]))


def load_sim_stat() -> dict[str, float] | None:
    sim_path = LIVE_DIR / "journal_sim.csv"
    if not sim_path.exists():
        return None
    df = pd.read_csv(sim_path)
    if "pnl_pct" not in df.columns or df.empty:
        return None
    vals = df["pnl_pct"].astype(float).values
    n = len(vals)
    wins = vals[vals > 0]
    losses = vals[vals <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    return {"n": float(n), "win_rate": len(wins) / n, "avg_ret": float(vals.mean()),
            "pf": abs(avg_win / avg_loss) if avg_loss != 0 else 0.0}


# ============== 历史 / 快照 ==============

def update_history(emit: str, row: dict[str, Any]) -> None:
    row = dict(row)
    row["emit"] = emit
    cols = list(row.keys())
    if HISTORY.exists():
        hist = pd.read_csv(HISTORY)
        # upsert by (date, emit)
        mask = (hist["date"] == row["date"]) & (hist["emit"] == emit)
        hist = hist[~mask]
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row], columns=cols)
    hist.to_csv(HISTORY, index=False, encoding="utf-8-sig")


def write_snapshot(now: datetime, payload: dict[str, Any]) -> Path:
    OBS_DIR.mkdir(parents=True, exist_ok=True)
    fp = OBS_DIR / f"monitor_snapshot_{now.strftime('%Y%m%d_%H%M%S')}.json"
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return fp


# ============== Main ==============

def main() -> None:
    ap = argparse.ArgumentParser(description="方案 E 实盘监控（真实 PnL 跟踪与偏差分析）")
    ap.add_argument("--emit", choices=["auto", "daily", "weekly", "snapshot"], default="auto",
                    help="输出粒度：auto=周五周报其余日报；snapshot=仅写历史+快照")
    ap.add_argument("--capital", type=float, default=100000.0, help="部署资金（用于 %% 折算，默认 100000）")
    ap.add_argument("--baseline", default=None, help="基线 JSON 路径（覆盖默认 v2 回测）")
    ap.add_argument("--no-freshness-check", action="store_true", help="跳过数据新鲜度守卫")
    ap.add_argument("--quiet", action="store_true", help="减少 stdout 输出（供 systemd）")
    args = ap.parse_args()

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    emit = args.emit
    if emit == "auto":
        emit = "weekly" if datetime.now().weekday() == 4 else "daily"

    baseline = DEFAULT_BASELINE.copy()
    if args.baseline:
        try:
            baseline.update(json.loads(Path(args.baseline).read_text(encoding="utf-8")))
        except Exception as e:
            print(f"⚠ 基线文件读取失败，用默认: {e}")

    journal = load_journal()
    closed, open_l = build_trades(journal)
    equity, daily_ret = daily_equity_and_returns(closed, open_l, args.capital)
    rmet = risk_metrics(daily_ret, equity)
    tstat_net = trade_stats(closed, "net_pct")
    tstat_gross = trade_stats(closed, "gross_pct")
    sim_stat = load_sim_stat()
    status, anoms = detect_anomalies(closed, open_l, rmet, equity, args.capital)
    journal_sha, journal_mtime = _journal_sha(JOURNAL)

    cum_net_pct = (equity.iloc[-1] / args.capital - 1) if args.capital > 0 and len(equity) else 0.0
    cum_net_money = equity.iloc[-1] - args.capital
    cost_drag = sum(t["gross_pnl"] for t in closed) - sum(t["net_pnl"] for t in closed)

    now = datetime.now()
    report = emit_report(emit, closed, open_l, equity, daily_ret, rmet, tstat_net, tstat_gross,
                         rmet, baseline, sim_stat, status, anoms, args.capital,
                         journal_sha, journal_mtime, {"cost_drag": cost_drag})

    OBS_DIR.mkdir(parents=True, exist_ok=True)
    if emit in ("daily", "weekly"):
        if emit == "weekly":
            fname = f"plan_e_live_week_{now.strftime('%G-W%V')}.md"
        else:
            fname = f"plan_e_live_{now.strftime('%Y-%m-%d')}.md"
        (OBS_DIR / fname).write_text(report, encoding="utf-8")
        (OBS_DIR / "plan_e_live_latest.md").write_text(report, encoding="utf-8")

    # 历史 upsert
    row = {
        "date": now.strftime("%Y-%m-%d"), "n_closed": len(closed), "n_open": len(open_l),
        "cum_net_pnl": round(cum_net_money, 2), "cum_net_pct": round(cum_net_pct, 6),
        "gross_pnl": round(sum(t["gross_pnl"] for t in closed), 2),
        "cost_drag": round(cost_drag, 2), "net_pnl": round(sum(t["net_pnl"] for t in closed), 2),
        "win_rate": round(tstat_net["win_rate"], 6), "avg_ret_net": round(tstat_net["avg_ret"], 6),
        "sharpe": round(rmet["sharpe"], 4), "sortino": round(rmet["sortino"], 4),
        "max_dd": round(rmet["max_dd"], 6), "calmar": round(rmet["calmar"], 4),
        "pf": round(tstat_net["pf"], 4), "expectancy": round(tstat_net["expectancy"], 6),
        "status": status, "n_anom": len(anoms),
        "journal_sha": journal_sha or "",
    }
    update_history(emit, row)

    # 快照
    snap = write_snapshot(now, {
        "version": __version__, "emit": emit, "signal_model": SIGNAL_MODEL,
        "generated_at": now.isoformat(timespec="seconds"),
        "capital": args.capital, "baseline": baseline, "cost": {"fee": FEE_RATE, "slip": SLIP_RATE, "bj": BJ_SLIP},
        "inputs": {"journal_sha256": journal_sha, "journal_mtime": journal_mtime, "n_journal_rows": len(journal)},
        "metrics": {"risk": rmet, "trade_net": tstat_net, "trade_gross": tstat_gross, "sim": sim_stat},
        "status": status, "anomalies": anoms,
        "closed_trades": closed, "open_trades": open_l,
    })

    if not args.quiet:
        print(report)
        print(f"\n=== ✅ 状态={status} 已平仓={len(closed)} 未平仓={len(open_l)} "
              f"累计净={_fmt_pct(cum_net_pct)} 异常={len(anoms)} ===")
        print(f"  报告: {OBS_DIR / (fname if emit in ('daily','weekly') else 'plan_e_live_latest.md')}")
        print(f"  快照: {snap.name}")
    else:
        print(f"[live-monitor] status={status} trades={len(closed)} cum_net={cum_net_pct*100:+.2f}% "
              f"anom={len(anoms)} report={'Y' if emit in ('daily','weekly') else 'N'}")


if __name__ == "__main__":
    main()
