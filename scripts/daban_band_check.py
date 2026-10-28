#!/usr/bin/env python
"""用本地日线反推每只票的真实涨跌幅限制，检查 data/all_a_stocks.json 的名称/板块标签是否过期。

为什么需要这一步
----------------
公式一里 ST 的判定靠 NAMEINCLUDE('ST')，涨停幅度靠 FINANCE(3) 分板块。通达信里这两个字段
是实时的，所以公式本身没问题；但在 Python 复算时我用的是本地导出的名称表
（data/all_a_stocks.json）。多日交叉验证一度抓到 603838 的涨停价对不上（本地按 5% 算出
12.80，东财写 13.41）。真正的原因不是名称表过期，而是规则变了：**2026-07-06 起沪深主板
风险警示股票（ST/*ST）涨跌幅由 5% 并轨 10%**（上证发〔2026〕41 号，深交所同步修订），
科创板/创业板 ST 仍是 20%，北交所 ST 仍是 30%。本脚本用来把这类"标签/规则与真实波动不符"
的票一次性量化出来，作为回归检查——修完规则后应当只剩真正无涨跌幅限制的票（新股等）。

判据（只用正向涨幅，避免除权造成的向下跳空干扰）：
  近 WINDOW 个交易日里最高的单日涨幅 max_gain，若超过假定上限 ratio + 容差，
  说明这只票在该区间里真实涨跌幅限制比标签给的更宽。

用法::

    venv/bin/python scripts/daban_band_check.py                 # 全市场
    venv/bin/python scripts/daban_band_check.py --window 60
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

from tdxfeed.daban.limit_price import exchange_prefix, is_st, limit_ratio  # noqa: E402
from tdxfeed.paths import DATA_ROOT, daily_dir  # noqa: E402

DATA = DATA_ROOT
DAILY = daily_dir()
TOL = 0.006  # 6 个bp 容差，够吸收四舍五入，又不足以放过真实的越界


def main() -> int:
    ap = argparse.ArgumentParser(description="本地日线反推涨跌幅限制，检查名称标签时效性")
    ap.add_argument("--window", type=int, default=30, help="回看交易日数")
    args = ap.parse_args()

    payload = json.loads((DATA / "all_a_stocks.json").read_text(encoding="utf-8"))
    universe = {str(k).zfill(6): str(v) for k, v in payload.items()}
    print(f"股票池 {len(universe)} 只，回看 {args.window} 个交易日\n")

    rows = []
    scanned = 0
    for code, name in sorted(universe.items()):
        path = DAILY / f"{exchange_prefix(code)}{code}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["date", "close"])
        if len(df) < 3:
            continue
        df = df.tail(args.window + 1)
        close = df["close"].astype(float).tolist()
        pct = [(close[i] / close[i - 1] - 1.0) for i in range(1, len(close))]
        if not pct:
            continue
        scanned += 1
        last_date = str(df["date"].iloc[-1])[:10]
        ratio = float(limit_ratio(code, name, on_date=last_date))
        max_gain = max(pct)
        if max_gain > ratio + TOL:
            rows.append({"code": code, "name": name, "assumed_ratio": round(ratio, 3),
                         "max_gain_pct": round(max_gain * 100, 3), "is_st_label": int(is_st(name)),
                         "last_date": last_date})
    print(f"实际有日线的票: {scanned}")
    print(f"涨幅越过假定上限（标签可疑）: {len(rows)}")
    if rows:
        frame = pd.DataFrame(rows).sort_values("max_gain_pct", ascending=False)
        st_rows = frame[frame["is_st_label"] == 1]
        print(f"  其中名称表标成 ST 的: {len(st_rows)} —— 若这些票的 assumed_ratio 已是 0.10，"
              f"越界就是真正的异常（无涨跌幅限制的新股、或名称/板块判定错了）")
        print()
        print(frame.head(30).to_string(index=False))
        out = DATA / "daban"
        out.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out / "band_check_stale_labels.csv", index=False)
        print(f"\n已写出 {out / 'band_check_stale_labels.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
