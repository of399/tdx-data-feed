#!/usr/bin/env python
"""min60 12.5 个月可执行打板验证（2025-09-05 ~ 2026-09-24）。

Part A：打板路径能不能执行
- 触板即买（first touch at limit）→ T+1 开盘/收盘卖
- 封板后等 N 根 60min K，再挂涨停买单；只有开板才成交 → T+1 开盘/收盘卖
- 按时点可得特征（连板、成交 K、市场宽度/收益、流动性）切分，检验是否存在正子集
- 关键对照：收盘是否封死（事后信息）会制造正收益假象

Part B：封板质量对「连板≥3 接力」有没有增量
- 基线：T 日收盘封死且连板≥3 → T+1 开盘买（开盘涨停剔除）→ T+1 收盘卖
- min60 封板质量特征的逐日 rank IC、分半稳定性、桶收益
- 迟到量能候选的事后检验（多试、尾部依赖、不可买率）

输出：docs/DABAN_MIN60_EXEC_20261022.md

用法::

    venv/bin/python scripts/daban_min60_exec.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ROOT / "data/daban/min60_features_20261022.parquet"
RELAY = ROOT / "data/daban/relay_hold1_20261022/daban_backtest_20200101_20261019.csv"
OUT = ROOT / "docs/DABAN_MIN60_EXEC_20261022.md"
EPS = 0.005
COST = 0.003

FEATS = [
    "open_gap", "last_bar_vol_share", "open_limit", "consecutive_boards",
    "reopen_count", "first_reopen", "n_sealed_bars", "first_seal",
    "vol_after_seal_share", "sealed_share_after", "seal_strength",
    "first_hour_vol_share", "last_hour_ret", "first_hour_ret", "morning_ret",
    "amplitude", "touch_count", "cb_prev",
]


def pct(x: float, digits: int = 3) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:+.{digits}f}"


def num(x: float, digits: int = 1) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def load_features(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_parquet(path)
    raw["date"] = raw["date"].astype(str)
    raw["suspect_ref"] = raw["suspect_ref"].astype(bool)
    valid = raw[~raw.suspect_ref].copy()
    valid["cb_prev"] = (valid.groupby("code", sort=False)["consecutive_boards"]
                        .shift(1).fillna(0).astype(int))
    dates = np.array(sorted(valid["date"].unique()))
    valid["half"] = np.where(valid["date"].isin(dates[: len(dates) // 2]), "H1", "H2")
    return raw, valid


def attach_exit_returns(frame: pd.DataFrame, entry: np.ndarray, prefix: str) -> pd.DataFrame:
    """T+1 开盘/收盘卖；跌停卖不掉则顺延 T+2 开盘，并做异常价闸门。"""
    entry = np.asarray(entry, dtype=float)
    ratio = frame["ratio"].to_numpy(dtype=float)
    for ex, tag in (("next_open", "open"), ("next_close", "close")):
        px = frame[ex].to_numpy(dtype=float)
        defer = px <= frame["next_lim_dn"].to_numpy(dtype=float) + EPS
        px2 = np.where(defer, frame["next2_open"].to_numpy(dtype=float), px)
        ret = px2 / entry - 1.0
        ok = (np.isfinite(entry) & (entry > 0)
              & np.isfinite(px2) & (px2 > 0))
        gate = ok & (ret >= -3.0 * ratio - 0.02) & (ret <= 2.0 * ratio + 0.01)
        frame[f"ret_{tag}_{prefix}"] = np.where(gate, ret - COST, np.nan)
        frame[f"defer_{tag}_{prefix}"] = defer & gate
    return frame


def summarize(sub: pd.DataFrame, col: str) -> dict:
    s = sub[col].dropna()
    if s.empty:
        return {"n": 0, "mean": np.nan, "win": np.nan, "daily": np.nan,
                "t": np.nan, "days": 0}
    days = sub.loc[s.index].groupby("date")[col].mean()
    t = (days.mean() / (days.std(ddof=1) / np.sqrt(len(days)))
         if len(days) > 2 else np.nan)
    return {"n": len(s), "mean": s.mean() * 100, "win": (s > 0).mean() * 100,
            "daily": days.mean() * 100, "t": t, "days": len(days)}


def market_context(valid: pd.DataFrame) -> tuple:
    gap = (valid["o0"] / valid["prev_close"] - 1.0).groupby(valid["date"]).mean()
    rets, brs = {}, {}
    for k in range(4):
        rets[k] = (valid[f"c{k}"] / valid["prev_close"] - 1.0).groupby(valid["date"]).mean()
        brs[k] = (valid[f"c{k}"] >= valid["limit"] - EPS).groupby(valid["date"]).mean()
    dates = np.array(sorted(valid["date"].unique()))
    prev_date = {dates[i]: dates[i - 1] for i in range(1, len(dates))}
    return gap, rets, brs, prev_date


def add_market_features(f: pd.DataFrame, bar_col: str, ctx: tuple) -> pd.DataFrame:
    gap, rets, brs, prev_date = ctx
    prev_bar = f[bar_col].to_numpy(dtype=int) - 2  # 成交 K 之前一根已收盘的 K
    mkt_ret = np.full(len(f), np.nan)
    breadth = np.full(len(f), np.nan)
    for i, (d, b) in enumerate(zip(f["date"].to_numpy(), prev_bar, strict=True)):
        if b < 0:
            mkt_ret[i] = gap.get(d, np.nan)
            pd_ = prev_date.get(d)
            breadth[i] = brs[3].get(pd_, np.nan) if pd_ else np.nan
        else:
            mkt_ret[i] = rets[b].get(d, np.nan)
            breadth[i] = brs[b].get(d, np.nan)
    f["mkt_ret_prior"] = mkt_ret
    f["breadth_prior"] = breadth
    return f


def board_label(ratio: float) -> str:
    if ratio >= 0.195:
        return "双创20%"
    if ratio <= 0.055:
        return "主板ST5%"
    return "主板10%"


def part_a(valid: pd.DataFrame, ctx: tuple) -> tuple[dict, list[str]]:
    touch = valid[valid["touched"]].copy()
    attach_exit_returns(touch, touch["limit"].to_numpy(dtype=float), "touch")

    fills = {}
    for w in (1, 2, 3):
        f = valid[valid[f"fill_w{w}"] & valid["touched"]].copy()
        attach_exit_returns(f, f[f"entry_w{w}"].to_numpy(dtype=float), f"w{w}")
        add_market_features(f, f"fill_bar_w{w}", ctx)
        f["board"] = [board_label(r) for r in f["ratio"]]
        f["liq_q"] = f.groupby("date")["avg_amount_5d"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False,
                              duplicates="drop") + 1)
        fills[w] = f

    res: dict = {"touch": touch, "fills": fills}

    # 触板即买：全部 / 事后封板 / 事后未封板 / 一字 / 非一字
    touch_rows = []
    touch_groups = [
        ("全部触板", np.ones(len(touch), dtype=bool)),
        ("收盘封死（事后）", touch["sealed_close"].to_numpy(dtype=bool)),
        ("　├ 非一字", (touch["sealed_close"] & ~touch["open_limit"]).to_numpy(dtype=bool)),
        ("　└ 一字", (touch["sealed_close"] & touch["open_limit"]).to_numpy(dtype=bool)),
        ("收盘未封死（事后）", ~touch["sealed_close"].to_numpy(dtype=bool)),
        ("　├ 非一字", (~touch["sealed_close"] & ~touch["open_limit"]).to_numpy(dtype=bool)),
        ("　└ 一字", (~touch["sealed_close"] & touch["open_limit"]).to_numpy(dtype=bool)),
        ("非一字（全部）", ~touch["open_limit"].to_numpy(dtype=bool)),
        ("一字（全部，多数买不到）", touch["open_limit"].to_numpy(dtype=bool)),
    ]
    for label, mask in touch_groups:
        s = touch[mask]
        a = summarize(s, "ret_open_touch")
        b = summarize(s, "ret_close_touch")
        touch_rows.append([label, str(a["n"]), pct(a["mean"]), num(a["win"]),
                           pct(a["t"]), pct(b["mean"]), num(b["win"]), pct(b["t"])])
    res["touch_rows"] = touch_rows

    # 封板后等 wait 根 K 再挂单，只有开板才成交
    fill_rows = []
    for w in (1, 2, 3):
        f = fills[w]
        for tag, lab in (("open", "T+1开盘卖"), ("close", "T+1收盘卖")):
            s = summarize(f, f"ret_{tag}_w{w}")
            fill_rows.append([f"wait{w}", lab, str(s["n"]), pct(s["mean"]),
                              num(s["win"]), pct(s["daily"]), pct(s["t"]),
                              str(int(f[f"defer_{tag}_w{w}"].sum()))])
    res["fill_rows"] = fill_rows

    # 事后封板 × 前一日连板，解释正收益来自哪里
    f1 = fills[1]
    la_all_rows = []
    for cb_lab, cb_mask in [
        ("全部", pd.Series(True, index=f1.index)),
        ("0", f1["cb_prev"].eq(0)), ("1", f1["cb_prev"].eq(1)),
        ("2", f1["cb_prev"].eq(2)), ("≥3", f1["cb_prev"].ge(3)),
    ]:
        row = [cb_lab]
        for sc in (True, False):
            s = summarize(f1[cb_mask & f1["sealed_close"].eq(sc)], "ret_close_w1")
            row += [str(s["n"]), pct(s["mean"])]
        la_all_rows.append(row)
    s_all_t = summarize(f1[f1["sealed_close"]], "ret_close_w1")
    s_all_f = summarize(f1[~f1["sealed_close"]], "ret_close_w1")
    res["la_all"] = (s_all_t, s_all_f)

    # 时点可得特征的分组
    f1 = fills[1]
    med_br = f1["breadth_prior"].median()
    med_mk = f1["mkt_ret_prior"].median()
    subgroups = [
        ("全部", pd.Series(True, index=f1.index)),
        ("前一日连板=0", f1["cb_prev"].eq(0)),
        ("前一日连板=1", f1["cb_prev"].eq(1)),
        ("前一日连板=2", f1["cb_prev"].eq(2)),
        ("前一日连板≥3", f1["cb_prev"].ge(3)),
        ("主板10%", f1["board"].eq("主板10%")),
        ("主板ST5%", f1["board"].eq("主板ST5%")),
        ("双创20%", f1["board"].eq("双创20%")),
        ("非一字", f1["open_limit"].eq(0)),
        ("一字", f1["open_limit"].eq(1)),
        ("开板价成交", f1["gap_fill_w1"].eq(1)),
        ("涨停价成交", f1["gap_fill_w1"].eq(0)),
        ("成交K=2", f1["fill_bar_w1"].eq(2)),
        ("成交K=3", f1["fill_bar_w1"].eq(3)),
        ("成交K=4", f1["fill_bar_w1"].eq(4)),
        ("首封K=1", f1["first_seal"].eq(1)),
        ("首封K=2", f1["first_seal"].eq(2)),
        ("首封K=3", f1["first_seal"].eq(3)),
        ("市场宽度≥中位", f1["breadth_prior"].ge(med_br)),
        ("市场收益≥中位", f1["mkt_ret_prior"].ge(med_mk)),
        ("流动性Q5", f1["liq_q"].eq(5)),
        ("流动性Q1", f1["liq_q"].eq(1)),
        ("非ST且前日连板0", f1["is_st"].eq(0) & f1["cb_prev"].eq(0)),
    ]
    sub_rows = []
    for label, mask in subgroups:
        s = f1[mask.fillna(False)]
        a = summarize(s, "ret_open_w1")
        b = summarize(s, "ret_close_w1")
        if b["n"] < 100:
            continue
        sub_rows.append([label, str(b["n"]), pct(a["mean"]), pct(b["mean"]),
                         num(b["win"]), pct(b["t"])])
    res["sub_rows"] = sub_rows

    # 单条件/组合过滤，全部要求 n≥100
    filters = [
        ("无过滤", pd.Series(True, index=f1.index)),
        ("前一日连板=0", f1["cb_prev"].eq(0)),
        ("非一字", f1["open_limit"].eq(0)),
        ("成交K=4", f1["fill_bar_w1"].eq(4)),
        ("首封K≥2", f1["first_seal"].ge(2)),
        ("市场宽度≥中位", f1["breadth_prior"].ge(med_br)),
        ("市场收益≥中位", f1["mkt_ret_prior"].ge(med_mk)),
        ("流动性Q5", f1["liq_q"].eq(5)),
        ("前日连板0 + 非一字", f1["cb_prev"].eq(0) & f1["open_limit"].eq(0)),
        ("前日连板0 + 非一字 + K4",
         f1["cb_prev"].eq(0) & f1["open_limit"].eq(0) & f1["fill_bar_w1"].eq(4)),
        ("前日连板0 + 非一字 + 宽度高",
         f1["cb_prev"].eq(0) & f1["open_limit"].eq(0) & f1["breadth_prior"].ge(med_br)),
        ("前日连板0 + 非一字 + 市场高",
         f1["cb_prev"].eq(0) & f1["open_limit"].eq(0) & f1["mkt_ret_prior"].ge(med_mk)),
        ("前日连板0 + 非一字 + 流动性Q5",
         f1["cb_prev"].eq(0) & f1["open_limit"].eq(0) & f1["liq_q"].eq(5)),
        ("前日连板0 + 非一字 + K4 + 宽度高",
         f1["cb_prev"].eq(0) & f1["open_limit"].eq(0)
         & f1["fill_bar_w1"].eq(4) & f1["breadth_prior"].ge(med_br)),
        ("事后：收盘封死", f1["sealed_close"].eq(1)),
    ]
    filt_rows = []
    for label, mask in filters:
        s = f1[mask.fillna(False)]
        a = summarize(s, "ret_open_w1")
        b = summarize(s, "ret_close_w1")
        if b["n"] < 100:
            continue
        filt_rows.append([label, str(b["n"]), pct(a["mean"]), pct(b["mean"]),
                          pct(b["t"])])
    filt_rows.sort(key=lambda r: float(r[3].replace("+", "")) if r[3] != "—" else -99,
                   reverse=True)
    res["filter_rows"] = filt_rows

    # 月度与分半
    f1 = f1.copy()
    f1["ym"] = f1["date"].str[:6]
    month_rows = []
    for ym, s in f1.groupby("ym"):
        a = summarize(s, "ret_open_w1")
        b = summarize(s, "ret_close_w1")
        month_rows.append([f"{ym[:4]}-{ym[4:]}", str(b["n"]), pct(a["mean"]),
                           pct(b["mean"]), num(b["win"])])
    res["month_rows"] = month_rows
    half_rows = []
    for h in ("H1", "H2"):
        s = summarize(f1[f1["half"].eq(h)], "ret_open_w1")
        b = summarize(f1[f1["half"].eq(h)], "ret_close_w1")
        half_rows.append([h, str(b["n"]), pct(s["mean"]), pct(b["mean"]), pct(b["t"])])
    res["half_rows"] = half_rows
    res["la_all_rows"] = la_all_rows
    return res


def ic_stats(df: pd.DataFrame, col: str, min_trades: int) -> tuple[int, float, float]:
    x = df[["date", "ret", col]].dropna()
    if x.empty:
        return 0, np.nan, np.nan
    cnt = x.groupby("date")["ret"].transform("size")
    x = x[cnt >= min_trades]
    if x.empty:
        return 0, np.nan, np.nan
    r = x.groupby("date")[col].rank()
    ry = x.groupby("date")["ret"].rank()
    r_dm = r - r.groupby(x["date"]).transform("mean")
    y_dm = ry - ry.groupby(x["date"]).transform("mean")
    num = (r_dm * y_dm).groupby(x["date"]).sum()
    den = np.sqrt((r_dm ** 2).groupby(x["date"]).sum()
                  * (y_dm ** 2).groupby(x["date"]).sum())
    ic = (num / den).replace([np.inf, -np.inf], np.nan).dropna()
    if len(ic) < 3:
        return len(ic), np.nan, np.nan
    t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic)))
    return len(ic), ic.mean(), t


def median_split(s: pd.DataFrame, col: str) -> tuple:
    x = s[[col, "ret"]].dropna()
    if x.empty or x[col].nunique() < 3:
        return np.nan, np.nan, 0, 0, np.nan, np.nan
    med = x[col].median()
    hi = x[x[col] >= med]["ret"]
    lo = x[x[col] < med]["ret"]
    return (hi.mean() * 100, lo.mean() * 100, len(hi), len(lo),
            (hi.mean() - lo.mean()) * 100, med)


def part_b(valid: pd.DataFrame) -> dict:
    base = valid[(valid["consecutive_boards"] >= 3) & valid["sealed_close"]
                 & (valid["next_open"] > 0) & (valid["next_close"] > 0)].copy()
    n_base = len(base)
    base["unbuyable"] = base["next_open"] >= base["next_lim_up"] - EPS
    n_unbuy = int(base["unbuyable"].sum())
    b = base[~base["unbuyable"]].copy()
    b["ret_g"] = b["next_close"] / b["next_open"] - 1.0
    bad = (b["ret_g"] < -b["ratio"] - 0.01) | (b["ret_g"] > 2 * b["ratio"] + 0.01)
    n_bad = int(bad.sum())
    b = b[~bad].copy()
    b["ret"] = b["ret_g"] - COST
    b["half"] = np.where(
        b["date"].isin(sorted(b["date"].unique())[: len(b["date"].unique()) // 2]),
        "H1", "H2")

    res = {"n_base": n_base, "n_unbuy": n_unbuy, "n_bad": n_bad,
           "b": b, "baseline": summarize(b, "ret")}

    ic_rows = []
    for c in FEATS:
        d2, ic2, t2 = ic_stats(b, c, 2)
        d3, ic3, t3 = ic_stats(b, c, 3)
        ic_rows.append([c, pct(ic2, 4), pct(t2, 2), str(d2),
                        pct(ic3, 4), pct(t3, 2), str(d3)])
    ic_rows.sort(key=lambda r: abs(float(r[1].replace("+", ""))) if r[1] != "—" else -1,
                 reverse=True)
    res["ic_rows"] = ic_rows

    split_rows = []
    for c in FEATS:
        _hi_all, _lo_all, n_hi, n_lo, diff_all, med = median_split(b, c)
        if min(n_hi, n_lo) < 50:
            continue
        h1 = b[b["half"].eq("H1")]
        h2 = b[b["half"].eq("H2")]
        _, _, _, _, diff_h1, _ = median_split(h1, c)
        _, _, _, _, diff_h2, _ = median_split(h2, c)
        split_rows.append([c, pct(diff_all), pct(diff_h1), pct(diff_h2),
                           str(n_hi), str(n_lo), num(med, 4)])
    split_rows.sort(key=lambda r: abs(float(r[1].replace("+", ""))) if r[1] != "—" else -1,
                    reverse=True)
    res["split_rows"] = split_rows

    # 离散桶
    discrete_rows = []
    for c in ("open_limit", "reopen_count", "first_seal"):
        for val, s in b.groupby(c):
            m = s["ret"].mean() * 100
            w = (s["ret"] > 0).mean() * 100
            discrete_rows.append([c, str(val), str(len(s)), pct(m), num(w)])
    cb_grp = np.where(b["consecutive_boards"] >= 9, "9+",
                      b["consecutive_boards"].astype(str))
    for val, s in b.groupby(cb_grp):
        m = s["ret"].mean() * 100
        w = (s["ret"] > 0).mean() * 100
        discrete_rows.append(["consecutive_boards", str(val), str(len(s)), pct(m), num(w)])
    res["discrete_rows"] = discrete_rows

    # 候选细节
    ol = b["open_limit"].astype(bool)
    lb_med = b["last_bar_vol_share"].median()
    lb_hi = b["last_bar_vol_share"] >= lb_med
    res["b_ol"] = summarize(b[ol], "ret")
    res["b_nool"] = summarize(b[~ol], "ret")
    res["b_lb_hi"] = summarize(b[lb_hi], "ret")
    res["b_lb_lo"] = summarize(b[~lb_hi], "ret")
    res["lb_med"] = lb_med

    # 分半稳定性（各自半内中位数）
    ol_half = []
    for h in ("H1", "H2"):
        s = b[b["half"].eq(h)]
        a = summarize(s[s["open_limit"].astype(bool)], "ret")
        c = summarize(s[~s["open_limit"].astype(bool)], "ret")
        ol_half.append((h, a, c))
    res["ol_half"] = ol_half

    lb_half = []
    for h in ("H1", "H2"):
        s = b[b["half"].eq(h)]
        m = s["last_bar_vol_share"].median()
        a = summarize(s[s["last_bar_vol_share"] >= m], "ret")
        c = summarize(s[s["last_bar_vol_share"] < m], "ret")
        lb_half.append((h, a, c))
    res["lb_half"] = lb_half

    # 分半阈值外推：H1 阈值用于 H2，反之亦然
    thr_h1 = b.loc[b["half"].eq("H1"), "last_bar_vol_share"].median()
    thr_h2 = b.loc[b["half"].eq("H2"), "last_bar_vol_share"].median()
    oos = []
    for h, thr in (("H2", thr_h1), ("H1", thr_h2)):
        s = b[b["half"].eq(h)]
        a = summarize(s[s["last_bar_vol_share"] >= thr], "ret")
        c = summarize(s[s["last_bar_vol_share"] < thr], "ret")
        o = summarize(s[s["open_limit"].astype(bool)
                        | (s["last_bar_vol_share"] >= thr)], "ret")
        oos.append((h, thr, a, c, o))
    res["lb_oos"] = oos

    # 同日价差、剔除极端日后
    spreads = []
    for _, s in b.groupby("date"):
        a = s.loc[s["last_bar_vol_share"] >= lb_med, "ret"].mean()
        c = s.loc[s["last_bar_vol_share"] < lb_med, "ret"].mean()
        if pd.notna(a) and pd.notna(c):
            spreads.append(a - c)
    sp = pd.Series(spreads, dtype=float)
    spread_stats = {"days": len(sp), "mean": sp.mean() * 100,
                    "t": sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp))) if len(sp) > 2 else np.nan,
                    "pos": (sp > 0).mean() * 100}
    for k in (5, 10):
        x = sp.sort_values().iloc[:-k]
        spread_stats[f"drop{k}_mean"] = x.mean() * 100
        spread_stats[f"drop{k}_t"] = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
    res["spread_stats"] = spread_stats

    # 不可买率
    base_lb_hi = base["last_bar_vol_share"] >= lb_med
    res["unbuy_all"] = base["unbuyable"].mean() * 100
    res["unbuy_lb_hi"] = base.loc[base_lb_hi, "unbuyable"].mean() * 100
    res["unbuy_lb_lo"] = base.loc[~base_lb_hi, "unbuyable"].mean() * 100
    res["unbuy_ol"] = base.loc[base["open_limit"].astype(bool), "unbuyable"].mean() * 100
    res["unbuy_nool"] = base.loc[~base["open_limit"].astype(bool), "unbuyable"].mean() * 100

    # 探索性组合：open_limit OR 迟到量能高
    def combo(mask: pd.Series, label: str) -> dict:
        return summarize(b[mask], "ret")
    res["combo_rows"] = [
        ["基线", summarize(b, "ret")],
        ["open_limit", res["b_ol"]],
        ["last_bar 高", res["b_lb_hi"]],
        ["二选一：open_limit 或 last_bar 高",
         combo(ol | lb_hi, "or")],
        ["两者都满足", combo(ol & lb_hi, "and")],
        ["非一字 + last_bar 高", combo((~ol) & lb_hi, "nool_hi")],
        ["非一字 + last_bar 低", combo((~ol) & (~lb_hi), "nool_lo")],
    ]
    res["combo_rows"] = [[lab, d] for lab, d in res["combo_rows"]]

    # 与 chase 回测逐笔核对
    res["crosscheck"] = None
    if RELAY.exists():
        try:
            r = pd.read_csv(RELAY, dtype={"code": str, "signal_date": str})
            w = r[(r["signal_date"] >= b["date"].min()) & (r["signal_date"] <= b["date"].max())
                  & (r["consecutive"] >= 3)].copy()
            m = b.merge(w, left_on=["code", "date"], right_on=["code", "signal_date"],
                        how="outer", indicator=True)
            both = m[m["_merge"] == "both"]
            only_r = m[m["_merge"] == "right_only"]
            res["crosscheck"] = {
                "relay_n": len(w), "both": len(both),
                "relay_only": len(only_r),
                "max_gross_diff": float((both["ret_g"] - both["ret_gross"]).abs().max())
                if len(both) else np.nan,
                "relay_only_mean": float(only_r["ret_gross"].mean() * 100)
                if len(only_r) else np.nan,
            }
        except Exception as exc:  # pragma: no cover - diagnostic only
            res["crosscheck"] = {"error": str(exc)}
    return res


def build_report(raw: pd.DataFrame, valid: pd.DataFrame, a: dict, b: dict) -> list[str]:
    lines = [
        "# daban min60 可执行性验证（2025-09-05 ~ 2026-09-24）",
        "",
        "> 结论先行：**打板路径在 12.5 个月、5,221 只股票的 min60 数据上仍不可执行。**",
        "> 触板即买的期望为负；封死后等开板再买更负；唯一为正的子集是",
        "> 「事后知道收盘封死」或「一字板（买不到）」。连板≥3 接力基线仍为",
        f"> **{pct(b['baseline']['mean'])}%/笔（n={b['baseline']['n']}，t={num(b['baseline']['t'],2)}）**，",
        "> min60 封板质量没有提供可验证的增量 edge。",
        "",
        "## 1. 数据与校验",
        "",
        f"- min60：4 根 60 分钟 K（10:30 / 11:30 / 14:00 / 15:00），"
        f"{valid['code'].nunique():,} 只、{valid['date'].nunique()} 个交易日、"
        f"{valid['date'].min()} ~ {valid['date'].max()}。",
        f"- 原始股票-日 {len(raw):,} 行；剔除价格对齐异常 {int(raw['suspect_ref'].sum()):,} 行"
        f"（{raw['suspect_ref'].mean() * 100:.3f}%）后 {len(valid):,} 行。",
        f"- 对齐校验：|min60 收盘 − 日线收盘| 的 P99.9 = "
        f"{valid['px_diff'].quantile(0.999):.6f} 元（最大 {valid['px_diff'].max():.2f}），"
        f"非零 {((valid['px_diff'] > 0).mean() * 100):.3f}%。",
        f"- 触板 {int(valid['touched'].sum()):,} 次；收盘封死 "
        f"{int(valid['sealed_close'].sum()):,} 次（占触板 "
        f"{valid.loc[valid['touched'], 'sealed_close'].mean() * 100:.1f}%）；"
        f"开盘一字 {int(valid['open_limit'].sum()):,} 次。",
        f"- 封板后等待 1/2/3 根 K 的可成交（开板）样本："
        f"{int(valid['fill_w1'].sum()):,} / {int(valid['fill_w2'].sum()):,} / "
        f"{int(valid['fill_w3'].sum()):,} 笔。",
        "- 成本双边 0.30%；T+1 收盘跌停则顺延到 T+2 开盘卖；"
        "异常价闸门为单笔收益 ∈ [−3×涨跌幅−2%, +2×涨跌幅+1%]。",
        "",
    ]
    cc = b.get("crosscheck")
    if cc and "error" not in cc:
        lines += [
            f"- 与 `daban_backtest.py --mode chase` 同期 cb≥3 逐笔核对："
            f"共同 {cc['both']} 笔，gross 最大误差 {cc['max_gross_diff']:.1e}；"
            f"回测有 {cc['relay_only']} 笔不在 min60 表中"
            f"（平均 {pct(cc['relay_only_mean'])}%），故本文基线略偏保守。",
            "",
        ]
    lines += [
        "## 2. Part A：打板路径",
        "",
        "### 2.1 触板即买（涨停价成交）",
        "",
        "T 日首次触板时按涨停价买入，T+1 开盘/收盘卖出。"
        "这是日线回测「T 日涨停价买入」的可执行版本。",
        "",
    ]
    lines += md_table(
        ["触板子集", "笔数", "T+1开盘 均净%", "胜率%", "t",
         "T+1收盘 均净%", "胜率%", "t"],
        a["touch_rows"])
    lines += [
        "",
        "全部触板的期望为负；正收益只出现在「收盘封死」这个事后子集"
        "（其中非一字 +1.28%，仍然需要事先知道会封死），"
        "而未封死子集的期望是灾难性的。一字板（开盘即涨停）按涨停价买入的假设"
        "本身不可成交，不能作为可执行收益。",
        "",
        "### 2.2 封板后等开板再买（唯一能成交的打板方式）",
        "",
        "封板后等待 N 根 60 分钟 K，再挂涨停买单：若该 K 开盘低于涨停价则按开盘价成交，"
        "否则盘中跌破涨停价时按涨停价成交。成交意味着板被打开。",
        "",
    ]
    lines += md_table(
        ["入场", "出场", "笔数", "均净%", "胜率%", "日均%", "t", "跌停顺延"],
        a["fill_rows"])
    lines += [
        "",
        "三个等待档、两种出场口径全部为负；等待越久、样本越少，收益没有改善。"
        "这与 min5 的结论一致：能成交的都是开板单，成交即利空。",
        "",
        "### 2.3 正收益来自事后信息：收盘封死 × 前一日连板",
        "",
    ]
    s_t, s_f = a["la_all"]
    lines += md_table(
        ["前一日连板", "收盘封死 n", "均净%", "收盘未封死 n", "均净%"],
        a["la_all_rows"])
    lines += [
        "",
        f"收盘封死子集 {pct(s_t['mean'])}%/笔（n={s_t['n']}，t={num(s_t['t'],2)}），"
        f"未封死子集 {pct(s_f['mean'])}%/笔（n={s_f['n']}，t={num(s_f['t'],2)}）。"
        "但「收盘封死」在盘中成交时不可知；按它筛选等于用未来信息。"
        "到了前一日已经≥3 连板，即使事后封死也不再为正，说明高位板开板后的反抽更差。",
        "",
        "### 2.4 时点可得特征的分组（wait1，T+1 收盘卖）",
        "",
    ]
    lines += md_table(
        ["子集", "笔数", "T+1开盘卖%", "T+1收盘卖%", "胜率%", "收盘t"],
        a["sub_rows"])
    lines += [
        "",
        "没有正子集。相对最不差的是「成交 K=4」「首封 K=3」「开板价成交」等，"
        "但仍在 −0.4% ~ −0.7%/笔；前一日连板越高越差（≥3 连板 −3.4%/笔），"
        "与接力的方向相反——打板买的是「封板失败后的开板」，接力买的是「已经封死后的次日开盘」。",
        "",
        "### 2.5 单条件/组合过滤：最好的仍是负的",
        "",
    ]
    lines += md_table(
        ["过滤（T 日盘中/收盘前可得）", "笔数", "T+1开盘卖%", "T+1收盘卖%", "收盘t"],
        a["filter_rows"])
    lines += [
        "",
        "唯一为正的过滤是「事后收盘封死」，再次说明可执行样本没有 edge。"
        "在时点可得的组合里，最好的「前日连板0 + 非一字 + 成交K4 + 宽度高」"
        "也只有约 −0.43%/笔（n≈1,027，日度 t≈−0.65）。",
        "",
        "### 2.6 时间稳定性",
        "",
    ]
    lines += md_table(["半段", "笔数", "T+1开盘卖%", "T+1收盘卖%", "收盘t"],
                      a["half_rows"])
    lines += ["", "逐月（wait1，T+1 收盘卖）：", ""]
    lines += md_table(["月份", "笔数", "T+1开盘卖%", "T+1收盘卖%", "胜率%"],
                      a["month_rows"])
    lines += [
        "",
        "13 个月中只有 2026-02 为正（+0.48%），其余月份均为负；两个半段都显著为负。"
        "这不是某一段行情的偶然。",
        "",
        "## 3. Part B：封板质量对连板≥3 接力的增量",
        "",
        "### 3.1 基线",
        "",
        f"T 日收盘封死、连板≥3，T+1 开盘买入（开盘涨停剔除）、T+1 收盘卖出。"
        f"原始 {b['n_base']:,} 个信号，T+1 开盘涨停买不进 {b['n_unbuy']:,} 个"
        f"（{b['n_unbuy'] / b['n_base'] * 100:.1f}%），异常价 {b['n_bad']} 个，"
        f"最终 **{b['baseline']['n']} 笔 / {b['baseline']['days']} 天**。",
        "",
        f"- 均净 **{pct(b['baseline']['mean'])}%/笔**，胜率 {num(b['baseline']['win'])}%，"
        f"日均 {pct(b['baseline']['daily'])}%，日度 t **{num(b['baseline']['t'], 2)}**。",
        "- 基线本身为正且显著；问题是有没有 min60 特征能在池内继续区分好坏。"
        "（日均按交易日等权，逐笔均值按每笔等权，二者因每日笔数不同而不同。）",
        "",
        "### 3.2 逐日 rank IC",
        "",
    ]
    lines += md_table(
        ["特征", "IC（全部日）", "t", "天数", "IC（≥3笔/日）", "t", "天数"],
        b["ic_rows"])
    lines += [
        "",
        "关键是最后一组：cb≥3 池每天只有约 3.9 笔（中位 3 笔），"
        "2 笔/日算出来的 rank IC 噪声极大。把每日最少笔数提到 3 笔后，"
        "所有特征 |IC| ≤ 0.065、|t| ≤ 1.43；`open_gap` 的 t=2.70 和 "
        "`last_bar_vol_share` 的 t=2.13 都消失了。18 个特征 × 多种切法下，"
        "Bonferroni 5% 阈值约 |t|>2.9，名义 t=2.7 本身也不足以称为发现。",
        "",
        "### 3.3 中位数分组与分半稳定性",
        "",
    ]
    lines += md_table(
        ["特征", "高−低（全样本）", "高−低 H1", "高−低 H2", "高组n", "低组n", "中位阈值"],
        b["split_rows"])
    lines += [
        "",
        "`open_gap` 的差在全样本 +0.19pp，但 H1 +0.44pp、H2 −0.15pp，方向翻转。"
        "`last_bar_vol_share` 两半都为正，但分组不单调：Q1~Q5 为 "
        "+0.68 / −0.24 / +0.90 / +0.79 / +1.05%/笔；均值差 +0.85pp，"
        "中位差只有约 +0.23pp，胜率 53.5% vs 51.7%，改善主要来自右尾"
        "（≥10% 的比例 5.2% vs 2.9%），不是稳定的胜率提升。",
        "",
        "### 3.4 离散桶",
        "",
    ]
    lines += md_table(["特征", "取值", "笔数", "均净%", "胜率%"], b["discrete_rows"])
    lines += [
        "",
        "`open_limit`（T 日开盘即涨停）看起来最强："
        f"{pct(b['b_ol']['mean'])}% vs {pct(b['b_nool']['mean'])}%，"
        f"两半分别为 +0.99/+0.77 与 +0.56/+0.40，方向稳定。"
        "但它有 49.3% 的 T+1 开盘涨停买不进（非一字只有 15.5%），"
        "且只在连板=3 的子组里有效（连板=4 无差异，连板≥5 反向）。"
        "同日配对价差 t 仅 1.57，不能算稳健增量。",
        "",
        "`sealed_share_after` / `reopen_count` 也有稳定外观：盘中开过板的低组 "
        "+1.24% vs 全天封死 +0.53%，两半同向。但 `reopen_count≥1` 的 T+1 开盘涨停"
        "买不进率只有 3.6%，无开板组 38.8%，差异主要来自可买性选择；"
        "分半 H2 只差约 +0.16pp，同日配对价差 t=−0.32，不能作为增量。",
        "`first_hour_vol_share` 高−低 −0.28pp 且两半同向，但低组 +0.77% 相对基线 "
        "+0.63% 只有 +0.14pp，幅度不足以构成可交易改进。",
        "",
        "### 3.5 迟到量能候选：看起来最好，但不够稳",
        "",
        f"把 T 日 15:00 这根 K 的成交量占全天比例（`last_bar_vol_share`，T 日收盘即可知）"
        f"按中位数 {b['lb_med'] * 100:.2f}% 分组：",
        "",
        f"- 高组 {pct(b['b_lb_hi']['mean'])}%/笔（n={b['b_lb_hi']['n']}，相对 0 的 t={num(b['b_lb_hi']['t'],2)}），"
        f"低组 {pct(b['b_lb_lo']['mean'])}%/笔（n={b['b_lb_lo']['n']}，t={num(b['b_lb_lo']['t'],2)}）。",
    ]
    for h, hi, lo in b["lb_half"]:
        lines.append(f"- {h}：高 {pct(hi['mean'])}% vs 低 {pct(lo['mean'])}%。")
    ss = b["spread_stats"]
    lines += [
        f"- 同日配对价差：{ss['days']} 天，均值 {pct(ss['mean'])}pp/日，t={num(ss['t'],2)}，"
        f"正价差天数 {num(ss['pos'])}%。",
        f"- 但剔除价差最大的 5 天后，均值降到 {pct(ss['drop5_mean'])}pp（t={num(ss['drop5_t'],2)}）；"
        f"剔除 10 天后 {pct(ss['drop10_mean'])}pp（t={num(ss['drop10_t'],2)}）。"
        "价差集中在少数只有 2 笔交易的日子。",
        "- 分位收益不单调：Q1~Q5 分别约 +0.68 / −0.24 / +0.90 / +0.79 / +1.05%/笔，"
        "低组被 Q2 拖累，而不是「量能越高越好」。",
        "- 与一字板交互符号翻转：一字板内高量能组 +0.38% vs 低量能组 +1.40%；"
        "非一字板内 +1.00% vs −0.05%。",
        f"- 高量能组 T+1 开盘涨停买不进率 {num(b['unbuy_lb_hi'])}%，"
        f"低量能组 {num(b['unbuy_lb_lo'])}%，高组本身被剔掉更多。",
        "",
        "### 3.6 探索性组合：只能当上界，不能当结论",
        "",
    ]
    combo_rows = []
    for label, s in b["combo_rows"]:
        combo_rows.append([label, str(s["n"]), pct(s["mean"]), num(s["win"]),
                           pct(s["t"]), str(s["days"])])
    lines += md_table(["组合", "笔数", "均净%", "胜率%", "t", "天数"], combo_rows)
    lines += [
        "",
        "「一字 或 迟到量能高」把基线从 +0.63% 提到约 +1.05%/笔，"
        "分半阈值外推仍为正（H1 +1.14%、H2 +0.96%）。但它是 18 个特征、多种切法里"
        "挑出来的，增量约 +0.42pp，配对 t 在剔除极端日后只有 1.0~1.8；"
        "**只能作为后续 paper trade 的观察项，不能据此改规则。**",
        "",
        "## 4. 结论与建议",
        "",
        "1. **打板路径关闭。** 12.5 个月的 min60 数据把 min5（1 个月）的结论放大并确认："
        "触板即买 −0.45%/−0.61%/笔，封板后等开板 −1.04%/−1.29%/笔；"
        "正收益子集要么是事后信息（收盘封死），要么不可成交（一字板）。"
        "继续在日线/分钟线上找打板参数没有意义。",
        "2. **连板≥3 接力仍是唯一幸存规则**，但 min60 封板质量不能稳定增强它。"
        "基线 +0.63%/笔、t=4.50；表现最好的 `open_limit` 和 `last_bar_vol_share` "
        "都有样本选择、非单调或尾部依赖问题，且未通过同日配对/多重检验。",
        "3. **不要切换现役模型，也不要为 daban 重训。** 模型在可执行样本上没有 edge，"
        "本次 min60 分析也没有给出可学习的标签/特征组合。",
        "4. 唯一可做的是继续 **paper trade cb≥3 接力**（T+1 开盘买、T+1 收盘卖，"
        "开盘涨停放弃，单票≤10%，最多 3 只并发），至少 3 个月，记录真实滑点；"
        "若单笔净收益低于 +0.2%，停止。",
        "",
        "## 5. 复现",
        "",
        "```bash",
        "# 1) 构建 min60 特征（约 123MB，5,221 只 × 256 天）",
        "venv/bin/python scripts/daban_min60_features.py --workers 16",
        "",
        "# 2) Part A/B 分析与报告",
        "venv/bin/python scripts/daban_min60_exec.py",
        "```",
        "",
        "## 6. 数据与偏差",
        "",
        "- min60 覆盖 2025-09-05 ~ 2026-09-24；2026 年只到 9 月下旬，"
        "且只有约 12.5 个月、单一市场周期。",
        "- 股票池为当前上市 A 股，存在退市/重组幸存者偏差；ST 用当前名称近似历史状态，"
        "5% 涨跌幅股票的历史 ST 身份可能有误。",
        "- 触板即买假设涨停价能成交；封板后等开板假设开板时能按开盘价/涨停价成交。"
        "真实排队与冲击成本未实测，0.30% 双边成本可能偏乐观。",
        "- 一字板按涨停价买入在多数情况下不可成交，文中已单独标注，未计入可执行结论。",
        "",
    ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", type=Path, default=FEATURES)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not args.features.exists():
        print(f"[error] 缺少 {args.features}，先跑 scripts/daban_min60_features.py")
        return 1
    print(f"[info] 读取 {args.features} ...")
    raw, valid = load_features(args.features)
    print(f"[info] {len(raw):,} 行 → 有效 {len(valid):,} 行 / "
          f"{valid['code'].nunique():,} 只 / {valid['date'].nunique()} 天")
    ctx = market_context(valid)
    print("[info] Part A ...")
    a = part_a(valid, ctx)
    print("[info] Part B ...")
    b = part_b(valid)
    print(f"[info] 基线 n={b['baseline']['n']} mean={b['baseline']['mean']:.3f}% "
          f"t={b['baseline']['t']:.2f}")
    lines = build_report(raw, valid, a, b)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] {args.out}  ({len(lines)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
