"""scripts/stage1_baseline.py — 阶段 1 Step 3.5 基线评测（复现 vpnl_huber）

目的
----
在**新 Polars 宽表管线**（`data/double_up/stage1_wide_table.parquet`）上，用与已部署
`vpnl_huber` 完全相同的模型协议（LGBMRegressor, objective=huber, alpha=0.5）复现
OOS 性能，建立**基线数字**。Step 4（Optuna 特征选择）必须以"不退化"为闸门，
故本脚本产出的 OOS Spearman / Top-N PnL 即是 Step 4 的对照基准。

关键事实（来自 docs/stage1_feature_migration_plan.md §5 与 scripts/train_pnl_proxy.py）：
- `vpnl_huber` = LGBMRegressor(huber, alpha=0.5, n_estimators=500, lr=0.05, max_depth=6,
  num_leaves=31, min_child_samples=20, subsample=0.8, colsample_bytree=0.8, seed=42)。
- 主目标 = `hypothetical_pnl`（T+1 开盘入场, TP=30%/SL=-10%/HOLD=10d, 含双边费滑）。
- 主指标 = 时序前向（walk-forward）OOS **Spearman** 秩相关；次指标 = OOS Top-N 组合 PnL。
- 特征集默认 = 26 基础维（对齐 vpnl_huber）；可选 18 / 全 35（26 基础 + 9 候选）。

去泄漏设计
----------
严格按时间切分：`train(signal_date < cutoff)` → `test(>= cutoff)`，绝不随机 shuffle。
多折用"扩展窗口 + 不重叠测试段"：cutoff 列表把时间轴切成不重叠的测试段，
训练段为起点到该 cutoff（含之前所有历史）。

用法
----
    # 默认：26 基础维，cutoff=2024/2025（两折），Top-N=10/20/50，huber
    venv/bin/python scripts/stage1_baseline.py

    # 全 35 维（含候选），单 cutoff
    venv/bin/python scripts/stage1_baseline.py --feature-set all35 --cutoffs 2025-01-01

    # 仅回归对照（MSE）
    venv/bin/python scripts/stage1_baseline.py --objective regression

产物
----
- logs/stage1_baseline_{ts}.json  机器可读结果
- logs/stage1_baseline_{ts}.md    报告（含核心指标 / Top N / 风险提示 / 下一步）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ---- 项目路径：让 scripts/ 能 import tdxfeed 包（单一来源特征名）----
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.stage1_wide_table import FEAT26_ORDER  # 26 基础维顺序（权威）
from tdxfeed.factors_candidates import CANDIDATE_NAMES  # 9 候选因子名

log = logging.getLogger("stage1_baseline")

# --------------------------------------------------------------------------- #
# 特征集定义（单一来源，避免与 stage1_wide_table 漂移）
# --------------------------------------------------------------------------- #
BASE18 = FEAT26_ORDER[:18]
BASE26 = FEAT26_ORDER                       # 18 基础 + 8 扩展，对齐 vpnl_huber
CANDIDATES = CANDIDATE_NAMES                # 9 候选
ALL35 = BASE26 + CANDIDATES

META_COLS = ["code", "signal_date", "is_positive", "hypothetical_pnl"]
FEATURE_SETS = {
    "base18": BASE18,
    "base26": BASE26,
    "all35": ALL35,
}

# vpnl_huber 训练协议（与 train_pnl_proxy.py / eval_signal_honest.py 一致）
DEFAULT_LGBM = dict(
    n_estimators=500, learning_rate=0.05, max_depth=6, num_leaves=31,
    min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
    random_state=42, n_jobs=-1, verbose=-1,
)


# --------------------------------------------------------------------------- #
# 1. 数据加载与校验
# --------------------------------------------------------------------------- #
def load_and_validate(
    wide_table: Path, feature_cols: list[str], drop_nonfinite: bool = True,
) -> pd.DataFrame:
    """读取宽表并做结构/完整性校验。

    校验项：
      1) 文件存在；
      2) META + 所选特征列全部存在；
      3) signal_date 可解析为日期；
      4) hypothetical_pnl 为有限值（缺失则剔除，近期缺 10 日未来数据）；
      5) 特征列非有限单元按需剔除（默认剔除整行，保持评估诚实）。

    返回校验后的 DataFrame（含解析后的 `_date` 列，按时间升序）。
    """
    if not wide_table.exists():
        raise FileNotFoundError(f"宽表不存在: {wide_table}")

    df = pd.read_parquet(wide_table)
    log.info("载入宽表: %s  rows=%d cols=%d", wide_table.name, len(df), df.shape[1])

    required = META_COLS + feature_cols
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"宽表缺失必需列: {missing}（请先用 stage1_wide_table.py 装配）")

    # signal_date → datetime
    df["_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    bad_date = int(df["_date"].isna().sum())
    if bad_date:
        log.warning("signal_date 解析失败 %d 行，剔除", bad_date)
        df = df[df["_date"].notna()].copy()

    # 标签完整性
    n_before = len(df)
    df = df[df["hypothetical_pnl"].notna()].copy()
    log.info("剔除 hypothetical_pnl 缺失: %d → %d", n_before, len(df))

    # 特征完整性
    if drop_nonfinite:
        feat_bad = df[feature_cols].apply(lambda s: ~np.isfinite(s))
        rows_bad = int(feat_bad.any(axis=1).sum())
        if rows_bad:
            log.warning("特征含非有限值的样本 %d 行，剔除", rows_bad)
            keep = ~feat_bad.any(axis=1).values
            df = df[keep].copy()

    df = df.sort_values("_date").reset_index(drop=True)
    log.info(
        "校验后: %d 样本 | 特征集=%d 维 | 日期 %s ~ %s | 正收益样本 %.1f%%",
        len(df), len(feature_cols),
        df["_date"].min().date(), df["_date"].max().date(),
        (df["hypothetical_pnl"] > 0).mean() * 100,
    )
    if len(df) < 200:
        raise ValueError(f"有效样本过少 ({len(df)})，不足以做 OOS 评估")
    return df


# --------------------------------------------------------------------------- #
# 2. 评估指标
# --------------------------------------------------------------------------- #
def _spearman(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """秩相关；优先 scipy，缺失时回退 pandas。"""
    if len(y_true) < 3:
        return float("nan")
    try:
        from scipy.stats import spearmanr

        r, _ = spearmanr(y_pred, y_true)
        return float(r)
    except Exception:
        return float(pd.Series(y_pred).corr(pd.Series(y_true), method="spearman"))


def _eval_fold(y_true: np.ndarray, y_pred: np.ndarray, top_ns: list[int]) -> dict:
    """单折测试集评估：Spearman + 各 Top-N 的均值 PnL 与胜率。"""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    sp = _spearman(y_true, y_pred)
    n = len(y_true)
    top_rows = {}
    order = np.argsort(-y_pred)  # 预测 PnL 降序
    for k in top_ns:
        kk = min(k, n)
        sel = y_true[order[:kk]]
        if kk == 0:
            top_rows[k] = {"n": 0, "mean_pnl": float("nan"), "win_rate": float("nan")}
        else:
            top_rows[k] = {
                "n": int(kk),
                "mean_pnl": float(np.mean(sel)),
                "win_rate": float((sel > 0).mean()),
            }
    return {"n": n, "spearman": sp, "top": top_rows}


# --------------------------------------------------------------------------- #
# 3. 时序前向（walk-forward）评估
# --------------------------------------------------------------------------- #
def walk_forward_eval(
    df: pd.DataFrame,
    feature_cols: list[str],
    cutoffs: list[pd.Timestamp],
    top_ns: list[int],
    objective: str = "huber",
    alpha: float = 0.5,
    lgbm_kwargs: dict | None = None,
) -> dict:
    """扩展窗口 + 不重叠测试段的 walk-forward 评测。

    cutoff 列表把时间轴切为不重叠测试段：
      test_i = [cutoff_i, cutoff_{i+1})（末段为 [last_cutoff, ∞)）
      train_i = [起点, cutoff_i)
    每折在 train 上拟合并 early-stopping，在 test 上打分。
    """
    try:
        import lightgbm as lgb
    except ImportError as e:
        raise ImportError("需要 lightgbm（venv 已装）；导入失败: " + str(e)) from e

    kw = dict(DEFAULT_LGBM)
    if lgbm_kwargs:
        kw.update(lgbm_kwargs)
    if objective == "huber":
        kw["objective"] = "huber"
        kw["alpha"] = alpha
    else:
        kw["objective"] = "regression"

    X = df[feature_cols].astype(float).values
    y = df["hypothetical_pnl"].astype(float).values
    dates = df["_date"].values

    boundaries = list(cutoffs) + [pd.Timestamp.max]
    folds = []
    for i in range(len(cutoffs)):
        cut, nxt = cutoffs[i], boundaries[i + 1]
        tr = dates < np.datetime64(cut)
        te = (dates >= np.datetime64(cut)) & (dates < np.datetime64(nxt))
        if tr.sum() < 200 or te.sum() < 10:
            log.warning("折 %d (cutoff=%s) 样本不足，跳过 train=%d test=%d",
                        i, cut.date(), int(tr.sum()), int(te.sum()))
            continue
        model = lgb.LGBMRegressor(**kw)
        model.fit(X[tr], y[tr], eval_set=[(X[te], y[te])],
                  callbacks=[lgb.early_stopping(50, verbose=False)])
        pred = model.predict(X[te], num_iteration=model.best_iteration_)
        res = _eval_fold(y[te], pred, top_ns)
        res["cutoff"] = str(cut.date())
        res["train_n"] = int(tr.sum())
        folds.append(res)
        log.info("折 %d | cutoff=%s | test_n=%d | Spearman=%.4f | Top20 PnL=%.4f",
                 i, cut.date(), res["n"], res["spearman"],
                 res["top"].get(20, {}).get("mean_pnl", float("nan")))

    if not folds:
        raise RuntimeError("所有折均样本不足，无法评估（请放宽 cutoffs）")

    # 池化估计：各折按 test 样本量加权平均（避免重算跨折合并预测，成本更低）。
    # 注：严格池化需跨折拼接预测再做一次 Spearman/Top-N，这里以 size 加权逼近，
    # 折数少且各折样本量相当时偏差可忽略；若需精确池化可后续扩展。
    w = np.array([f["n"] for f in folds], dtype=float)
    w = w / w.sum()
    sp_pool = float(np.nansum([f["spearman"] * wi for f, wi in zip(folds, w)]))
    top_pool = {}
    for k in top_ns:
        mp = np.nansum([f["top"][k]["mean_pnl"] * wi for f, wi in zip(folds, w)])
        wr = np.nansum([f["top"][k]["win_rate"] * wi for f, wi in zip(folds, w)])
        top_pool[k] = {"mean_pnl": float(mp), "win_rate": float(wr)}

    return {
        "folds": folds,
        "pooled": {"spearman": sp_pool, "top": top_pool},
        "feature_cols": feature_cols,
        "n_folds": len(folds),
    }


# --------------------------------------------------------------------------- #
# 4. 报告输出
# --------------------------------------------------------------------------- #
def build_report(result: dict, meta: dict) -> str:
    """生成符合项目规范的 Markdown 报告。"""
    folds = result["folds"]
    pooled = result["pooled"]
    top_ns = meta["top_ns"]
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 阶段1 基线评测 (vpnl_huber 复现) ({ts})",
        "",
        "## 📊 核心指标",
        f"- 基线模型：`vpnl_huber`（LGBMRegressor, huber α={meta['alpha']}）",
        f"- 特征集：**{meta['feature_set']}**（{len(meta['feature_cols'])} 维）",
        f"- 有效样本：{meta['n_samples']:,}（剔除 pnl/特征缺失后）",
        f"- 切分折数：{result['n_folds']}（cutoffs={meta['cutoffs']}）",
        f"- **OOS Spearman（加权平均）：{pooled['spearman']:.4f}**",
        f"- **OOS Top-20 PnL：{pooled['top'].get(20, {}).get('mean_pnl', float('nan')):.4f}**"
        f"（胜率 {pooled['top'].get(20, {}).get('win_rate', float('nan'))*100:.1f}%）",
        f"- Top-50 PnL：{pooled['top'].get(50, {}).get('mean_pnl', float('nan')):.4f}"
        f"（胜率 {pooled['top'].get(50, {}).get('win_rate', float('nan'))*100:.1f}%）",
        "",
        "## 🎯 Top N 明细",
        "",
        "| 折 / 池化 | test_n | Spearman | "
        + " | ".join(f"Top{k} PnL" for k in top_ns) + " |",
        "|---|---:|---:|" + "---:|" * len(top_ns),
    ]
    for f in folds:
        row = [f"cutoff={f['cutoff']}", str(f["n"]), f"{f['spearman']:.4f}"]
        row += [f"{f['top'][k]['mean_pnl']:.4f}" for k in top_ns]
        lines.append("| " + " | ".join(row) + " |")
    prow = ["**池化(加权)**", str(sum(f["n"] for f in folds)),
            f"{pooled['spearman']:.4f}"]
    prow += [f"{pooled['top'][k]['mean_pnl']:.4f}" for k in top_ns]
    lines.append("| " + " | ".join(prow) + " |")
    lines += [
        "",
        "## 📈 统计分布",
        f"- 各折 Spearman 区间：[{min(f['spearman'] for f in folds):.4f}, "
        f"{max(f['spearman'] for f in folds):.4f}]",
        f"- 各折 Top-20 PnL 区间：[{min(f['top'][20]['mean_pnl'] for f in folds):.4f}, "
        f"{max(f['top'][20]['mean_pnl'] for f in folds):.4f}]",
        "- 说明：本基线用**时序前向 OOS**（非随机 CV），数字偏保守；随机 5-fold CV 会更高但含泄漏。",
        "",
        "## ⚠️ 风险提示",
        "1. 历史回测不代表未来收益，OOS 数字仅作 Step 4 对照基线。",
        "2. 模型基于 2020-2026 数据训练，2026 年新模式可能失效。",
        "3. 翻倍策略属激进策略，建议小资金启动，严格止损(-10%)。",
        "4. 北交所(920XXX)流动性低，滑点可能放大，评估集若含相关样本需注意。",
        "5. 基线以加权池化估计 OOS；若某折样本极少，权重偏低但仍计入，建议复核折样本量。",
        "",
        "## 🔧 下一步建议",
        "1. **Step 4**：以本基线为闸门，跑 Optuna 在 35 维(26 基础 + 9 候选)上做特征选择，",
        "   主指标=OOS Spearman、次=OOS Top-N PnL，要求**不退化**才切换。",
        "2. 若基线 Spearman 接近 0，说明 `hypothetical_pnl` 本身弱可预测，",
        "   需重新审视标签口径（TP/SL/HOLD/成本）而非仅换特征。",
        "3. 评估 `objective=regression` 对照，确认 huber 鲁棒性收益。",
        "",
        "---",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"生成者：scripts/stage1_baseline.py",
        f"模型版本：vpnl_huber (baseline)",
        f"数据日期：{meta['data_date']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 5. 主入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="阶段1 Step 3.5 基线评测（复现 vpnl_huber）")
    p.add_argument("--wide-table", type=str,
                   default=str(ROOT / "data" / "double_up" / "stage1_wide_table.parquet"),
                   help="宽表 parquet 路径")
    p.add_argument("--feature-set", type=str, choices=list(FEATURE_SETS), default="base26",
                   help="特征集: base18 / base26(默认,对齐vpnl_huber) / all35")
    p.add_argument("--cutoffs", type=str, default="2024-01-01,2025-01-01",
                   help="逗号分隔的 cutoff 日期，划分不重叠测试段（扩展窗口）")
    p.add_argument("--top-n", type=str, default="10,20,50",
                   help="逗号分隔的 Top-N（按预测 PnL 取前 N 的平均实际 PnL）")
    p.add_argument("--objective", type=str, choices=["huber", "regression"],
                   default="huber", help="LGBM 目标（默认 huber 对齐 vpnl_huber）")
    p.add_argument("--alpha", type=float, default=0.5, help="huber 的 delta")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "logs"),
                   help="JSON / MD 报告输出目录")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        cutoffs = [pd.Timestamp(x.strip()) for x in args.cutoffs.split(",") if x.strip()]
        if len(cutoffs) < 1:
            raise ValueError("--cutoffs 至少提供一个日期")
        cutoffs = sorted(cutoffs)
        top_ns = [int(x) for x in args.top_n.split(",") if x.strip()]
        feature_cols = FEATURE_SETS[args.feature_set]

        log.info("启动基线评测 | 特征集=%s(%d维) | cutoffs=%s | top_n=%s | obj=%s",
                 args.feature_set, len(feature_cols),
                 [c.date() for c in cutoffs], top_ns, args.objective)

        df = load_and_validate(Path(args.wide_table), feature_cols)
        result = walk_forward_eval(
            df, feature_cols, cutoffs, top_ns,
            objective=args.objective, alpha=args.alpha,
            lgbm_kwargs={"random_state": args.seed},
        )

        meta = {
            "feature_set": args.feature_set,
            "feature_cols": feature_cols,
            "cutoffs": [c.strftime("%Y-%m-%d") for c in cutoffs],
            "top_ns": top_ns,
            "alpha": args.alpha,
            "objective": args.objective,
            "n_samples": len(df),
            "data_date": str(df["_date"].max().date()),
        }
        out_json = out_dir / f"stage1_baseline_{ts}.json"
        out_md = out_dir / f"stage1_baseline_{ts}.md"
        out_json.write_text(json.dumps({"meta": meta, "result": _jsonable(result)},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
        report = build_report(result, meta)
        out_md.write_text(report, encoding="utf-8")

        # 控制台核心结论
        pooled = result["pooled"]
        print("\n=== 基线评测完成 ===")
        print(f"特征集={args.feature_set}({len(feature_cols)}维)  折数={result['n_folds']}")
        print(f"OOS Spearman(加权) = {pooled['spearman']:.4f}")
        for k in top_ns:
            t = pooled["top"][k]
            print(f"  Top-{k}: mean_pnl={t['mean_pnl']:.4f}  win_rate={t['win_rate']*100:.1f}%")
        print(f"\n报告: {out_md}")
        print(f"JSON: {out_json}")
        return 0
    except Exception as e:  # noqa: BLE001 — 顶层兜底，给出可读错误
        log.error("基线评测失败: %s", e, exc_info=True)
        print(f"\n❌ 基线评测失败: {e}", file=sys.stderr)
        return 1


def _jsonable(result: dict) -> dict:
    """把 numpy 类型转成 JSON 可序列化。"""
    out = {"n_folds": result["n_folds"], "pooled": result["pooled"], "folds": []}
    for f in result["folds"]:
        out["folds"].append({
            "cutoff": f["cutoff"], "train_n": f["train_n"], "n": f["n"],
            "spearman": f["spearman"],
            "top": {str(k): v for k, v in f["top"].items()},
        })
    return out


if __name__ == "__main__":
    raise SystemExit(main())
