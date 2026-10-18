"""诊断：打板样本集的正样本率与「不可交易」样本噪声分布（纯只读，不训练不落盘权重）。

背景（缺陷 DEFECT-SAMPLE-2026-10-18-01）：
  标签 y = 1 当且仅当 close[t+1]/close[t] - 1 >= limit，只看收盘价。
  关键：要区分「T 日不可买入」与「T+1 不可卖出」——
    · T 日一字板  → 打板者买不进 → 该样本实盘不可执行 → 真噪声
    · T+1 一字板  → 持有者可卖出获利 → 可执行 → 不是噪声（是好样本）

本脚本统计：
  1. 自然正样本率（正样本 / 全面板样本，未负采样）
  2. T 日一字板 / T 日开盘涨停 的样本规模及其中的正样本数
  3. 剔除不可交易样本后，正样本率落到多少（对照目标 1-2%）
  4. 按数据长度（上市日龄代理）与板块分层定位噪声分布

用法：
    venv/bin/python scripts/diagnose_sample_design.py              # 全量（约 2-4 min）
    venv/bin/python scripts/diagnose_sample_design.py --limit 300  # 抽样（秒级）
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tdxfeed"))

from features_5d import get_limit  # noqa: E402
from train_daban_model import iter_stock_files  # noqa: E402


def _bucket_len(n: int) -> str:
    if n < 200:
        return "1) <200日(疑似次新)"
    if n < 400:
        return "2) 200-400日"
    if n < 700:
        return "3) 400-700日"
    return "4) >=700日(老股)"


def _bucket_board(code: str) -> str:
    if code.startswith("68"):
        return "科创板(20%)"
    if code.startswith("30"):
        return "创业板(20%)"
    return "主板(10%)"


def diag_one(item: tuple) -> dict | None:
    """统计单只股票：总样本、正样本、T日一字板/开盘涨停分布。"""
    code, fp = item
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    if len(df) < 30:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    limit_val = get_limit(code)
    close = df["close"].values.astype(float)
    openp = df["open"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    n = len(df)

    out = {
        "code": code, "len": n,
        "board": _bucket_board(code), "len_bucket": _bucket_len(n),
        "n_total": 0, "n_pos": 0,
        "n_t_yizi": 0, "n_t_yizi_pos": 0,
        "n_t_olimit": 0, "n_t_olimit_pos": 0,
        "n_t1_yizi": 0,
    }

    for sig_idx in range(4, n - 1):
        cur_close = close[sig_idx]
        nxt_close = close[sig_idx + 1]
        if cur_close <= 0:
            continue
        out["n_total"] += 1

        prev_close = close[sig_idx - 1]
        is_pos = (nxt_close / cur_close - 1.0) >= (limit_val - 0.001)

        # T 日（信号日，即买入日）状态 —— 决定「能不能买到」
        o, h, l, c = openp[sig_idx], high[sig_idx], low[sig_idx], cur_close
        t_yizi = (o == h == l == c) and prev_close > 0 and (
            (c / prev_close - 1.0) >= (limit_val - 0.001))
        t_olimit = prev_close > 0 and ((o / prev_close - 1.0) >= (limit_val - 0.001))

        # T+1 日状态 —— 决定「能不能卖出」（一字板可卖出）
        j = sig_idx + 1
        t1_yizi = (openp[j] == high[j] == low[j] == close[j]) and is_pos

        if t_yizi:
            out["n_t_yizi"] += 1
            if is_pos:
                out["n_t_yizi_pos"] += 1
        if t_olimit:
            out["n_t_olimit"] += 1
            if is_pos:
                out["n_t_olimit_pos"] += 1
        if t1_yizi:
            out["n_t1_yizi"] += 1
        if is_pos:
            out["n_pos"] += 1

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="抽样股票数")
    ap.add_argument("--workers", type=int, default=4, help="并行进程数")
    args = ap.parse_args()

    files = iter_stock_files(args.limit)
    print(f"诊断 {len(files)} 只股票（workers={args.workers}）...")

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = [r for r in ex.map(diag_one, files, chunksize=32) if r]

    if not results:
        print("❌ 无样本", file=sys.stderr)
        return 1

    keys = ["n_total", "n_pos", "n_t_yizi", "n_t_yizi_pos", "n_t_olimit", "n_t_olimit_pos", "n_t1_yizi"]
    tot = {k: sum(r[k] for r in results) for k in keys}

    print(f"\n{'='*66}")
    print("【总体】")
    print(f"{'='*66}")
    print(f"  股票数                    : {len(results):,}")
    print(f"  总样本(全面板,未负采样)   : {tot['n_total']:,}")
    print(f"  正样本(T+1收盘涨停)       : {tot['n_pos']:,}")
    print(f"  → 自然正样本率            : {tot['n_pos']/max(tot['n_total'],1)*100:.2f}%")
    print()
    print(f"  [T 日状态 = 能不能买到]  ← 这才是「不可交易」噪声")
    print(f"  ├ T 日一字板(开=高=低=收) : {tot['n_t_yizi']:,}  (其中正样本 {tot['n_t_yizi_pos']:,})")
    print(f"  ├ T 日开盘即涨停          : {tot['n_t_olimit']:,}  (其中正样本 {tot['n_t_olimit_pos']:,})")
    print(f"  └ 剔除 T 日一字板后正样本率: {(tot['n_pos']-tot['n_t_yizi_pos'])/max(tot['n_total']-tot['n_t_yizi'],1)*100:.2f}%")
    print(f"     剔除 T 日开盘涨停后正样本率: {(tot['n_pos']-tot['n_t_olimit_pos'])/max(tot['n_total']-tot['n_t_olimit'],1)*100:.2f}%")
    print()
    print(f"  [T+1 日状态 = 能不能卖出] ← 一字板可顺利卖出获利, 非噪声")
    print(f"  └ T+1 一字板(含于正样本)   : {tot['n_t1_yizi']:,}  ({tot['n_t1_yizi']/max(tot['n_pos'],1)*100:.2f}% of 正样本)")

    def _agg(keyfunc) -> dict:
        d: dict[str, dict] = {}
        for r in results:
            k = keyfunc(r)
            e = d.setdefault(k, {"stocks": 0})
            for kk in keys:
                e.setdefault(kk, 0)
            e["stocks"] += 1
            for kk in keys:
                e[kk] += r[kk]
        return d

    def _print(title: str, agg: dict, header: str) -> None:
        print(f"\n{'='*66}")
        print(f"【{title}】")
        print(f"{'='*66}")
        print(f"  {header:<24}{'股票':>6}{'总样本':>12}{'正样本':>9}"
              f"{'自然率':>8}{'T一字':>8}{'T一字%':>8}{'T开盘%':>8}{'剔后率':>8}")
        for k in sorted(agg):
            v = agg[k]
            t = v["n_total"]
            p = v["n_pos"]
            ty = v["n_t_yizi"]
            to = v["n_t_olimit"]
            tpop = v["n_t_yizi_pos"]
            top = v["n_t_olimit_pos"]
            rem = p - top
            remtot = t - to
            print(f"  {k:<24}{v['stocks']:>6}{t:>12,}{p:>9,}"
                  f"{p/max(t,1)*100:>7.2f}%{ty:>8,}{ty/max(p,1)*100:>7.2f}%"
                  f"{to/max(p,1)*100:>7.2f}%{rem/max(remtot,1)*100:>7.2f}%")

    _print("按数据长度分层 (len(df) 作上市日龄代理)", _agg(lambda r: r["len_bucket"]), "层")
    _print("按板块分层", _agg(lambda r: r["board"]), "板块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
