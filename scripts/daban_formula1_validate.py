#!/usr/bin/env python
"""公式一语义的多日交叉验证：本地复算的涨停价/连板数 vs 东财官方池。

和 daban_formula1_screen.py 的分工
----------------------------------
screen 回答"今天这一天选股跑不跑得出来"；本脚本回答"这套规则放到历史若干天里，
和交易所口径差多少"。重点验三件事：

1. 涨停价（对应公式里的 ZTPRICE 四舍五入）——低价股、20% 双创、5% 主板 ST 三类边界；
2. 连板数（对应 BARSLASTCOUNT(封板)）；
3. 收盘封板家数集合，与炸板集合的口径差。

对照字段来自两个独立来源：
  * 涨停池的"最新价"（收盘封死 ⇒ 最新价 == 当日涨停价）；
  * 炸板池的"涨停价"（显式字段；这些票收盘没封住，用最新价验不到，所以更有价值）。

用法::

    venv/bin/python scripts/daban_formula1_validate.py            # 最近 10 个交易日
    venv/bin/python scripts/daban_formula1_validate.py --days 20

只做研究和对照，不下单、不接券商。
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
EPS = 0.001
PRICE_TOL = 0.005  # 只允许分位以内的浮点噪声


def load_universe() -> dict[str, str]:
    payload = json.loads((DATA / "all_a_stocks.json").read_text(encoding="utf-8"))
    return {str(k).zfill(6): str(v) for k, v in payload.items()}


def external_pools(day: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """东财涨停池 / 炸板池（与 screen 脚本同口径）。"""
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover
        print(f"akshare 不可用: {exc}", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame()
    try:
        zt = ak.stock_zt_pool_em(date=day)
    except Exception as exc:
        print(f"涨停池 {day} 失败: {type(exc).__name__} {exc}", file=sys.stderr)
        zt = pd.DataFrame()
    zb = pd.DataFrame()
    for fname in ("stock_zt_pool_zbgc_em", "stock_zb_pool_em"):
        fn = getattr(ak, fname, None)
        if fn is None:
            continue
        try:
            zb = fn(date=day)
            break
        except Exception as exc:
            print(f"炸板池 {fname} {day} 失败: {type(exc).__name__} {exc}", file=sys.stderr)
    return zt if zt is not None else pd.DataFrame(), zb


def recent_trading_days(n: int) -> list[str]:
    """用一只常年不停牌的票（600000 浦发银行）的日线日期当交易日历。"""
    for probe in ("600000", "601398", "000001"):
        path = DAILY / f"{exchange_prefix(probe)}{probe}.parquet"
        if path.exists():
            dates = pd.read_parquet(path, columns=["date"])["date"].astype(str).str[:10]
            return [d.replace("-", "") for d in dates.tolist()[-n:]]
    raise SystemExit("找不到用作交易日历的本地日线文件")


def barslastcount(flags: list[bool]) -> int:
    streak = 0
    for ok in reversed(flags):
        if not ok:
            break
        streak += 1
    return streak


def screen_asof(universe: dict[str, str], day: str) -> pd.DataFrame:
    """在 day 收盘时点复算公式一的涨停价 / 触板 / 封板 / 连板（严格只用 day 及以前）。"""
    rows = []
    for code, name in sorted(universe.items()):
        path = DAILY / f"{exchange_prefix(code)}{code}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["date", "open", "high", "low", "close", "volume"])
        df["d"] = df["date"].astype(str).str[:10].str.replace("-", "")
        df = df[df["d"] <= day]
        if df.empty or df["d"].iloc[-1] != day or len(df) < 2:
            continue
        prev_close = float(df["close"].iloc[-2])
        ratio = limit_ratio(code, name, on_date=day)
        ztj = limit_up_price(prev_close, ratio)
        last = df.iloc[-1]
        _o, h, _l, c = (float(last[k]) for k in ("open", "high", "low", "close"))
        # 连板：逐日重算涨停价，只看该日及之前 30 根（比例按各日规则取）
        tail = df.tail(31).reset_index(drop=True)
        flags = []
        for i in range(1, len(tail)):
            pc = float(tail["close"][i - 1])
            r = limit_ratio(code, name, on_date=str(tail["d"][i]))
            lim = limit_up_price(pc, r)
            flags.append(float(tail["close"][i]) >= lim - EPS)
        streak = barslastcount(flags)
        touched = h >= ztj - EPS
        sealed = c >= ztj - EPS
        if not touched:
            continue
        rows.append({
            "code": code, "name": name, "board": board_label(code, name, on_date=day),
            "ratio": float(ratio), "prev_close": prev_close, "ZTJ": ztj,
            "raw_limit": round(prev_close * (1 + float(ratio)), 4),
            "close": c, "touched": int(touched), "sealed": int(sealed),
            "lianzong": streak,
        })
    return pd.DataFrame(rows)


def check_day(day: str, universe: dict[str, str]) -> dict:
    frame = screen_asof(universe, day)
    zt, zb = external_pools(day)
    res = {"day": day, "本地触板": len(frame), "本地封板": int(frame["sealed"].sum()) if len(frame) else 0,
           "东财涨停池": len(zt), "东财炸板池": len(zb),
           "涨停价_验证样本": 0, "涨停价_不一致": [], "连板数_不一致": [],
           "封板_本地有东财无": [], "封板_东财有本地无": [], "ST样本被外部验证": 0}
    if not len(frame) or not len(zt):
        res["skipped"] = True
        return res

    f = frame.copy()
    f["code"] = f["code"].astype(str).str.zfill(6)

    # 1) 涨停池：收盘封死，最新价 == 当日涨停价
    z = zt.copy()
    z["code"] = z["代码"].astype(str).str.zfill(6)
    m = f[f["sealed"] == 1].merge(z[["code", "最新价", "连板数"]], on="code", how="inner")
    for _, r in m.iterrows():
        res["涨停价_验证样本"] += 1
        if abs(float(r["最新价"]) - float(r["ZTJ"])) > PRICE_TOL:
            res["涨停价_不一致"].append({"src": "涨停池", "day": day, "code": r["code"], "name": r["name"],
                                       "board": r["board"], "prev": r["prev_close"],
                                       "raw": r["raw_limit"], "mine": r["ZTJ"], "ext": float(r["最新价"])})
        if is_st(r["name"]):
            res["ST样本被外部验证"] += 1
        if int(r["lianzong"]) != int(r["连板数"]):
            res["连板数_不一致"].append({"code": r["code"], "name": r["name"],
                                      "mine": int(r["lianzong"]), "ext": int(r["连板数"])})
    sealed_mine = set(f.loc[f["sealed"] == 1, "code"])
    sealed_ext = set(z["code"])
    res["封板_本地有东财无"] = sorted(sealed_mine - sealed_ext)
    res["封板_东财有本地无"] = sorted(sealed_ext - sealed_mine)

    # 2) 炸板池：显式"涨停价"字段，专治"没封住所以验不到"的盲区
    if len(zb) and "涨停价" in zb.columns:
        b = zb.copy()
        b["code"] = b["代码"].astype(str).str.zfill(6)
        mb = f.merge(b[["code", "涨停价"]], on="code", how="inner")
        for _, r in mb.iterrows():
            res["涨停价_验证样本"] += 1
            ext = float(r["涨停价"])
            if ext > 0 and abs(ext - float(r["ZTJ"])) > PRICE_TOL:
                res["涨停价_不一致"].append({"src": "炸板池", "day": day, "code": r["code"], "name": r["name"],
                                           "board": r["board"], "prev": r["prev_close"],
                                           "raw": r["raw_limit"], "mine": r["ZTJ"], "ext": ext})
            if is_st(r["name"]):
                res["ST样本被外部验证"] += 1
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="公式一涨停价/连板数的多日交叉验证")
    ap.add_argument("--days", type=int, default=10, help="回看最近 N 个交易日")
    args = ap.parse_args()

    universe = load_universe()
    days = recent_trading_days(args.days)
    print(f"验证区间 {days[0]} → {days[-1]}（{len(days)} 个交易日），股票池 {len(universe)} 只\n")

    all_res = []
    for day in days:
        r = check_day(day, universe)
        all_res.append(r)
        bad = len(r["涨停价_不一致"]) + len(r["连板数_不一致"])
        print(f"  {day}  触板{r['本地触板']:>4} 封板{r['本地封板']:>3} | 东财涨停{r['东财涨停池']:>3} "
              f"炸板{r['东财炸板池']:>3} | 涨停价样本{r['涨停价_验证样本']:>3} 不一致{len(r['涨停价_不一致']):>2} "
              f"| 连板不一致{len(r['连板数_不一致']):>2} | 封板集合差 {len(r['封板_本地有东财无'])}/{len(r['封板_东财有本地无'])}"
              f"{'  ⚠' if bad else ''}")

    n_price = sum(len(r["涨停价_不一致"]) for r in all_res)
    n_streak = sum(len(r["连板数_不一致"]) for r in all_res)
    n样本 = sum(r["涨停价_验证样本"] for r in all_res)
    st_ext = sum(r["ST样本被外部验证"] for r in all_res)
    print("\n== 汇总 ==")
    print(f"  涨停价交叉样本: {n样本}   不一致: {n_price}")
    print(f"  连板数不一致: {n_streak}")
    print(f"  其中 ST 票被外部样本覆盖: {st_ext}（东财两个池都不收 ST，所以 ST 的 5% 进位只能靠规则核对）")
    for tag, key in (("涨停价", "涨停价_不一致"), ("连板数", "连板数_不一致")):
        items = [it for r in all_res for it in r[key]]
        if items:
            print(f"\n== {tag} 明细（{len(items)} 条）==")
            print(pd.DataFrame(items).to_string(index=False))
    setdiff = [(r["day"], r["封板_本地有东财无"], r["封板_东财有本地无"]) for r in all_res
               if r["封板_本地有东财无"] or r["封板_东财有本地无"]]
    if setdiff:
        print("\n== 封板集合逐日差异（不含 ST/新股，东财池同口径）==")
        for day, a, b in setdiff:
            print(f"  {day}  本地多 {a}  东财多 {b}")
    else:
        print("\n  封板集合：每一天都与东财涨停池完全一致")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"formula1_validate_{days[-1]}.json"
    path.write_text(json.dumps(all_res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n已写出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
