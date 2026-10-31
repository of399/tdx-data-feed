"""横盘突破信号的 T+1 胜率与期望评估。

对齐鲁《A股打板交易策略执行计划书》的闸门口径：**直接复用 Stage 2 的执行与成本模型**
（``tdxfeed.daban.execution_backtest``），这样跑出来的数字能和
``data/daban/execution_backtest_20261022.json`` 里的 baseline 放在同一张表上比。

两层评估
--------
Layer A —— 全量信号：横盘突破选出来的票绝大多数当日并不触板（本地实测只约 15% 收盘涨停），
  Stage 0 事件表里根本没有它们的行，所以这层用本地日线自建 T/T+1/T+2 价格，按两种可执行
  入场口径算毛/净收益，并给出**同交易日全市场等权基线**作为对照。
    · A1 尾盘买：T 收盘价进 → T+1 收盘出（当日收盘涨停的票视为买不进，单独剔除）
    · A2 次日买：T+1 开盘进 → T+2 开盘出（T+1 开盘即涨停的票视为买不进）

Layer B —— 触板子集：信号与 Stage 0 事件表按 (code, date) 内连接，只保留"当日触板"的
  横盘突破，然后走 Stage 2 的原函数（select_signals/simulate_signals/aggregate），
  于是 fill_rate、seal_rate_fills、avg_net、profit_factor、日度 t 全部同口径可比。

用法::

    venv/bin/python scripts/hengpan_t1_eval.py                 # 默认参数网格
    venv/bin/python scripts/hengpan_t1_eval.py --hp 20 --band 8 --vr 1.5
    venv/bin/python scripts/hengpan_t1_eval.py --start 20250905 --end 20261023

产物：``data/daban/hengpan_t1_eval_YYYYMMDD.json``
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.execution_backtest import (  # noqa: E402
    ExecutionConfig,
    net_return,
    run_rule,
)
from tdxfeed.daban.limit_price import (  # noqa: E402
    as_date,
    is_st,
    limit_ratio,
    limit_up_price,
    normalize_code,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import daily_dir, universe_path  # noqa: E402

DAILY_DIR = daily_dir()
EVENTS_PATH = _ROOT / "data" / "daban" / "event_table_min60_20261022.parquet"
UNIVERSE_PATH = universe_path()
OUT_DIR = _ROOT / "data" / "daban"
EPS = 0.001
ORIGINAL_COL = "sig_original_broken"

# 参数网格：(横盘周期, 前序振幅上限%, 放量倍数)
GRID = [
    (20, 2.0, 1.5),    # 用户原稿缺省
    (20, 5.0, 1.5),
    (20, 8.0, 1.5),    # 修订版缺省
    (20, 12.0, 1.5),
    (10, 8.0, 1.5),
    (10, 12.0, 1.5),
    (20, 8.0, 2.5),
]

# Stage 2 的硬闸门，原样搬过来做判定
GATE = {"net_expectancy_gt": 0.002, "fill_rate_ge": 0.30, "min_fills": 30}


# ---------------------------------------------------------------- 纯函数


def breakout_flags(
    df: pd.DataFrame,
    hp_days: int = 20,
    band_pct: float = 8.0,
    vol_ratio: float = 1.5,
    *,
    use_ref: bool = True,
) -> pd.Series:
    """横盘突破布尔序列。

    ``use_ref=True`` 是修订版：箱体窗口取"截至前一交易日"，于是 ``C > 横盘高点`` 可能为真。
    ``use_ref=False`` 是用户原稿：窗口含当日，``C<=H<=HHV(H,N)`` 恒成立 → 永远不触发。
    """
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"]
    hhv = high.rolling(int(hp_days)).max()
    llv = low.rolling(int(hp_days)).min()
    if use_ref:
        hhv = hhv.shift(1)
        llv = llv.shift(1)
    width = hhv - llv
    band = width / llv * 100.0
    sideways = (width > 0) & (band < float(band_pct))
    heavy = vol > vol.rolling(5).mean() * float(vol_ratio)
    return ((close > hhv) & sideways & heavy).astype(bool)


def attach_forward(df: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    """按**市场日历**拼接 T+1 / T+2 价格。

    本地日线里"下一行"可能是复牌后的第 20 个交易日，直接 shift 会把长停复牌的第一根
    当成 T+1。这里要求下一行必须正好是日历里的下一天，否则 ``forward_ok=False``。
    """
    pos = {d: i for i, d in enumerate(calendar)}
    out = df.reset_index(drop=True).copy()
    n = len(out)
    dates = [str(d)[:10] for d in out["date"]]
    next_cols: dict[str, list] = {
        "next_date": [None] * n,
        "next_open": [np.nan] * n,
        "next_high": [np.nan] * n,
        "next_low": [np.nan] * n,
        "next_close": [np.nan] * n,
        "next2_open": [np.nan] * n,
    }
    ok = [False] * n
    opens = out["open"].to_numpy(dtype=float)
    highs = out["high"].to_numpy(dtype=float)
    lows = out["low"].to_numpy(dtype=float)
    closes = out["close"].to_numpy(dtype=float)
    for i in range(n - 1):
        d0, d1 = dates[i], dates[i + 1]
        i0 = pos.get(d0)
        i1 = pos.get(d1)
        if i0 is None or i1 is None or i1 != i0 + 1:
            continue  # 停牌/退市/日历外
        ok[i] = True
        next_cols["next_date"][i] = d1
        next_cols["next_open"][i] = opens[i + 1]
        next_cols["next_high"][i] = highs[i + 1]
        next_cols["next_low"][i] = lows[i + 1]
        next_cols["next_close"][i] = closes[i + 1]
        if i + 2 < n and pos.get(dates[i + 2]) == i1 + 1:
            next_cols["next2_open"][i] = opens[i + 2]
    for k, v in next_cols.items():
        out[k] = v
    out["forward_ok"] = ok
    return out


def summary(rets) -> dict:
    """胜率 / 均值 / 中位数 / 盈亏比 / 日度无关的 t。"""
    r = np.asarray(rets, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return {"n": 0, "win_rate": float("nan"), "mean": float("nan"),
                "median": float("nan"), "profit_factor": float("nan"),
                "t": float("nan"), "p10": float("nan"), "p90": float("nan")}
    wins = r[r > 0]
    losses = r[r < 0]
    gross_loss = abs(float(losses.sum()))
    pf = float(wins.sum()) / gross_loss if gross_loss > 0 else math.inf
    mean = float(r.mean())
    sd = float(r.std(ddof=1)) if r.size > 1 else 0.0
    if sd > 0:
        t = mean / (sd / math.sqrt(r.size))
    else:
        t = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    return {
        "n": int(r.size),
        "win_rate": float((r > 0).mean()),
        "mean": mean,
        "median": float(np.median(r)),
        "profit_factor": pf,
        "t": t,
        "p10": float(np.percentile(r, 10)),
        "p90": float(np.percentile(r, 90)),
    }


# ---------------------------------------------------------------- 面板构建


def load_universe(path: Path = UNIVERSE_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_panel(
    daily_dir: Path,
    universe: dict[str, str],
    grid: list[tuple[int, float, float]],
    *,
    date_from: str | None = None,
    min_bars: int = 30,
) -> tuple[pd.DataFrame, list[str]]:
    """把本地日线拼成一张带 T+1/T+2 价格和逐组合信号标记的面板。

    本地日线是**全历史**（单只 6000 行、全市场 3000 万行级别），所以必须先用
    ``date_from`` 切片再做滚动计算，否则面板会爆内存。``date_from`` 要比评估起点
    多留一段缓冲，好让起点当天也算得出 20 日箱体。
    """
    files = sorted(p for p in daily_dir.glob("*.parquet") if not p.stem.endswith(".bak"))
    frames = []
    calendar: set[str] = set()
    for p in files:
        code = normalize_code(p.stem)
        try:
            df = pd.read_parquet(
                p, columns=["date", "open", "high", "low", "close", "volume"]
            )
        except Exception:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = [str(d)[:10] for d in df["date"]]
        if date_from:
            df = df[df["date"] >= date_from].reset_index(drop=True)
        if len(df) < min_bars:
            continue
        calendar.update(df["date"])
        frames.append((code, df))
    cal = sorted(calendar)
    rows: list[pd.DataFrame] = []
    for code, df in frames:
        named = attach_forward(df, cal)
        named.insert(0, "code", code)
        named["name"] = universe.get(code, "")
        prev_close = named["close"].shift(1)
        named["limit_price"] = [
            float(limit_up_price(pc, limit_ratio(code, named["name"], on_date=as_date(d))))
            if np.isfinite(pc) and pc > 0 else np.nan
            for pc, d in zip(prev_close.to_numpy(), named["date"], strict=True)
        ]
        named["sealed_today"] = (
            (named["close"] >= named["limit_price"] - EPS) & named["limit_price"].notna()
        )
        named["is_st"] = [int(is_st(n)) for n in named["name"]]
        named["is_new"] = [int(str(n).startswith(("N", "C"))) for n in named["name"]]
        for hp_days, band_pct, vol_ratio in grid:
            named[_sig_col(hp_days, band_pct, vol_ratio)] = breakout_flags(
                named, hp_days, band_pct, vol_ratio
            )
        # 用户原稿口径（箱体窗口含当日）留一列做对照，结构上应恒为 False
        named[ORIGINAL_COL] = breakout_flags(named, 20, 2.0, 1.5, use_ref=False)
        rows.append(named)
    panel = pd.concat(rows, ignore_index=True)
    return panel, cal


def _sig_col(hp_days: int, band_pct: float, vol_ratio: float) -> str:
    return f"sig_{hp_days}_{band_pct:g}_{vol_ratio:g}"


# ---------------------------------------------------------------- Layer A


def layer_a(panel: pd.DataFrame, sig_col: str, cfg: ExecutionConfig) -> dict:
    """全量信号：两种入场口径的毛/净收益 + 同交易日全市场等权基线。"""
    hit = panel[(panel[sig_col]) & (panel["forward_ok"])].copy()
    out: dict[str, object] = {"n_signals": len(hit),
                              "n_days": int(hit["date"].nunique()),
                              "avg_per_day": float(len(hit) / max(hit["date"].nunique(), 1))}
    if hit.empty:
        out.update({"a1": summary(np.array([])), "a2": summary(np.array([])),
                    "a1_tradable": summary(np.array([])),
                    "a2_tradable": summary(np.array([])), "baseline": {}})
        return out

    # A1：T 收盘进 → T+1 收盘出；当日收盘涨停视为买不进
    a1_gross = hit["next_close"] / hit["close"] - 1.0
    a1_net = [net_return(c, nc, cfg) for c, nc in zip(hit["close"], hit["next_close"], strict=True)]
    out["a1"] = summary(a1_net)
    can_a1 = ~hit["sealed_today"]
    out["a1_tradable"] = summary([n for n, k in zip(a1_net, can_a1, strict=True) if bool(k)])
    out["a1_gross_all"] = summary(a1_gross)

    # A2：T+1 开盘进 → T+2 开盘出；T+1 开盘即涨停视为买不进
    hit["next_limit"] = [
        float(limit_up_price(pc, limit_ratio(code, "", on_date=as_date(d))))
        if np.isfinite(pc) and pc > 0 else np.nan
        for pc, code, d in zip(hit["close"], hit["code"], hit["next_date"], strict=True)
    ]
    open_at_limit = hit["next_open"] >= hit["next_limit"] - EPS
    a2_net = [net_return(no, n2, cfg)
              for no, n2 in zip(hit["next_open"], hit["next2_open"], strict=True)]
    out["a2"] = summary(a2_net)
    can_a2 = (~open_at_limit) & np.isfinite(hit["next2_open"].to_numpy(dtype=float))
    out["a2_tradable"] = summary([n for n, k in zip(a2_net, can_a2, strict=True) if bool(k)])

    # 封板占比（信号里当日收盘涨停的比例）
    out["sealed_close_rate"] = float(hit["sealed_today"].mean())
    out["st_rate"] = float(hit["is_st"].mean())
    out["new_rate"] = float(hit["is_new"].mean())

    # 基线：同一批交易日、同样可成交假设下的全市场等权
    days = sorted(hit["date"].unique())
    base_pool = panel[(panel["date"].isin(days)) & (panel["forward_ok"]) & (~panel["sealed_today"])]
    base_net = [net_return(c, nc, cfg)
                for c, nc in zip(base_pool["close"].to_numpy(), base_pool["next_close"].to_numpy(), strict=True)]
    bs = summary(base_net)
    out["baseline"] = {k: bs[k] for k in ("n", "win_rate", "mean", "median")}
    out["alpha_a1_tradable"] = (out["a1_tradable"]["mean"] - bs["mean"]
                                if math.isfinite(out["a1_tradable"]["mean"]) else float("nan"))
    return out


# ---------------------------------------------------------------- Layer B


def layer_b(events_path: Path, panel: pd.DataFrame, sig_col: str,
            cfg: ExecutionConfig) -> dict:
    """与 Stage 0 事件表内连接：只评"当日触板"的横盘突破，走 Stage 2 原函数。"""
    if not events_path.exists():
        return {"skipped": f"缺少 {events_path.name}"}
    ev = pd.read_parquet(events_path)
    ev["code"] = ev["code"].astype(str).str.zfill(6)
    ev["date"] = ev["date"].astype(str)
    sig = panel[panel[sig_col]][["code", "date"]].copy()
    sig["date"] = sig["date"].str.replace("-", "", regex=False)
    key = set(zip(sig["code"], sig["date"], strict=True))
    sub = ev[[ (c, d) in key for c, d in zip(ev["code"], ev["date"], strict=True) ]].copy()
    out: dict[str, object] = {
        "n_events_total": len(ev),
        "n_signals_touch": len(sub),
        "overlap_rate": float(len(sub) / max(len(sig), 1)),
    }
    if sub.empty:
        out.update({"rule": summary(np.array([])), "all_events": summary(np.array([]))})
        out["gate_passed"] = False
        return out
    rule, _trades = run_rule(sub, "hengpan_touch", cfg)
    allrule, _alltrades = run_rule(ev, "all_touch_events", cfg)
    out["rule"] = {k: rule.get(k) for k in
                   ("n_signals", "n_fills", "fill_rate", "seal_rate_fills", "avg_net",
                    "median_net", "win_rate", "profit_factor", "daily_mean", "t",
                    "avg_net_sealed", "avg_net_unsealed", "total_return", "max_drawdown")}
    out["all_events"] = {k: allrule.get(k) for k in
                         ("n_signals", "n_fills", "fill_rate", "seal_rate_fills", "avg_net",
                          "win_rate", "profit_factor", "t")}
    g = GATE
    out["gate_passed"] = bool(
        np.isfinite(rule.get("avg_net", np.nan))
        and rule["avg_net"] > g["net_expectancy_gt"]
        and rule["fill_rate"] >= g["fill_rate_ge"]
        and rule["n_fills"] >= g["min_fills"]
    )
    return out


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="横盘突破 T+1 胜率与期望评估")
    ap.add_argument("--hp", type=int, default=20, help="横盘周期（交易日）")
    ap.add_argument("--band", type=float, default=8.0, help="前序振幅上限%%")
    ap.add_argument("--vr", type=float, default=1.5, help="放量倍数")
    ap.add_argument("--grid", action="store_true", help="跑内置参数网格（忽略 --hp/--band/--vr）")
    ap.add_argument("--start", default="20250905", help="评估起日 YYYYMMDD（默认对齐事件表）")
    ap.add_argument("--end", default="20261023", help="评估止日 YYYYMMDD")
    ap.add_argument("--fee-bps", type=float, default=2.5)
    ap.add_argument("--stamp-bps", type=float, default=5.0)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    cfg = ExecutionConfig(wait=1, exit_mode="open", fee_bps=args.fee_bps,
                          stamp_bps=args.stamp_bps, slip_bps=args.slip_bps)
    grid = list(GRID) if args.grid else [(args.hp, args.band, args.vr)]
    start, end = str(args.start).replace("-", ""), str(args.end).replace("-", "")

    universe = load_universe()
    buffer_from = (datetime.strptime(start, "%Y%m%d") - timedelta(days=90)).strftime("%Y-%m-%d")
    print(f"[info] 本地日线目录 {DAILY_DIR.name}，股票名册 {len(universe)} 只，"
          f"评估区间 {start}~{end}（箱体缓冲自 {buffer_from}）")
    panel, _cal = build_panel(DAILY_DIR, universe, grid, date_from=buffer_from)
    panel["day"] = panel["date"].str.replace("-", "", regex=False)
    panel = panel[(panel["day"] >= start) & (panel["day"] <= end)].reset_index(drop=True)
    print(f"[info] 面板 {len(panel):,} 行，{panel['code'].nunique()} 只，"
          f"{panel['date'].nunique()} 个交易日")

    report: dict[str, object] = {
        "stage": "hengpan_t1_eval",
        "window": {"start": start, "end": end, "panel_rows": len(panel),
                   "n_days": int(panel["date"].nunique())},
        "execution_config": cfg.to_dict(),
        "gate": GATE,
        "combos": {},
    }
    for hp_days, band_pct, vol_ratio in grid:
        key = f"HP{hp_days}_BAND{band_pct:g}_VR{vol_ratio:g}"
        sig_col = _sig_col(hp_days, band_pct, vol_ratio)
        a = layer_a(panel, sig_col, cfg)
        b = layer_b(EVENTS_PATH, panel, sig_col, cfg)
        monthly = _monthly(panel, sig_col, cfg)
        report["combos"][key] = {"layer_a": a, "layer_b": b, "monthly": monthly}
        _print_combo(key, a, b, monthly)

    n_original = int(panel[ORIGINAL_COL].sum())
    report["original_formula_check"] = {
        "description": "用户原稿 C>HHV(H,N)（窗口含当日），理论上恒不成立",
        "n_signals": n_original,
    }
    print(f"\n[对照] 原稿写法（窗口含当日）在评估区间内命中 {n_original} 个信号（应为 0）")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"hengpan_t1_eval_{pd.Timestamp.now():%Y%m%d}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_jsonable),
                        encoding="utf-8")
    print(f"[done] 写出 {out_path}")
    return 0


def _monthly(panel: pd.DataFrame, sig_col: str, cfg: ExecutionConfig) -> dict:
    hit = panel[(panel[sig_col]) & (panel["forward_ok"]) & (~panel["sealed_today"])].copy()
    if hit.empty:
        return {}
    hit["net"] = [net_return(c, nc, cfg) for c, nc in zip(hit["close"], hit["next_close"], strict=True)]
    hit["month"] = hit["date"].str[:7]
    res = {}
    for m, g in hit.groupby("month"):
        res[m] = {"n": len(g), "win_rate": float((g["net"] > 0).mean()),
                  "mean": float(g["net"].mean())}
    return res


def _print_combo(key: str, a: dict, b: dict, monthly: dict) -> None:
    a1 = a["a1_tradable"]
    a2 = a["a2_tradable"]
    base = a.get("baseline", {})
    print(f"\n===== {key} =====")
    print(f"  信号数 {a['n_signals']} · 覆盖 {a['n_days']} 天 · 日均 {a['avg_per_day']:.1f} 只"
          f" · 其中当日收盘涨停 {a.get('sealed_close_rate', float('nan')) * 100:.1f}%")
    if a1["n"]:
        print(f"  A1 尾盘买T收盘→T+1收盘 : n={a1['n']:5d} 胜率 {a1['win_rate']*100:5.1f}% "
              f"净期望 {a1['mean']*100:+6.2f}% 中位 {a1['median']*100:+6.2f}% "
              f"PF {a1['profit_factor']:.2f} t {a1['t']:+.2f}")
    if a2["n"]:
        print(f"  A2 次日开盘→T+2开盘    : n={a2['n']:5d} 胜率 {a2['win_rate']*100:5.1f}% "
              f"净期望 {a2['mean']*100:+6.2f}% 中位 {a2['median']*100:+6.2f}% "
              f"PF {a2['profit_factor']:.2f} t {a2['t']:+.2f}")
    if base.get("n"):
        print(f"  同日全市场基线(A1口径) : n={base['n']:6d} 胜率 {base['win_rate']*100:5.1f}% "
              f"均值 {base['mean']*100:+6.2f}%  → 超额 "
              f"{(a.get('alpha_a1_tradable') or float('nan'))*100:+6.2f}%")
    if b.get("rule") and b["rule"].get("n_fills"):
        r = b["rule"]
        pf = r["profit_factor"]
        tstat = r["t"]
        print(f"  B 触板子集(Stage2口径) : 信号 {b['n_signals_touch']} 成交率 "
              f"{r['fill_rate']*100:.1f}% 封板率 {r['seal_rate_fills']*100:.1f}% "
              f"净期望 {r['avg_net']*100:+6.2f}% 胜率 {r['win_rate']*100:5.1f}% "
              f"成交 {r['n_fills']} 笔 PF "
              f"{'inf' if pf is None or (isinstance(pf, float) and pf == float('inf')) else f'{pf:.2f}'} "
              f"t {tstat if tstat is None else f'{tstat:+.2f}'} 闸门通过={b['gate_passed']}")
    else:
        print(f"  B 触板子集(Stage2口径) : 信号 {b.get('n_signals_touch', 0)} 只")


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
