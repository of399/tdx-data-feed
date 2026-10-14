"""多因子回测引擎（v1）：截面打分 → ICIR 加权 → ERC 风险预算 → T+1 开盘执行 → 组合净值循环。

设计见 docs/MULTIFACTOR.md。复用 factor_engine 的日 K 加载口径与项目既有的
T+1 开盘成交 / 剔除 ST·停牌·上市<60 日 等回测约束；风险/组合/成本/过拟合防御为新建模块。
组合层指标由本模块独立计算（与项目事件回测的 compute_metrics 口径一致：年化/夏普/索提诺/回撤/卡玛）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from .cost_model import CostConfig, portfolio_turnover_cost  # noqa: E402
from .cv_utils import ic_by_year, icir  # noqa: E402
from .factors import (  # noqa: E402
    CATEGORY_OF,
    FACTOR_METAS,
    MFConfig,
    build_factor_panel,
    compute_stock_raw,
    is_stock_code,
    load_lhb_trailing,
    parse_daily,
    zscore_panel,
)
from .portfolio import ERCConfig, enforce_turnover, solve_erc  # noqa: E402
from .risk_model import IndustryMap, RiskModel  # noqa: E402
from .valuation_data import ValuationConfig, ValuationProvider  # noqa: E402

PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
OUTPUT_DIR = ROOT / "data" / "plan_e"


# --------------------------------------------------------------------------
# 组合层指标（独立实现，口径与项目一致）
# --------------------------------------------------------------------------
def _portfolio_metrics(nav_rets: list[tuple]) -> dict:
    if not nav_rets:
        return {}
    s = pd.Series({d: r for d, r in nav_rets})
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()
    eq = (1 + s).cumprod()
    peak = eq.cummax()
    mdd = float(((eq - peak) / peak).min())
    mu, sd = s.mean(), s.std()
    sharpe = float(mu / sd * np.sqrt(252)) if sd and np.isfinite(sd) and sd > 0 else 0.0
    down = s[s < 0]
    dsd = down.std() if len(down) > 1 else 0.0
    sortino = float(mu / dsd * np.sqrt(252)) if dsd and np.isfinite(dsd) and dsd > 0 else 0.0
    span = (s.index.max() - s.index.min()).days
    n_trading = max(int(span * 252 / 365), len(s))
    ann = float(eq.iloc[-1] ** (252 / n_trading) - 1) if eq.iloc[-1] > 0 and n_trading > 0 else 0.0
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    return {"ann_ret": ann, "sharpe": sharpe, "sortino": sortino, "max_dd": mdd,
            "calmar": calmar, "n_days": len(s)}


# --------------------------------------------------------------------------
# 数据切片工具
# --------------------------------------------------------------------------
def _series_between(ser: pd.Series, start, end):
    if ser is None or ser.empty:
        return None
    sub = ser.loc[(ser.index >= start) & (ser.index < end)]
    return sub if not sub.empty else None


def _simulate_period(sel_codes, w, start, end, cache):
    """组合日收益 Series（首日 open→close，其余 close→close）。无数据返回 None。"""
    closes, opens = [], []
    for c in sel_codes:
        cs = _series_between(cache[c]["close"], start, end)
        os = _series_between(cache[c]["open"], start, end)
        if cs is not None and not cs.empty:
            closes.append(cs.rename(c))
            opens.append(os.rename(c) if os is not None else cs.rename(c))
    if len(closes) < 1:
        return None
    cdf = pd.concat(closes, axis=1).sort_index().ffill().bfill()
    odf = pd.concat(opens, axis=1).sort_index().ffill().bfill()
    if cdf.shape[0] < 2:
        return None
    ret = cdf.pct_change().fillna(0.0)
    first_open = odf.iloc[0].replace(0, np.nan)
    first_ret = (cdf.iloc[0] / first_open - 1).fillna(0.0)
    ret.iloc[0] = first_ret
    wv = pd.Series(w, index=sel_codes).reindex(cdf.columns).fillna(0.0).values
    agg = (ret * wv).sum(axis=1)
    return agg


def _exec_next(open_ser, after_date):
    fut = open_ser.loc[open_ser.index > after_date]
    if fut.empty:
        return None, None
    return fut.index[0], float(fut.iloc[0])


def _trailing_returns(codes, date, cache, window):
    cols = []
    for c in codes:
        ser = cache[c]["close"]
        sub = ser.loc[ser.index <= date].tail(window)
        if sub is None or sub.empty:
            continue
        r = sub.pct_change().dropna()
        if not r.empty:
            cols.append(r.rename(c))
    if not cols:
        return None
    return pd.concat(cols, axis=1).dropna()


def _forward_returns(codes, exec_date, cache, n):
    out = {}
    for c in codes:
        ser = cache[c]["close"]
        fut = ser.loc[ser.index > exec_date]
        if len(fut) <= n:
            continue
        o = cache[c]["open"].loc[cache[c]["open"].index > exec_date]
        if o.empty:
            continue
        out[c] = fut.iloc[n] / o.iloc[0] - 1
    return pd.Series(out) if out else None


def _rank_ic(a, b):
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    m = a.notna() & b.notna()
    if m.sum() < 5:
        return np.nan
    return float(pd.Series(a[m]).corr(pd.Series(b[m]), method="spearman"))


# --------------------------------------------------------------------------
# 合成
# --------------------------------------------------------------------------
def _composite(class_scores: dict[str, pd.Series], icir_w: dict[str, float]) -> pd.Series:
    cats = [c for c in class_scores if class_scores[c].notna().any()]
    if not cats:
        return pd.Series(dtype=float)
    if not icir_w or all(icir_w.get(c, 0) == 0 for c in cats):
        w = {c: 1.0 for c in cats}
    else:
        raw = {c: max(icir_w.get(c, 0.0), 0.01) for c in cats}
        tot = sum(raw.values()) or 1.0
        w = {c: v / tot for c, v in raw.items()}
    acc = None
    for c in cats:
        s = class_scores[c] * w[c]
        acc = s if acc is None else acc + s
    return acc


def _turnover_cost_frac(prev_codes, prev_w, sel_codes, w, nav_level, exec_date,
                        cost_cfg, cache):
    if not prev_codes or nav_level <= 0:
        return 0.0
    port_value = nav_level
    old_w = dict(zip(prev_codes, prev_w, strict=True)) if prev_w is not None and len(prev_w) == len(prev_codes) else {}
    new_w = dict(zip(sel_codes, w, strict=True))
    allc = set(prev_codes) | set(sel_codes)
    trades = []
    for c in allc:
        nw = new_w.get(c, 0.0)
        ow = old_w.get(c, 0.0)
        trade = abs(nw - ow) * port_value
        if trade <= 0:
            continue
        amt = cache[c]["amount"]
        m_amt = 0.0
        if amt is not None:
            sub = amt.loc[amt.index <= exec_date].tail(20)
            m_amt = float(sub.mean()) if not sub.empty else 0.0
        pr = min(trade / m_amt, 0.10) if m_amt > 0 else 0.0
        trades.append({"code": c, "notional_buy": trade if nw > ow else 0.0,
                       "notional_sell": trade if ow > nw else 0.0,
                       "participation_buy": pr if nw > ow else 0.0,
                       "participation_sell": pr if ow > nw else 0.0})
    if not trades:
        return 0.0
    cost = portfolio_turnover_cost(pd.DataFrame(trades), cost_cfg)
    return cost["total"] / port_value if port_value > 0 else 0.0


def run_mf_backtest(cfg: MFConfig) -> tuple[pd.DataFrame, dict]:
    np.random.seed(cfg.seed)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 股票池 + 缓存（close/open/amount + 因子原始序列）
    # 仅纳入沪深 A 股个股（剔除指数 / ETF / 债券 / 北交所），且排除迁移备份 .bak；
    # 先过滤再截断 limit，避免 limit 截断到文件排序靠前的指数/ETF/债券。
    files = [f for f in sorted(PARQUET_DIR.glob("*.parquet"))
             if ".bak" not in f.name and is_stock_code(f.stem)]
    if cfg.limit:
        files = files[: cfg.limit]
    cache: dict[str, dict] = {}
    for fp in files:
        code = fp.stem
        # 仅纳入沪深 A 股个股：剔除指数 / ETF / 债券 / 北交所微盘股
        if not is_stock_code(code):
            continue
        df = parse_daily(fp)
        if df is None or len(df) < cfg.ipo_min_days + 130:
            continue
        df = df.set_index("date")
        raw = compute_stock_raw(df.reset_index(), code)  # 返回以 date 为索引的因子序列
        cache[code] = {"close": df["close"], "open": df["open"],
                       "amount": df.get("amount"), "raw": raw}
    if not cache:
        return pd.DataFrame(), {}

    lhb = load_lhb_trailing()
    valp = ValuationProvider(ValuationConfig(mode=cfg.valuation_mode))
    risk = RiskModel(IndustryMap())
    erc_cfg = ERCConfig(max_weight=cfg.max_weight,
                        max_industry_exposure=cfg.max_industry_exposure,
                        max_weekly_turnover=cfg.max_weekly_turnover, seed=cfg.seed)
    cost_cfg = CostConfig(commission=cfg.commission, min_commission=cfg.min_commission,
                         stamp_tax=cfg.stamp_tax, slippage_main=cfg.slippage_main,
                         slippage_bj=cfg.slippage_bj, impact_gamma=cfg.impact_gamma)

    rbdates = pd.date_range(cfg.start, cfg.end, freq=cfg.rebalance_freq)
    rb_list = list(rbdates)

    prev_w = None
    prev_codes: list[str] = []
    nav_level = 1_000_000.0  # 以真实资金基数初始化，使绝对成本（最低佣金5元）占比合理
    nav_rets: list[tuple] = []
    ic_records: list[dict] = []
    factor_ic_hist: dict[str, list[float]] = {m.name: [] for m in FACTOR_METAS}
    class_ic_hist: dict[str, list[float]] = {}
    holdings_hist: list[tuple] = []
    turnover_hist: list[float] = []
    last_end = pd.Timestamp(cfg.end)

    for di, d in enumerate(rb_list):
        raw_map = {c: cache[c]["raw"] for c in cache}
        panel = build_factor_panel(raw_map, d, lhb, valp)
        if di == 2:
            print(f"[DEBUG] d={d} panel={len(panel)} keep_after_dropna={panel.dropna(subset=['mom_60d','zt_density_20d','fundflow_divergence_20d','val_proxy']).shape[0]}")
        if valp.mode != "proxy":
            panel = panel.drop(columns=["val_proxy"], errors="ignore")
        keep = panel.dropna(subset=["mom_60d", "zt_density_20d",
                                    "fundflow_divergence_20d", "val_proxy"]
                            if valp.mode == "proxy" else
                            ["mom_60d", "zt_density_20d", "fundflow_divergence_20d"])
        # 确保估值类至少有值
        if valp.mode == "proxy":
            keep = keep.dropna(subset=["val_proxy"])
        else:
            keep = keep.dropna(subset=["pe_ttm", "pb_mrq"], how="all")
        if keep.empty or len(keep) < max(cfg.top_n, 10):
            continue
        # IC 符号校正：样本内累计 IC 显著为负时翻转该因子方向（≥20 个样本且 IC<0）
        dir_override = {}
        for fac, hist in factor_ic_hist.items():
            if len(hist) >= 20 and np.mean(hist) < 0:
                dir_override[fac] = -1
        z = zscore_panel(keep, dir_override=dir_override)

        class_scores: dict[str, pd.Series] = {}
        for cat in set(CATEGORY_OF.values()):
            cols = [c for c in z.columns if CATEGORY_OF.get(c) == cat]
            valid = [c for c in cols if z[c].notna().any()]
            if valid:
                class_scores[cat] = z[valid].mean(axis=1)
        icir_w = {cat: icir(pd.Series(class_ic_hist.get(cat, []))) for cat in class_scores}
        comp = _composite(class_scores, icir_w)
        if comp.dropna().empty:
            continue
        comp = comp.dropna().sort_values(ascending=False)
        # 候选集：由综合得分选出的固定规模（3*top_n）真实「选中组合」。
        # 落选个股的权重将在周换手约束下逐周平滑归零，避免硬性清仓造成的结构高换手。
        sel_codes = list(comp.head(max(3 * cfg.top_n, 10)).index)

        # 风险协方差（候选集 trailing 60 日收益）
        rets = _trailing_returns(sel_codes, d, cache, 60)
        try:
            if rets is not None and rets.shape[1] >= 2 and rets.dropna().shape[0] >= 10:
                ac = list(rets.columns)
                sub_panel = keep.reindex(ac)
                cov = risk.estimate_cov(rets, sub_panel)
                w = solve_erc(cov, ac, {c: risk.industry.get(c) for c in ac},
                              prev_w, prev_codes, erc_cfg)
                sel_codes = ac
            else:
                w = None
        except Exception:
            w = None
        if w is None or len(w) != len(sel_codes):
            w = np.full(len(sel_codes), 1.0 / len(sel_codes))

        w = np.asarray(w, dtype=float)
        # 剔除权重极小的「浮尘」持仓（避免过多近似 0 权重股），保留 ERC 风险预算结果；
        # 不做 top_n 硬截断，使周换手约束真正作用于实际持仓、组合保持动态。
        keep_idx = np.where(w >= 1e-4)[0]
        if len(keep_idx) >= max(cfg.top_n, 10):
            sel_codes = [sel_codes[i] for i in keep_idx]
            w = w[keep_idx]
        w = w / w.sum() if w.sum() > 0 else w
        # 过滤/重归一化后再次强制周换手约束（凸组合保持权重/行业上限）
        w = enforce_turnover(w, sel_codes, prev_w, prev_codes, cfg.max_weekly_turnover)

        # 执行日（次一交易日开盘）
        exec_dates, exec_prices = {}, {}
        for c in sel_codes:
            ed, ep = _exec_next(cache[c]["open"], d)
            if ed is not None:
                exec_dates[c], exec_prices[c] = ed, ep
        exec_set = set(exec_prices.keys())
        pre_codes = sel_codes
        pre_w = w
        sel_codes = [c for c in pre_codes if c in exec_set]
        if not sel_codes:
            prev_w, prev_codes = w, sel_codes
            continue
        # 按代码对齐权重（过滤仅移除无执行价的个股，不改变顺序）
        wmap = dict(zip(pre_codes, pre_w, strict=True))
        w = np.array([wmap.get(c, 0.0) for c in sel_codes], dtype=float)
        w = w / w.sum() if w.sum() > 0 else w
        # 移除部分持仓后再强制一次换手约束（凸组合保持权重/行业上限）
        w = enforce_turnover(w, sel_codes, prev_w, prev_codes, cfg.max_weekly_turnover)
        # 真实换手（与 enforce 一致口径：prev 投影到当前 code，1-范数），用于报告与成本
        if prev_w is not None and prev_codes is not None and len(prev_w) == len(prev_codes):
            pm = dict(zip(prev_codes, prev_w, strict=True))
            pw = np.array([pm.get(c, 0.0) for c in sel_codes])
            psum = pw.sum()
            if psum > 0:
                pw = pw / psum
                turnover_hist.append(float(np.abs(w - pw).sum()))
        exec_date = max(exec_dates.values())
        if exec_date >= last_end:
            prev_w, prev_codes = w, sel_codes
            continue

        cost_frac = _turnover_cost_frac(prev_codes, prev_w, sel_codes, w,
                                       nav_level, exec_date, cost_cfg, cache)
        next_rb = rb_list[di + 1] if di + 1 < len(rb_list) else last_end
        period_ret = _simulate_period(sel_codes, w, exec_date, next_rb, cache)
        if period_ret is not None and len(period_ret) > 0:
            period_ret = period_ret.copy()
            period_ret.iloc[0] = period_ret.iloc[0] - cost_frac
            for dt, r in period_ret.items():
                nav_rets.append((str(dt)[:10], float(r)))
            nav_level *= float((1 + period_ret).prod())

        # IC 记录（composite / 类 / 因子 vs 前瞻 5 日收益）
        fwd = _forward_returns(sel_codes, exec_date, cache, 5)
        if fwd is not None and len(fwd.dropna()) > 5:
            for fac in z.columns:
                if fac not in factor_ic_hist:
                    continue
                sub = keep.reindex(sel_codes)[fac]
                ic = _rank_ic(sub, fwd)
                if np.isfinite(ic):
                    factor_ic_hist[fac].append(ic)
                    ic_records.append({"date": d, "factor": fac, "ic": ic})
            for cat, s in class_scores.items():
                c_ic = _rank_ic(s.reindex(sel_codes), fwd)
                if np.isfinite(c_ic):
                    class_ic_hist.setdefault(cat, []).append(c_ic)

        holdings_hist.append((str(exec_date)[:10], dict(zip(sel_codes, w, strict=True))))
        prev_w, prev_codes = w, sel_codes

    # ---- 指标 ----
    metrics = _portfolio_metrics(nav_rets)
    if ic_records:
        ic_df = pd.DataFrame(ic_records)
        metrics["ic_mean"] = float(ic_df["ic"].mean())
        metrics["icir_overall"] = icir(ic_df["ic"])
        metrics["ic_pos_rate"] = float((ic_df["ic"] > 0).mean())
    # 等权全市场基准（含成本前对比）
    bench = _benchmark_returns(cache, cfg.start, cfg.end)
    if bench is not None and len(bench) > 5:
        eq = (1 + bench).cumprod()
        n_t = max(int((bench.index.max() - bench.index.min()).days * 252 / 365), len(bench))
        metrics["bench_ann_ret"] = float(eq.iloc[-1] ** (252 / n_t) - 1) if eq.iloc[-1] > 0 else 0.0
        metrics["bench_max_dd"] = float(((eq - eq.cummax()) / eq.cummax()).min())
    metrics["avg_weekly_turnover"] = (sum(turnover_hist) / len(turnover_hist)) if turnover_hist else 0.0
    metrics["max_weekly_turnover"] = max(turnover_hist) if turnover_hist else 0.0
    metrics["n_rebalances"] = len(holdings_hist)
    metrics["universe_size"] = len(cache)
    metrics["valuation_mode"] = valp.mode
    metrics["yearly"] = _yearly_breakdown(nav_rets)
    if ic_records:
        metrics["ic_by_year"] = ic_by_year(ic_records)

    # ---- 输出 ----
    out_df = pd.DataFrame(nav_rets, columns=["date", "ret"])
    if not out_df.empty:
        out_df["nav"] = (1 + out_df["ret"]).cumprod()
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    csv_fp = OUTPUT_DIR / f"plan_e_backtest_mf_v1_{ts}.csv"
    out_df.to_csv(csv_fp, index=False, encoding="utf-8-sig")
    report = _build_report(metrics, holdings_hist, ic_records, metrics["yearly"],
                           cfg, valp, ts, metrics.get("ic_by_year"))
    md_fp = OUTPUT_DIR / f"plan_e_backtest_mf_v1_{ts}.md"
    md_fp.write_text(report, encoding="utf-8")
    (OUTPUT_DIR / "plan_e_backtest_mf_v1_latest.md").write_text(report, encoding="utf-8")
    return out_df, metrics


# --------------------------------------------------------------------------
# 报告 / 辅助
# --------------------------------------------------------------------------


def _yearly_breakdown(nav_rets):
    if not nav_rets:
        return {}
    s = pd.Series({d: r for d, r in nav_rets})
    s.index = pd.to_datetime(s.index)
    out = {}
    for yr, g in s.groupby(s.index.year):
        eq = (1 + g).cumprod()
        mdd = float(((eq - eq.cummax()) / eq.cummax()).min())
        ann = float(eq.iloc[-1] ** (252 / len(g)) - 1) if eq.iloc[-1] > 0 else 0.0
        out[str(yr)] = {"ann_ret": ann, "max_dd": mdd, "days": len(g)}
    return out


def _df_to_md(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "(无)"
    head = "| 年份 | " + " | ".join(map(str, df.columns)) + " |"
    sep = "|---|" + "|".join(["---:"] * len(df.columns)) + "|"
    lines = [head, sep]
    for idx, row in df.iterrows():
        lines.append(f"| {idx} | " + " | ".join(f"{v:.3f}" for v in row.values) + " |")
    return "\n".join(lines)


def _benchmark_returns(cache: dict, start, end) -> pd.Series | None:
    """等权全市场日收益基准（对比策略，不含成本），用于有效性边界判定。"""
    series = []
    for _, v in cache.items():
        s = v["close"]
        sub = s.loc[(s.index >= start) & (s.index <= end)]
        if sub is not None and len(sub) > 1:
            series.append(sub.pct_change().fillna(0.0))
    if not series:
        return None
    df = pd.concat(series, axis=1)
    return df.mean(axis=1, skipna=True)


def _build_report(metrics, holdings_hist, ic_records, yearly, cfg, valp, ts, ic_by_year_df=None):
    L = ["# 多因子组合回测报告（multifactor v1 · "
         f"{pd.Timestamp.now():%Y-%m-%d %H:%M}）\n", "## 一、核心指标\n",
         "| 指标 | 值 |\n|---|---|",
         f"| 年化收益 | {metrics.get('ann_ret',0)*100:.2f}% |",
         f"| 夏普 | {metrics.get('sharpe',0):.2f} |",
         f"| 索提诺 | {metrics.get('sortino',0):.2f} |",
         f"| 最大回撤 | {metrics.get('max_dd',0)*100:.2f}% |",
         f"| 卡玛 | {metrics.get('calmar',0):.2f} |",
         f"| 平均 IC | {metrics.get('ic_mean',0):.3f} |",
         f"| ICIR | {metrics.get('icir_overall',0):.3f} |",
         f"| IC 正向率 | {metrics.get('ic_pos_rate',0)*100:.1f}% |",
         f"| 平均周换手 | {metrics.get('avg_weekly_turnover',0)*100:.1f}% |",
         f"| 最大周换手 | {metrics.get('max_weekly_turnover',0)*100:.1f}% |",
         f"| 调仓次数 | {metrics.get('n_rebalances',0)} |",
         f"| 股票池 | {metrics.get('universe_size',0)} |",
         f"| 估值模式 | {valp.mode} |",
         f"| 基准(等权全市场)年化 | {metrics.get('bench_ann_ret',0)*100:.2f}% |",
         f"| 基准最大回撤 | {metrics.get('bench_max_dd',0)*100:.2f}% |",
         f"| 相对基准超额(年化) | {(metrics.get('ann_ret',0)-metrics.get('bench_ann_ret',0))*100:.2f}% |",
         "\n## 二、最近一期持仓（Top 权重）\n"]
    if holdings_hist:
        last_date, hw = holdings_hist[-1]
        L.append(f"（执行日 {last_date}）\n| 代码 | 权重 |\n|---|---:|")
        for c, w in sorted(hw.items(), key=lambda x: -x[1])[:15]:
            L.append(f"| {c} | {w*100:.2f}% |")
    L.append("\n## 三、逐年分解\n| 年份 | 年化 | 最大回撤 | 交易日 |\n|---|---:|---:|---:|")
    for yr, v in yearly.items():
        L.append(f"| {yr} | {v['ann_ret']*100:.2f}% | {v['max_dd']*100:.2f}% | {v['days']} |")
    # IC 分年衰减（过拟合防御观测）
    if ic_by_year_df is not None and not ic_by_year_df.empty:
        L.append("\n## 三-B、因子 IC 分年衰减（ICIR 加权依据）\n")
        L.append(_df_to_md(ic_by_year_df))
    L.append("\n## 四、风险提示\n")
    L.append("1. 本结果为历史回测口径，不承诺绝对收益；实盘滑点/冲击可能高于假设。")
    L.append("2. 估值/基本面 real 数据经 akshare 下载，PE/PB 覆盖约 2021-10 起，早期以 proxy 占位。")
    L.append("3. 情绪（龙虎榜）数据仅覆盖 2025-2026，窗口外按缺失处理，可能削弱情绪因子稳定性。")
    L.append("4. 过拟合残留：尽管采用 ICIR 加权 / Walk-Forward / 合成正则化，因子 IC 样本外可能衰减 30-50%。")
    L.append("5. 组合换手受 ≤40% 周预算约束，极端行情下可能无法充分再平衡。")
    L.append("\n## 五、下一步建议\n")
    L.append("- 待 data/valuation 全量下载完成后以 valuation_mode=real 重跑，验证估值因子真实贡献。")
    L.append("- 用 purged+embargo CV 对因子合成做样本外稳健性复核。")
    L.append("- 引入 LightGBM 截面排序作为对照增强（强制 purged CV）。")
    L.append(f"\n---\n生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}\n"
             f"生成者：quant/multifactor/engine.py\n模型版本：multifactor v1\n"
             f"数据日期：{pd.Timestamp.now():%Y-%m-%d}\n")
    return "\n".join(L)
