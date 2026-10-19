"""daban 实盘验证：核对推荐股在验证日的实际涨停命中率。

背景：
  daban 模型标签 = y=1 当且仅当 close[T+1]/close[T] - 1 >= limit（次日收盘涨停）。
  数据最新日 = T（如 10-16 周五收盘），则验证日 = T 的下一个交易日（如 10-19 周一）。
  因此推荐清单在「验证日」收盘后即可核对命中情况。

流程：
  1. 读取 track CSV（由 predict.py 输出整理，含 date/model/rank/code/prob/close/consec）
  2. 对每只股票，取验证日（track date 当天，或之后第一个有数据的交易日）的收盘价
  3. 计算 验证日收盘 / 上一交易日收盘 - 1，判断是否达到涨停限制
  4. 分别统计 daban 与 stage1 的 Top-N 命中率，给出对照

用法：
    venv/bin/python scripts/verify_daban_hit.py --track data/predictions/verify/track_20261019.csv
    venv/bin/python scripts/verify_daban_hit.py --track <file> --topn 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tdxfeed"))

from features_5d import get_limit  # noqa: E402

PARQUET_DIR = ROOT / "data" / "parquet" / "daily"


def find_parquet(code: str) -> Path | None:
    for cand in (f"{code}.parquet", f"sh{code}.parquet",
                 f"sz{code}.parquet", f"bj{code}.parquet"):
        fp = PARQUET_DIR / cand
        if fp.exists():
            return fp
    return None


def hit_one(code: str, verify_date: str) -> tuple[bool, float, str]:
    """判断单只股票在验证日是否涨停。返回 (是否涨停, 涨幅, 实际日期)。"""
    fp = find_parquet(code)
    if fp is None:
        return False, float("nan"), "无数据"

    df = pd.read_parquet(fp)
    if len(df) < 30:
        return False, float("nan"), "数据不足"

    df = df.sort_values("date").reset_index(drop=True)
    df["date_str"] = df["date"].astype(str).str[:10]

    # 验证日：track date 当天；若无（未同步），取之后第一个交易日
    after = df[df["date_str"] >= verify_date]
    if after.empty:
        return False, float("nan"), "未同步"

    idx = after.index[0]
    if idx == 0:
        return False, float("nan"), "无前收"

    limit_val = get_limit(code)
    prev_close = float(df["close"].iloc[idx - 1])
    close = float(df["close"].iloc[idx])
    if prev_close <= 0:
        return False, float("nan"), "前收为0"

    ret = close / prev_close - 1.0
    return ret >= (limit_val - 0.001), ret, str(df["date_str"].iloc[idx])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", required=True, help="track CSV 路径")
    ap.add_argument("--topn", type=int, default=20, help="统计前 N 只")
    args = ap.parse_args()

    fp = Path(args.track)
    if not fp.exists():
        print(f"❌ 找不到 {fp}", file=sys.stderr)
        return 1

    df = pd.read_csv(fp, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.zfill(6)

    print(f"核对 {fp.name}  (共 {len(df)} 条)")
    print(f"{'='*72}")

    results = {}
    for model in sorted(df["model"].unique()):
        sub = df[df["model"] == model].sort_values("rank").head(args.topn)
        hits, rets, dates = 0, [], set()
        rows = []
        for _, r in sub.iterrows():
            ok, ret, vd = hit_one(r["code"], str(r["date"]))
            hits += int(ok)
            rets.append(ret)
            dates.add(vd)
            rows.append({"rank": r["rank"], "code": r["code"], "name": r["name"],
                         "prob": r["prob"], "consec": r.get("consec", 0),
                         "ret": ret, "hit": "✅" if ok else "—", "verify_date": vd})
        rate = hits / max(len(sub), 1)
        results[model] = {"hits": hits, "n": len(sub), "rate": rate,
                          "avg_ret": sum(x for x in rets if x == x) / max(
                              len([x for x in rets if x == x]), 1)}

        print(f"\n【{model}】Top {len(sub)} 命中 {hits} 只 → 命中率 {rate*100:.1f}%  "
              f"(平均涨幅 {results[model]['avg_ret']*100:+.2f}%)  验证日: {sorted(dates)}")
        print(f"  {'rank':<5}{'代码':<8}{'名称':<12}{'prob':<8}{'连板':<5}{'涨幅':<9}{'命中':<5}")
        for r in rows:
            ret_s = f"{r['ret']*100:+.2f}%" if r["ret"] == r["ret"] else "n/a"
            print(f"  {r['rank']:<5}{r['code']:<8}{r['name']:<12}{r['prob']:<8.4f}"
                  f"{r['consec']:<5}{ret_s:<9}{r['hit']:<5}")

    if len(results) == 2:
        print(f"\n{'='*72}")
        print("【对照】")
        for m, v in results.items():
            print(f"  {m:<8}: {v['hits']}/{v['n']}  命中率 {v['rate']*100:.1f}%  "
                  f"平均涨幅 {v['avg_ret']*100:+.2f}%")
        a, b = sorted(results.items(), key=lambda x: -x[1]["rate"])
        print(f"  → 命中率更优: {a[0]}（{a[1]['rate']*100:.1f}% vs {b[1]['rate']*100:.1f}%）")
        print(f"  → 平均涨幅更优: {max(results.items(), key=lambda x: x[1]['avg_ret'])[0]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
