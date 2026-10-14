#!/usr/bin/env python3
"""方案A 实证：对比 新模型(v4fwd) 三分类闸门 vs 基线，并按事件年做 walk-forward 拆分。

输入：一份 LLM 方向信号 CSV（llm_signal_plan_e.py gen 产出，含 code/direction）。
闸门：baseline(无过滤) / 涨过滤 / 排除跌 / 排除震荡。
walk-forward：按翻倍事件年份拆分，逐年报告 baseline 与「排除跌」效果
（2024-2026 与模型训练期重叠，标注为「训练期重合」，仅供稳定性参考）。

用法：
  venv/bin/python scripts/compare_plan_e_signals.py \
      --signal-csv data/plan_e/llm_sig_v4fwd.csv --name v4fwd
"""
from __future__ import annotations

import argparse
import re

import pandas as pd
from plan_e_backtest import backtest, get_first_double_date, load_double_up

THETA = 0.02


def _norm_code(c: str) -> str:
    c = str(c).lower()
    c = re.sub(r"^(sh|sz|bj)", "", c)
    return c.zfill(6)


def _is_bj(c: str) -> bool:
    c = _norm_code(c)
    return c.startswith("920") or (c and c[0] == "8")


def _metrics(bt: pd.DataFrame) -> dict:
    if bt.empty:
        return {"n": 0, "win_rate": float("nan"), "avg_ret": float("nan"),
                "median_ret": float("nan"), "tp": 0, "sl": 0, "time": 0,
                "cum_ret": float("nan")}
    n = len(bt)
    wins = int((bt["return_pct"] > 0).sum())
    return {"n": n, "win_rate": wins / n, "avg_ret": bt["return_pct"].mean(),
            "median_ret": bt["return_pct"].median(),
            "tp": int((bt["exit_reason"] == "TP").sum()),
            "sl": int((bt["exit_reason"] == "SL").sum()),
            "time": int((bt["exit_reason"] == "TIME").sum()),
            "cum_ret": (1 + bt["return_pct"]).prod() - 1}


def _row(name: str, m: dict) -> str:
    if not m["n"]:
        return f"| {name} | 0 | — | — | — | — | — | — | — |"
    return (f"| {name} | {m['n']} | {m['win_rate']*100:.2f}% | {m['avg_ret']*100:.2f}% | "
            f"{m['median_ret']*100:.2f}% | {m['cum_ret']*100:.2f}% | {m['tp']} | {m['sl']} | {m['time']} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal-csv", required=True)
    ap.add_argument("--name", default="v4fwd")
    ap.add_argument("--theta", type=float, default=THETA)
    args = ap.parse_args()

    bt, _ = backtest(load_double_up())
    bt = bt[~bt["code"].apply(_is_bj)].reset_index(drop=True)
    bt["code_n"] = bt["code"].map(_norm_code)
    print(f"方案E 非北交所有效交易: {len(bt)} 笔")

    # 事件年份映射
    try:
        fd = get_first_double_date(load_double_up())
        year_map = {_norm_code(r["code"]): pd.Timestamp(r["signal_date"]).year
                   for _, r in fd.iterrows()}
    except Exception as e:
        print(f"[warn] 年份映射失败: {e}")
        year_map = {}
    bt["year"] = bt["code_n"].map(year_map).fillna(0).astype(int)

    sig = pd.read_csv(args.signal_csv)
    sig["code_n"] = sig["code"].astype(str).map(_norm_code)
    dmap = dict(zip(sig["code_n"], sig["direction"]))
    bt["pred"] = bt["code_n"].map(dmap)
    n_pred = bt["pred"].notna().sum()
    print(f"信号覆盖: {n_pred}/{len(bt)} 笔有方向预测")
    eff = bt[bt["pred"].notna()]
    if len(eff):
        d = eff["pred"]
        print(f"方向分布: 涨{(d==1).sum()}({(d==1).sum()/len(d)*100:.1f}%) "
              f"震荡{(d==0).sum()}({(d==0).sum()/len(d)*100:.1f}%) "
              f"跌{(d==-1).sum()}({(d==-1).sum()/len(d)*100:.1f}%)  "
              f"（若仍≈100%涨则为退化，未修复）")

    base = set(bt["code_n"])
    gates = {
        "baseline(无过滤)": base,
        f"{args.name}_涨过滤": base & set(bt[bt["pred"] == 1]["code_n"]),
        f"{args.name}_排除跌": base & set(bt[bt["pred"] != -1]["code_n"]),
        f"{args.name}_排除震荡": base & set(bt[bt["pred"].isin([1, -1])]["code_n"]),
    }
    res = {k: _metrics(bt[bt["code_n"].isin(v)]) for k, v in gates.items()}

    print(f"\n# 方案E 回测对比：{args.name}（三分类闸门）\n")
    print("| 模式 | 笔数 | 胜率 | 均收益 | 中位收益 | 累计收益 | TP | SL | TIME |")
    print("|------|------|------|--------|----------|----------|----|----|------|")
    for k, m in res.items():
        print(_row(k, m))

    # walk-forward：按事件年拆分
    print("\n# Walk-forward（按翻倍事件年份；2024-2026 与模型训练期重合）\n")
    print("| 年份 | 模式 | 笔数 | 胜率 | 均收益 | 中位收益 | 累计 | SL |")
    print("|------|------|------|------|--------|----------|------|----|")
    for yr in sorted(bt["year"].unique()):
        sub = bt[bt["year"] == yr]
        if len(sub) < 5:
            continue
        tag = "（训练期重合）" if yr >= 2024 else ""
        for label, codes in (("baseline", base & set(sub["code_n"])),
                             (f"{args.name}_排除跌", set(sub[sub['pred'] != -1]['code_n']))):
            print(f"| {yr}{tag if label=='baseline' else ''} | {label} | "
                  + _row("", _metrics(sub[sub["code_n"].isin(codes)]))[2:])
    print("\n说明：该 walk-forward 为「按事件年评估闸门稳定性」，非严格时序 OOS"
          "（全量模型训练期覆盖全部年份）。如需严格 OOS 须用训练期截止模型重训。")


if __name__ == "__main__":
    main()
