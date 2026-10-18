"""对比：现役 vpnl_huber_stage1（回归 PnL）vs 实验组 daban（二分类次日涨停）。

在同一「可交易样本」口径下的 OOS AUC 对比，用于决定是否切换实盘。

两个模型目标不同，必须先统一口径才可比：
  · vpnl_huber_stage1 : 回归（数日波段 PnL），21 维精选特征
  · daban             : 二分类（次日收盘涨停），29 维特征（26 旧 + 3 新涨停特征）

统一口径三要素：
  1. 同一评估集：剔除 T 日（信号日/买入日）开盘价已达涨停价的样本（实盘买不进）
  2. 同一标签：y = 1 当且仅当 close[t+1]/close[t] - 1 >= limit（次日收盘涨停）
  3. 同一切分：SPLIT_DATE = 2025-06-01，在 test 集上评估

指标：AUC（排序能力）—— 两个模型输出量纲不同，不可直接比数值；
      另附 Top-k 命中率作为实盘更直观的补充。

实现说明：
  compute_window_features_extended 逐点调用为 O(n)，对全部 sig_idx 遍历即 O(n²)，
  全市场约 41 亿次运算（实测训练脚本约 17 min）。本脚本为快速对比，
  采用「抽样股票 × 每只股票最近 N 个交易日窗口」，在可控时间内给出统计有效结论。

用法：
    venv/bin/python scripts/compare_daban_vs_stage1.py                    # 默认 150 股 × 最近 300 日
    venv/bin/python scripts/compare_daban_vs_stage1.py --limit 300 --recent 500
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tdxfeed"))

from features_5d import get_limit  # noqa: E402
from train_daban_model import SPLIT_DATE, iter_stock_files  # noqa: E402
from _common import compute_window_features_extended  # noqa: E402
from tdxfeed.factors_candidates import compute_candidate_features  # noqa: E402

MODEL_STAGE1 = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt"
MODEL_DABAN = ROOT / "data" / "double_up" / "d_signal_5d_model_daban.txt"

CAND_COLS = ("date", "open", "high", "low", "close", "volume", "amount")

_M_STAGE1 = None
_M_DABAN = None


def _models():
    """worker 内加载两模型（幂等）。"""
    global _M_STAGE1, _M_DABAN
    if _M_STAGE1 is None:
        _M_STAGE1 = lgb.Booster(model_file=str(MODEL_STAGE1))
    if _M_DABAN is None:
        _M_DABAN = lgb.Booster(model_file=str(MODEL_DABAN))
    return _M_STAGE1, _M_DABAN


def process_stock(item: tuple) -> dict | None:
    """单只股票 -> 可交易窗口内的 (date, label, 两模型打分)。"""
    code, fp, recent = item
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    if len(df) < 30:
        return None

    m1, m2 = _models()
    df = df.sort_values("date").reset_index(drop=True)
    limit_val = get_limit(code)
    close = df["close"].values.astype(float)
    openp = df["open"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    n = len(df)

    cols_s1 = m1.feature_name()
    cols_db = m2.feature_name()
    cand_src = [c for c in CAND_COLS if c in df.columns]
    df_pl = pl.from_pandas(df[cand_src].reset_index(drop=True)) if len(cand_src) >= 5 else None

    # 仅取最近 recent 个交易日作为评估窗口，控制 O(n²) 计算量
    start = max(4, n - 1 - recent) if recent and recent > 0 else 4

    rows, n_total, n_excluded, n_pos = [], 0, 0, 0

    for sig_idx in range(start, n - 1):
        cur = close[sig_idx]
        if cur <= 0:
            continue
        n_total += 1
        prev = close[sig_idx - 1]

        # 标签（次日收盘涨停）
        ret_next = close[sig_idx + 1] / cur - 1.0
        is_pos = 1 if ret_next >= (limit_val - 0.001) else 0

        # 可交易口径：T 日开盘价未达涨停价（否则一字/准一字，实盘买不进）
        if prev > 0 and ((openp[sig_idx] / prev - 1.0) >= (limit_val - 0.001)):
            n_excluded += 1
            continue
        if is_pos:
            n_pos += 1

        feat = compute_window_features_extended(df, code, sig_idx=sig_idx)
        if feat is None:
            continue
        row = dict(feat)
        if df_pl is not None:
            cand = compute_candidate_features(df_pl, code, sig_idx=sig_idx)
            if cand:
                row.update(cand)
        row["date"] = str(df["date"].iloc[sig_idx])[:10]
        row["label"] = is_pos
        rows.append(row)

    return {"n_total": n_total, "n_excluded": n_excluded, "n_pos": n_pos,
            "df": pd.DataFrame(rows) if rows else None}


def _auc(y: np.ndarray, s: np.ndarray) -> float:
    """Rank-based AUC（处理并列）。"""
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    sorted_s = s[order]
    ranks = np.empty(len(s), dtype=float)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150, help="抽样股票数(0=全量)")
    ap.add_argument("--recent", type=int, default=300,
                    help="每只股票仅取最近 N 个交易日(0=全历史)")
    ap.add_argument("--workers", type=int, default=4, help="并行进程数")
    args = ap.parse_args()

    files = iter_stock_files(args.limit)
    tasks = [(c, f, args.recent) for c, f in files]
    print(f"对比 {len(files)} 只股票，每只最近 {args.recent} 个交易日 "
          f"workers={args.workers}）...", flush=True)

    records = []
    n_total = n_excluded = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(process_stock, tasks, chunksize=8), 1):
            if not r:
                continue
            n_total += r.get("n_total", 0)
            n_excluded += r.get("n_excluded", 0)
            if r.get("df") is not None:
                records.append(r["df"])
            if i % 100 == 0:
                print(f"  {i}/{len(files)} ...", flush=True)

    if not records:
        print("❌ 无样本", file=sys.stderr)
        return 1

    full = pd.concat(records, ignore_index=True)
    n_keep = len(full)
    n_pos = int(full["label"].sum())

    print(f"\n{'='*64}")
    print("【样本口径】")
    print(f"{'='*64}")
    print(f"  股票数                 : {len(files):,}")
    print(f"  评估窗口样本           : {n_total:,}  (每只最近 {args.recent} 日)")
    print(f"  剔除(T日开盘即涨停)    : {n_excluded:,} ({n_excluded/max(n_total,1)*100:.2f}%)")
    print(f"  可交易样本             : {n_keep:,}")
    print(f"  其中正样本(次日涨停)   : {n_pos:,}")
    print(f"  → 自然正样本率         : {n_pos/max(n_keep,1)*100:.2f}%")

    # 统一打分：两模型各取自己训练时的特征子集
    m1 = lgb.Booster(model_file=str(MODEL_STAGE1))
    m2 = lgb.Booster(model_file=str(MODEL_DABAN))
    full["score_stage1"] = m1.predict(full[m1.feature_name()])
    full["score_daban"] = m2.predict(full[m2.feature_name()])

    test_df = full[full["date"] >= SPLIT_DATE]
    n_test = len(test_df)

    print(f"\n{'='*64}")
    print(f"【OOS AUC 对比】(test: date >= {SPLIT_DATE}, n={n_test:,})")
    print(f"{'='*64}")
    print(f"  {'模型':<30}{'test n':>10}{'AUC':>10}")

    out = {}
    for tag, col in (("vpnl_huber_stage1(现役)", "score_stage1"),
                     ("daban(实验组)", "score_daban")):
        y = test_df["label"].to_numpy()
        s = test_df[col].to_numpy()
        a = _auc(y, s)
        out[tag] = a
        print(f"  {tag:<30}{n_test:>10,}{a:>10.4f}")

    keys = list(out.keys())
    if len(keys) == 2:
        d = out[keys[1]] - out[keys[0]]
        better = keys[1] if d > 0 else keys[0]
        print(f"\n  差值 (daban - stage1) = {d:+.4f}")
        print(f"  更优: {better}")
        ok = "达标" if min(out.values()) > 0.60 else "未达标"
        print(f"  验收线 AUC > 0.60: 两者均{ok}")

    # 落盘对比结果
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out_fp = ROOT / "data" / "double_up" / f"compare_stage1_vs_daban_{ts}.csv"
    full.to_csv(out_fp, index=False)
    print(f"\n  样本明细: {out_fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
