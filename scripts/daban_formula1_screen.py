#!/usr/bin/env python
"""按通达信公式一 DB_V2 的逻辑，在本地全市场跑一遍盘后选股，并与东财涨停池对账。

为什么要对账
------------
公式一在通达信里"测试通过"只证明语法能编译。它算出的涨停价、连板数、
首板/接力判定是不是和交易所真实数据一致，必须用独立数据源验一次。
这里用本地日线（通达信源，量纲：手）复算公式一的每一条语句，
再用 akshare 的东财涨停池/炸板池做外部对照（换手率、流通市值、连板数、涨停价）。

用法::

    venv/bin/python scripts/daban_formula1_screen.py                 # 全市场
    venv/bin/python scripts/daban_formula1_screen.py --limit 300     # 冒烟测试
    venv/bin/python scripts/daban_formula1_screen.py --date 20261022

产物::

    data/daban/formula1_screen_YYYYMMDD.csv     候选明细（含每一步过滤结果）
    data/daban/formula1_screen_YYYYMMDD.json    漏斗计数 + 对账差异

注意：本脚本只做研究和对照，不下单、不接券商。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.limit_price import (  # noqa: E402
    board_label,
    exchange_prefix,
    is_st,
    limit_ratio,
    limit_up_price,
)
from tdxfeed.paths import DATA_ROOT, daily_dir  # noqa: E402

DATA = DATA_ROOT
DAILY = daily_dir()
OUT = DATA / "daban"
EPS = 0.001  # 与通达信公式里的 -0.001 保持一致


def load_universe() -> dict[str, str]:
    payload = json.loads((DATA / "all_a_stocks.json").read_text(encoding="utf-8"))
    return {str(k).zfill(6): str(v) for k, v in payload.items()}


def read_daily(code: str, tail: int = 40) -> pd.DataFrame | None:
    path = DAILY / f"{exchange_prefix(code)}{code}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["date", "open", "high", "low", "close", "volume"])
    if df.empty:
        return None
    return df.tail(tail).reset_index(drop=True)


def sealed_series(df: pd.DataFrame, code: str, name: str) -> list[bool]:
    """逐根复算"收盘封板"，用于 BARSLASTCOUNT(封板)。

    比例按每根 K 线自己的日期取（主板 ST 在 2026-07-06 由 5% 并轨 10%）。
    名称里的 ST 标记仍按当前名称回溯，历史上被戴帽/摘帽的票会有偏差，
    这里接受这个近似，并在输出里标出这类票。
    """
    closes = df["close"].tolist()
    dates = df["date"].astype(str).tolist()
    out: list[bool] = []
    for i in range(1, len(closes)):
        ratio = limit_ratio(code, name, on_date=dates[i])
        limit = limit_up_price(closes[i - 1], ratio)
        out.append(bool(closes[i] >= limit - EPS))
    return out


def barslastcount(flags: list[bool]) -> int:
    n = 0
    for f in reversed(flags):
        if not f:
            break
        n += 1
    return n


def screen(universe: dict[str, str], day: str, limit: int = 0) -> tuple[pd.DataFrame, dict]:
    rows: list[dict] = []
    funnel = {
        "股票池": len(universe),
        "有当日K线": 0,
        "触板": 0,
        "收盘封板": 0,
        "非一字": 0,
        "非ST": 0,
        "非新股前缀": 0,
        "量能1~8": 0,
        "换手3~30": 0,
        "流值20~500亿": 0,
        "XG命中": 0,
    }
    items = sorted(universe.items())
    if limit:
        items = items[:limit]
    for code, name in items:
        df = read_daily(code)
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        if str(last["date"])[:10].replace("-", "") != day:
            continue  # 停牌或当日无数据
        funnel["有当日K线"] += 1
        if len(df) < 2:
            continue
        prev_close = float(df["close"].iloc[-2])
        ratio = limit_ratio(code, name, on_date=day)
        ztj = limit_up_price(prev_close, ratio)
        o, h, l, c = (float(last["open"]), float(last["high"]),
                      float(last["low"]), float(last["close"]))
        touched = h >= ztj - EPS
        sealed = c >= ztj - EPS
        one_word = o >= ztj - EPS and l >= ztj - EPS
        vols = df["volume"].tolist()
        ma5_prev = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else 0.0
        liang = (float(vols[-1]) / ma5_prev) if ma5_prev > 0 else float("nan")
        flags = sealed_series(df, code, name)
        streak = barslastcount(flags)
        if not touched:
            continue
        funnel["触板"] += 1
        row = {
            "code": code,
            "name": name,
            "board": board_label(code, name, on_date=day),
            "ratio": float(ratio),
            "prev_close": prev_close,
            "ZTJ": ztj,
            "raw_limit": round(prev_close * (1 + float(ratio)), 4),
            "open": o, "high": h, "low": l, "close": c,
            "pct": round((c / prev_close - 1) * 100, 3),
            "touched": int(touched),
            "sealed": int(sealed),
            "one_word": int(one_word),
            "lianzong": streak,
            "liang_ratio": round(liang, 3) if liang == liang else float("nan"),
            "vol_lots": float(vols[-1]),
        }
        if not sealed:
            rows.append(row)
            continue
        funnel["收盘封板"] += 1
        if one_word:
            rows.append(row)
            continue
        funnel["非一字"] += 1
        if is_st(name):
            rows.append(row)
            continue
        funnel["非ST"] += 1
        if name[:1] in ("N", "C"):
            row["new_prefix"] = 1
            rows.append(row)
            continue
        funnel["非新股前缀"] += 1
        if not (1.0 <= liang <= 8.0):
            rows.append(row)
            continue
        funnel["量能1~8"] += 1
        rows.append(row)
    frame = pd.DataFrame(rows)
    return frame, funnel


def external_pools(day: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """东财涨停池 / 炸板池，用于对账和补齐换手率、流通市值。"""
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover
        print(f"akshare 不可用: {exc}", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame()
    zt = ak.stock_zt_pool_em(date=day)
    zb = pd.DataFrame()
    for fname in ("stock_zt_pool_zbgc_em", "stock_zb_pool_em"):
        fn = getattr(ak, fname, None)
        if fn is None:
            continue
        try:
            zb = fn(date=day)
            break
        except Exception as exc:
            print(f"炸板池 {fname} 失败: {type(exc).__name__} {exc}", file=sys.stderr)
    return zt, zb


def reconcile(frame: pd.DataFrame, zt: pd.DataFrame, zb: pd.DataFrame) -> dict:
    mine_sealed = set(frame.loc[frame["sealed"] == 1, "code"])
    mine_touch = set(frame["code"])
    ext_zt = set(zt["代码"].astype(str).str.zfill(6)) if len(zt) else set()
    ext_zb = set(zb["代码"].astype(str).str.zfill(6)) if len(zb) else set()
    price_diff = []
    if len(zt):
        zt2 = zt.copy()
        zt2["code"] = zt2["代码"].astype(str).str.zfill(6)
        merged = frame.merge(zt2[["code", "最新价", "涨跌幅", "连板数", "换手率", "流通市值"]],
                             on="code", how="inner")
        for _, r in merged[merged["sealed"] == 1].iterrows():
            if abs(float(r["最新价"]) - float(r["ZTJ"])) > 0.005:
                price_diff.append({"code": r["code"], "name": r["name"],
                                   "mine": float(r["ZTJ"]), "ext": float(r["最新价"])})
        streak_diff = [
            {"code": r["code"], "name": r["name"], "mine": int(r["lianzong"]),
             "ext": int(r["连板数"])}
            for _, r in merged[merged["sealed"] == 1].iterrows()
            if int(r["lianzong"]) != int(r["连板数"])
        ]
    else:
        streak_diff = []
    return {
        "本地封板家数": len(mine_sealed),
        "东财涨停池家数": len(ext_zt),
        "本地有但东财无": sorted(mine_sealed - ext_zt),
        "东财有但本地无": sorted(ext_zt - mine_sealed),
        "本地触板未封家数": len(mine_touch - mine_sealed),
        "东财炸板池家数": len(ext_zb),
        "触板未封_本地有东财无": sorted((mine_touch - mine_sealed) - ext_zb),
        "涨停价不一致": price_diff,
        "连板数不一致": streak_diff,
    }


def enrich(frame: pd.DataFrame, zt: pd.DataFrame, zb: pd.DataFrame) -> pd.DataFrame:
    if not len(zt) and not len(zb):
        return frame
    pool = pd.concat([zt, zb], ignore_index=True) if len(zt) and len(zb) else (zt if len(zt) else zb)
    keep = [c for c in ["代码", "换手率", "流通市值", "封板资金", "炸板次数", "连板数"]
            if c in pool.columns]
    pool = pool[keep].copy()
    pool["code"] = pool["代码"].astype(str).str.zfill(6)
    pool = pool.drop(columns=["代码"]).drop_duplicates("code")
    return frame.merge(pool, on="code", how="left")


def main() -> int:
    ap = argparse.ArgumentParser(description="公式一 DB_V2 本地全市场选股 + 东财对账")
    ap.add_argument("--date", default="20261022", help="交易日 YYYYMMDD")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（冒烟测试）")
    ap.add_argument("--no-ext", action="store_true", help="不请求东财数据")
    args = ap.parse_args()

    day = args.date
    date_str = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    print(f"复算公式一 {date_str}（本地通达信日线，量纲：手）")
    universe = load_universe()
    frame, funnel = screen(universe, day, args.limit)
    # 漏斗里的换手/流值两步要等东财字段补齐才算得出来，放到最后统一打印。

    if args.no_ext:
        zt = zb = pd.DataFrame()
    else:
        try:
            zt, zb = external_pools(day)
            print(f"\n东财涨停池 {len(zt)} 家 / 炸板池 {len(zb)} 家")
        except Exception as exc:
            print(f"东财数据取不到，跳过对账: {type(exc).__name__} {exc}", file=sys.stderr)
            zt = zb = pd.DataFrame()

    check = reconcile(frame, zt, zb) if (len(frame) and len(zt)) else {}
    if check:
        print("\n== 对账 ==")
        for k, v in check.items():
            print(f"  {k}: {v}")

    frame = enrich(frame, zt, zb)
    if len(zt):
        zt2 = zt.copy()
        zt2["code"] = zt2["代码"].astype(str).str.zfill(6)
        keep = [c for c in ["code", "换手率", "流通市值", "封板资金", "炸板次数"] if c in zt2.columns]
        frame = frame.drop(columns=[c for c in ["换手率", "流通市值", "封板资金", "炸板次数"]
                                   if c in frame.columns], errors="ignore")
        frame = frame.merge(zt2[keep].rename(columns={"换手率": "换手率_ext",
                                                     "流通市值": "流通市值_ext",
                                                     "封板资金": "封板资金_ext",
                                                     "炸板次数": "炸板次数_ext"}),
                            on="code", how="left")
        # 换手/流值过滤只能在封板票上算，这里补算 XG 判定
        hs = pd.to_numeric(frame.get("换手率_ext"), errors="coerce")
        lz = pd.to_numeric(frame.get("流通市值_ext"), errors="coerce") / 1e8
        base = (frame["sealed"] == 1) & (frame["one_word"] == 0)
        st_flag = frame["name"].map(is_st)
        new_flag = frame["name"].str[:1].isin(["N", "C"])
        ok_liang = frame["liang_ratio"].between(1, 8)
        ok_hs = hs.between(3, 30)
        ok_lz = lz.between(20, 500)
        first = base & (~st_flag) & (~new_flag) & ok_liang & ok_hs & ok_lz & (frame["lianzong"] == 1)
        relay = base & (~st_flag) & (~new_flag) & ok_liang & ok_hs & ok_lz & frame["lianzong"].between(2, 4)
        frame["XG"] = ((first | relay).fillna(False)).astype(int)
        funnel["换手3~30"] = int((base & ok_hs).sum())
        funnel["流值20~500亿"] = int((base & ok_hs & ok_lz).sum())
        funnel["XG命中"] = int(frame["XG"].sum())
        print("\n== 完整公式一（含换手/流值）==")
        print("  换手3~30:", funnel["换手3~30"], " 流值20~500亿:", funnel["流值20~500亿"],
              " XG命中:", funnel["XG命中"])
        cols = ["code", "name", "board", "prev_close", "raw_limit", "ZTJ", "close", "pct",
                "lianzong", "one_word", "sealed", "liang_ratio", "换手率_ext",
                "流通市值_ext", "封板资金_ext", "炸板次数_ext", "XG"]
        cols = [c for c in cols if c in frame.columns]
        show = frame[frame["XG"] == 1][cols]
        if len(show):
            print("\n== 入选（XG）==")
            print(show.to_string(index=False))
        print("\n== 封板但未入选（被哪一条挡掉）==")
        miss = frame[(frame["sealed"] == 1) & (frame["XG"] == 0)][cols]
        if len(miss):
            print(miss.to_string(index=False))

    print("\n== 漏斗（换手/流值两步来自东财字段，口径见文件头）==")
    for k, v in funnel.items():
        print(f"  {k:12s} {v}")

    OUT.mkdir(parents=True, exist_ok=True)
    csv = OUT / f"formula1_screen_{day}.csv"
    frame.to_csv(csv, index=False, encoding="utf-8-sig")
    js = OUT / f"formula1_screen_{day}.json"
    js.write_text(json.dumps({"date": date_str, "funnel": funnel, "reconcile": check,
                             "n_rows": len(frame), "limit": args.limit},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {csv} 和 {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
