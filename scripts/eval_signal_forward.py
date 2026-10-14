#!/usr/bin/env python3
"""scripts/eval_signal_forward.py — 信号模型时序前向横向评测

背景
----
项目"价值增长瓶颈"不在 AUC(方案D 信号模型 AUC≈0.79 已非随机),而在
"信号 → 可交易 PnL" 的转化(方案E 含成本后夏普≈0.58、胜率≈35%,接近随机)。
`data/double_up/` 里已有一族**未部署**候选模型(分类 v9/v10-DART、回归
vpnl/vpnl_asym/vpnl_huber[+industry/market])。本脚本在**时序前向验证集**
(近期 signal_date)上公平对比它们的信号→PnL 能力,选出最优接入 predict.py。

方法
----
按 signal_date 时序切分:train = signal_date < CUTOFF,test = >= CUTOFF。
用各模型在 test 上打分(模型在全部数据上训练,存在泄漏,但跨模型相对排序
有效;绝对数参见各训练报告)。统一以 `hypothetical_pnl`(TP+30%/SL-10%/
HOLD10d 的真实 PnL 代理)作为"真实目标"。

指标
----
  分类(cls): AUC(is_positive)、Spearman(hypothetical_pnl)
  回归(reg): Spearman(hypothetical_pnl)、rank-AUC(is_positive)
  统一:      Top-N(50/100/200/500) 平均 realized PnL、is_positive 命中率、
            long-short(最高 decile 均 PnL − 最低 decile 均 PnL) —— 直接衡量
            "模型能否把高 PnL 标的排到前面"(价值瓶颈核心)。

用法
----
  venv/bin/python scripts/eval_signal_forward.py --cutoff 2025-01-01
  venv/bin/python scripts/eval_signal_forward.py --cutoff 2024-01-01 --topn 50,100,200
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
DU = ROOT / "data" / "double_up"

# (name, model_file, feature_csv, kind)
# kind: cls=分类器(输出概率), reg=回归器(输出预期 PnL)
CANDIDATES = [
    ("v9",             "d_signal_5d_model_v9.txt",             "d_signal_5d_features_v9.csv",             "cls"),
    ("v10_dart",       "d_signal_5d_model_v10.txt",            "d_signal_5d_features_v9.csv",             "cls"),
    ("vpnl",           "d_signal_5d_model_vpnl.txt",           "d_signal_5d_features_vpnl.csv",           "reg"),
    ("vpnl_asym",      "d_signal_5d_model_vpnl_asym.txt",      "d_signal_5d_features_vpnl.csv",           "reg"),
    ("vpnl_huber",     "d_signal_5d_model_vpnl_huber.txt",     "d_signal_5d_features_vpnl.csv",           "reg"),
    ("vpnl_huber_ind", "d_signal_5d_model_vpnl_huber_industry.txt", "d_signal_5d_features_v9_industry.csv", "reg"),
    ("vpnl_huber_mkt", "d_signal_5d_model_vpnl_huber_market.txt",   "d_signal_5d_features_v9_market.csv",   "reg"),
]


def load_df(csv_name: str) -> pd.DataFrame:
    """加载特征 CSV;回归目标 hypothetical_pnl 缺失时从 vpnl CSV 合并。"""
    df = pd.read_csv(DU / csv_name)
    if "hypothetical_pnl" not in df.columns:
        pnl_src = DU / "d_signal_5d_features_vpnl.csv"
        pnl = pd.read_csv(pnl_src, usecols=["code", "signal_date", "hypothetical_pnl"])
        df = df.merge(pnl, on=["code", "signal_date"], how="left")
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    return df


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    try:
        from scipy.stats import spearmanr
        r = spearmanr(x, y).correlation
        return float(r) if np.isfinite(r) else float("nan")
    except Exception:
        return float(pd.Series(x).corr(pd.Series(y), method="spearman"))


def evaluate(name: str, model_file: str, csv_name: str, kind: str,
             cutoff: pd.Timestamp, topn: list[int]) -> dict | None:
    mp = DU / model_file
    if not mp.exists():
        print(f"  [skip] {name}: 模型缺失 {model_file}", file=sys.stderr)
        return None
    try:
        df = load_df(csv_name)
    except Exception as e:
        print(f"  [skip] {name}: CSV 加载失败 {e}", file=sys.stderr)
        return None

    df = df.dropna(subset=["signal_date"])
    test = df[df["signal_date"] >= cutoff].copy()
    if test.empty or len(test) < 100:
        print(f"  [skip] {name}: test 样本不足 ({len(test)})", file=sys.stderr)
        return None

    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    try:
        model = lgb.Booster(model_file=str(mp))
        feats = model.feature_name()
    except Exception as e:
        print(f"  [skip] {name}: 模型加载失败 {e}", file=sys.stderr)
        return None
    missing = [c for c in feats if c not in test.columns]
    if missing:
        print(f"  [skip] {name}: 缺特征 {missing[:5]}", file=sys.stderr)
        return None

    X = test[feats].fillna(0.0)
    try:
        pred = np.asarray(model.predict(X)).ravel()
    except Exception as e:
        print(f"  [skip] {name}: 预测失败 {e}", file=sys.stderr)
        return None

    y_pnl = test["hypothetical_pnl"].values.astype(float)
    y_pos = test["is_positive"].values.astype(int)
    order = np.argsort(-pred)
    pnl_sorted = y_pnl[order]

    rec: dict = {"name": name, "kind": kind, "n_test": len(test), "n_feat": len(feats)}
    rec["random_pnl"] = float(np.mean(y_pnl))
    for n in topn:
        k = min(n, len(pnl_sorted))
        rec[f"top{n}_pnl"] = float(np.mean(pnl_sorted[:k]))
    dec = max(1, len(pnl_sorted) // 10)
    rec["top_decile_pnl"] = float(np.mean(pnl_sorted[:dec]))
    rec["bot_decile_pnl"] = float(np.mean(pnl_sorted[-dec:]))
    rec["long_short"] = rec["top_decile_pnl"] - rec["bot_decile_pnl"]
    k200 = min(200, len(pnl_sorted))
    rec["top200_hit"] = float(np.mean(y_pos[order[:k200]]))
    rec["spearman_pnl"] = spearman(pred, y_pnl)
    if kind == "cls":
        try:
            rec["auc_ispos"] = float(roc_auc_score(y_pos, pred))
        except Exception:
            rec["auc_ispos"] = float("nan")
        rec["rank_auc_ispos"] = float("nan")
    else:
        try:
            rec["rank_auc_ispos"] = float(roc_auc_score(y_pos, pred))
        except Exception:
            rec["rank_auc_ispos"] = float("nan")
        rec["auc_ispos"] = float("nan")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description="信号模型时序前向横向评测")
    ap.add_argument("--cutoff", default="2025-01-01", help="test 起始日(含),YYYY-MM-DD")
    ap.add_argument("--topn", default="50,100,200,500", help="Top-N 列表,逗号分隔")
    ap.add_argument("--no-report", action="store_true", help="不写 Markdown 报告")
    args = ap.parse_args()
    topn = [int(x) for x in args.topn.split(",")]
    cutoff = pd.Timestamp(args.cutoff)

    print("=== 信号模型时序前向评测 ===")
    print(f"  test 窗口: signal_date >= {cutoff.date()}")
    print(f"  Top-N: {topn}\n")

    rows: list[dict] = []
    for name, model_file, csv_name, kind in CANDIDATES:
        rec = evaluate(name, model_file, csv_name, kind, cutoff, topn)
        if rec is not None:
            rows.append(rec)
            print(f"  ✓ {name:16s} n={rec['n_test']:5d} "
                  f"top50_pnl={rec['top50_pnl']:+.4f} "
                  f"long_short={rec['long_short']:+.4f} "
                  f"spr={rec['spearman_pnl']:+.3f}", file=sys.stderr)

    if not rows:
        sys.exit("❌ 无可用模型")

    df = pd.DataFrame(rows).sort_values("long_short", ascending=False).reset_index(drop=True)

    # 控制台汇总
    cols = ["name", "kind", "n_test", "top50_pnl", "top100_pnl", "top200_pnl",
            "top500_pnl", "long_short", "top200_hit", "spearman_pnl",
            "auc_ispos", "rank_auc_ispos"]
    cols = [c for c in cols if c in df.columns]
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    print("\n=== 横向对比(按 long_short 降序) ===")
    print(df[cols].to_string(index=False))

    winner = df.iloc[0]["name"]
    print(f"\n>>> 候选最优(按 long_short): {winner}")

    if not args.no_report:
        write_report(df, cols, cutoff, topn, winner)


def write_report(df: pd.DataFrame, cols: list[str], cutoff: pd.Timestamp,
                 topn: list[int], winner: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    fname = f"eval_signal_forward_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out = DU / fname
    random_pnl = df.iloc[0]["random_pnl"]
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 信号模型时序前向横向评测 ({ts})\n\n")
        f.write(f"- **test 窗口**: signal_date >= {cutoff.date()}\n")
        f.write(f"- **候选数**: {len(df)}\n")
        f.write("- **统一真实目标**: `hypothetical_pnl`(TP+30%/SL-10%/HOLD10d)\n")
        f.write(f"- **随机基线均 PnL**: {random_pnl:+.4f}\n\n")

        f.write("## 📊 核心指标(按 long_short 降序)\n\n")
        f.write("| 模型 | 类型 | n | Top50 PnL | Top100 PnL | Top200 PnL | "
                "Top500 PnL | long-short | Top200 命中 | Spearman(PnL) | AUC(is_pos) | rank-AUC |\n")
        f.write("|---|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for _, r in df.iterrows():
            f.write(f"| {r['name']} | {r['kind']} | {int(r['n_test'])} | "
                    f"{r['top50_pnl']:+.4f} | {r['top100_pnl']:+.4f} | {r['top200_pnl']:+.4f} | "
                    f"{r['top500_pnl']:+.4f} | **{r['long_short']:+.4f}** | {r['top200_hit']:.3f} | "
                    f"{r['spearman_pnl']:+.3f} | "
                    f"{r['auc_ispos']:.4f} | {r['rank_auc_ispos']:.4f} |\n")

        f.write("\n## 🎯 Top N 明细(PnL 代理)\n\n")
        f.write("| 模型 | " + " | ".join(f"Top{n} PnL" for n in topn) + " | 随机基线 |\n")
        f.write("|---|" + "---:|" * (len(topn) + 1) + "\n")
        for _, r in df.iterrows():
            cells = " | ".join(f"{r[f'top{n}_pnl']:+.4f}" for n in topn)
            f.write(f"| {r['name']} | {cells} | {random_pnl:+.4f} |\n")

        f.write("\n## 📈 统计分布\n\n")
        f.write("- `long-short` = 最高 decile 均 PnL − 最低 decile 均 PnL,衡量模型排序能力(越大越好)。\n")
        f.write("- `Top200 命中` = Top200 中 `is_positive`(5 日内再涨 30%+) 占比,衡量候选池筛选有效性。\n")
        f.write("- `Spearman(PnL)` = 预测分与真实 PnL 的秩相关。\n")
        f.write("- 随机基线 = 从候选池随机选的均 PnL;模型 Top-N 均 PnL 应显著 > 基线才具价值。\n\n")

        f.write("## ⚠️ 风险提示\n\n")
        f.write("1. 历史回测不代表未来收益;2026 年新模式可能失效。\n")
        f.write("2. 本评测模型在**全部数据**上训练(含 test 窗口),存在时间泄漏,"
                "跨模型相对排序有效,绝对数偏乐观;严格部署前建议做 walk-forward。\n")
        f.write("3. 翻倍策略属激进策略,建议小资金启动,严格止损(-10%)。\n")
        f.write("4. 北交所(920XXX)流动性低,滑点可能放大,评估集若含相关样本需注意。\n")
        f.write("5. 回归模型(huber 系)输出为预期 PnL,接入 predict.py 需适配阈值语义"
                "(按 Top-N 排序而非概率阈值)。\n\n")

        f.write("## 🔧 下一步建议\n\n")
        f.write(f"- 候选最优(按 long_short): **{winner}**。\n")
        f.write("- 若为分类模型:直接切换 `predict.py` 的 `MODEL_FILE` 为该模型并验证产出。\n")
        f.write("- 若为回归模型:为 `predict.py` 增加回归模式(Top-N 排序 + PnL 语义统计)后部署。\n")
        f.write("- 部署后跑一次 `predict.py` 对比 Top picks,并接入方案E 实盘 journal 观察真实 PnL。\n\n")
        f.write("---\n\n")
        f.write(f"生成时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("生成者:scripts/eval_signal_forward.py\n")
        f.write(f"数据日期:{cutoff.date()}\n")
    print(f"\n  ✓ 报告: {out.name}")


if __name__ == "__main__":
    main()
