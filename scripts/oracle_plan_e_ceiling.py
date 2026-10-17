#!/usr/bin/env python3
"""方案A 天花板测试（零成本，无需训练）。

用方案E 每笔交易的「实际实现收益 return_pct」作为 oracle 方向标签，
模拟一个"完美方向分类器"能带来的最大可达收益，用以判断前瞻信号是否值得训练。

- oracle 方向：return_pct > +θ → 涨；< -θ → 跌；否则 震荡（θ 与训练标签一致=0.02）。
- 闸门：baseline(无过滤) / 涨过滤 / 排除跌 / 排除震荡。
- 仅非北交所事件池（与 llm_signal_plan_e.compare 一致）。

用法：
  venv/bin/python scripts/oracle_plan_e_ceiling.py --theta 0.02
"""
from __future__ import annotations

import argparse
import re

import pandas as pd
from _common import is_bj, norm_code  # 审计 P1-3-1 2026-10-17: 统一 4 处重复实现
from plan_e_backtest import backtest, load_double_up

THETA = 0.02



def _metrics(bt: pd.DataFrame) -> dict:
    if bt.empty:
        return {"n": 0, "win_rate": float("nan"), "avg_ret": float("nan"),
                "median_ret": float("nan"), "tp": 0, "sl": 0, "time": 0,
                "cum_ret": float("nan")}
    n = len(bt)
    wins = int((bt["return_pct"] > 0).sum())
    avg = bt["return_pct"].mean()
    med = bt["return_pct"].median()
    cum = (1 + bt["return_pct"]).prod() - 1
    return {"n": n, "win_rate": wins / n, "avg_ret": avg, "median_ret": med,
            "tp": int((bt["exit_reason"] == "TP").sum()),
            "sl": int((bt["exit_reason"] == "SL").sum()),
            "time": int((bt["exit_reason"] == "TIME").sum()),
            "cum_ret": cum}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--theta", type=float, default=THETA)
    args = ap.parse_args()
    theta = args.theta

    bt_valid, _ = backtest(load_double_up())
    bt_valid = bt_valid[~bt_valid["code"].apply(
        lambda c: norm_code(c).startswith("920") or (norm_code(c) and norm_code(c)[0] == "8"))].reset_index(drop=True)
    bt_valid["code_n"] = bt_valid["code"].map(norm_code)
    bt_valid["oracle"] = bt_valid["return_pct"].apply(
        lambda r: 1 if r > theta else (-1 if r < -theta else 0))
    print(f"方案E 非北交所有效交易: {len(bt_valid)} 笔（oracle θ={theta}）")
    od = bt_valid["oracle"]
    n = len(od)
    print(f"  oracle 分布: 涨{(od==1).sum()}({(od==1).sum()/n*100:.1f}%) "
          f"震荡{(od==0).sum()}({(od==0).sum()/n*100:.1f}%) "
          f"跌{(od==-1).sum()}({(od==-1).sum()/n*100:.1f}%)")

    base = set(bt_valid["code_n"])
    subsets = {
        "baseline(无过滤)": base,
        "oracle_涨过滤": base & set(bt_valid[bt_valid["oracle"] == 1]["code_n"]),
        "oracle_排除跌": base & set(bt_valid[bt_valid["oracle"] != -1]["code_n"]),
        "oracle_排除震荡": base & set(bt_valid[bt_valid["oracle"].isin([1, -1])]["code_n"]),
    }
    results = {name: _metrics(bt_valid[bt_valid["code_n"].isin(codes)])
               for name, codes in subsets.items()}

    print("\n# 方案E 天花板：oracle 完美方向分类器（理论最大可达）\n")
    print("| 模式 | 笔数 | 胜率 | 均收益 | 中位收益 | 累计收益 | TP | SL | TIME |")
    print("|------|------|------|--------|----------|----------|----|----|------|")
    for name, m in results.items():
        wr = f"{m['win_rate']*100:.2f}%" if m["n"] else "—"
        ar = f"{m['avg_ret']*100:.2f}%" if m["n"] else "—"
        mr = f"{m['median_ret']*100:.2f}%" if m["n"] else "—"
        cr = f"{m['cum_ret']*100:.2f}%" if m["n"] else "—"
        print(f"| {name} | {m['n']} | {wr} | {ar} | {mr} | {cr} | {m['tp']} | {m['sl']} | {m['time']} |")
    print("\n说明：oracle 用每笔实际实现收益作方向，是『完美预测』的理论上限；"
          "真实模型只能逼近，故实际可达 << 此值。若上限很小则无需训练。")


if __name__ == "__main__":
    main()
