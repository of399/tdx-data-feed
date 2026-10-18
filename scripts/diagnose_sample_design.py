"""诊断：打板样本集的正样本率与「不可交易」样本噪声分布（纯只读，不训练不落盘权重）。

背景（缺陷 DEFECT-SAMPLE-2026-10-18-01）：
  现有标签 y = 1 当且仅当 close[t+1]/close[t] - 1 >= limit，只看收盘价，
  导致「一字板」（开盘即涨停、全天封死、实盘买不进）被 100% 判为正样本。

本脚本回答四件事（全部只读 parquet）：
  1. 自然正样本率（正样本 / 全面板样本，未负采样）——对照训练用的 1:5 采样后比例；
  2. 不可交易正样本规模：严格一字板（开=高=低=收）与开盘涨停两种口径；
  3. 噪声是否集中在「数据长度短 = 次新股」层；
  4. 剔除不可交易样本后正样本率落到多少（是否达 1-2%）。

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
    """按股票数据长度分桶（作为上市日龄的代理指标）。"""
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
    """统计单只股票的样本量、正样本数与一字板/开盘涨停分布。"""
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
        "n_total": 0, "n_pos": 0, "n_yizi": 0, "n_open_limit": 0,
    }

    for sig_idx in range(4, n - 1):
        out["n_total"] += 1
        if close[sig_idx] <= 0:
            continue
        ret_next = close[sig_idx + 1] / close[sig_idx] - 1.0
        if ret_next < limit_val - 0.001:
            continue
        out["n_pos"] += 1
        j = sig_idx + 1
        if openp[j] == high[j] == low[j] == close[j]:
            out["n_yizi"] += 1
        if close[sig_idx] > 0 and openp[j] / close[sig_idx] - 1.0 >= limit_val - 0.001:
            out["n_open_limit"] += 1

    return out


def _print_table(title: str, agg: dict, key_header: str) -> None:
    print(f"\n{'='*64}")
    print(f"【{title}】")
    print(f"{'='*64}")
    print(f"  {key_header:<22}{'股票':>6}{'总样本':>12}{'正样本':>10}"
          f"{'自然率':>8}{'一字板':>9}{'一字%':>8}{'开盘涨停':>9}{'开盘%':>8}{'剔除后率':>9}")
    for k in sorted(agg):
        v = agg[k]
        tr = v["pos"] - v["open_limit"]
        print(f"  {k:<22}{v['stocks']:>6}{v['tot']:>12,}{v['pos']:>10,}"
              f"{v['pos']/max(v['tot'],1)*100:>7.2f}%{v['yizi']:>9,}"
              f"{v['yizi']/max(v['pos'],1)*100:>7.2f}%{v['open_limit']:>9,}"
              f"{v['open_limit']/max(v['pos'],1)*100:>7.2f}%"
              f"{tr/max(v['tot'],1)*100:>8.2f}%")


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

    tot = sum(r["n_total"] for r in results)
    pos = sum(r["n_pos"] for r in results)
    yizi = sum(r["n_yizi"] for r in results)
    olimit = sum(r["n_open_limit"] for r in results)

    print(f"\n{'='*64}")
    print("【总体】")
    print(f"{'='*64}")
    print(f"  股票数                  : {len(results):,}")
    print(f"  总样本(全面板,未采样)   : {tot:,}")
    print(f"  正样本(T+1收盘涨停)     : {pos:,}   自然正样本率 = {pos/max(tot,1)*100:.2f}%")
    print(f"  ├ 严格一字板(四价合一) : {yizi:,}   ({yizi/max(pos,1)*100:.2f}% of 正样本)")
    print(f"  ├ 开盘即涨停(open=limit): {olimit:,}   ({olimit/max(pos,1)*100:.2f}% of 正样本)")
    print(f"  └ 可交易涨停(剔除开盘涨停): {pos-olimit:,}   ({(pos-olimit)/max(pos,1)*100:.2f}% of 正样本)")
    print(f"     剔除开盘涨停后自然正样本率 = {(pos-olimit)/max(tot,1)*100:.2f}%")
    print(f"  参考: 训练用比例(1:5 负采样后) ≈ {1/6*100:.1f}%，与 {pos/max(tot,1)*100:.2f}% 非同一口径")

    def _agg(keyfunc) -> dict:
        d: dict[str, dict] = {}
        for r in results:
            k = keyfunc(r)
            e = d.setdefault(k, {"stocks": 0, "tot": 0, "pos": 0, "yizi": 0, "open_limit": 0})
            e["stocks"] += 1
            e["tot"] += r["n_total"]
            e["pos"] += r["n_pos"]
            e["yizi"] += r["n_yizi"]
            e["open_limit"] += r["n_open_limit"]
        return d

    _print_table("按数据长度分层 (len(df) 作为上市日龄代理)",
                 _agg(lambda r: r["len_bucket"]), "层")
    _print_table("按板块分层", _agg(lambda r: r["board"]), "板块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
