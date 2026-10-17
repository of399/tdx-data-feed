"""scripts/stage1_optuna.py — 阶段 1 Step 4 特征选择 + 超参优化（Optuna）

目的
----
在 Step 3.5 已验证的宽表管线上，用 Optuna 做**特征选择 + GBDT 超参调优**，目标是对齐
已部署 `vpnl_huber` 的 **OOS Spearman（主）/ OOS Top-N PnL（次）**，并以 Step 3.5 基线
（Spearman≈0.315 / Top-20≈0.21）为**不退化闸门**。

搜索空间（遵循 docs/stage1_feature_migration_plan.md §5）
-----------------------------------------------
1) 特征 on/off 开关：候选池 = 26 基础 + 9 候选 = 35 维，每维 `Categorical([0,1])`。
   - `--pool all35`：35 维全开关（设计文档默认口径）。
   - `--pool candidates`：26 基础固定 + 仅 9 候选开关（更快更稳，推荐做增量验证）。
2) 模型超参（GBDT 等价映射，非神经网络）：
   - `learning_rate`（学习率，log 区间 1e-3~3e-1）
   - `n_estimators`（boosting 轮数 ≈ NN 的迭代/epoch，搜索上限）
   - `num_leaves`（树宽度 ≈ NN 隐藏单元）
   - `max_depth`（树深度 ≈ NN 层数）
   - `min_child_samples`（叶最小样本，结构正则）
   - `subsample` / `colsample_bytree`（行/列随机采样 ≈ dropout 式正则）
   - `reg_lambda` / `reg_alpha`（L2 / L1 正则系数）
   说明：GBDT 无 batch size；`subsample` 是其在随机梯度意义上的等价随机性控制。

目标函数
--------
每个 trial：采样特征子集 + 超参 → 时序前向 walk-forward（扩展窗口 + 不重叠测试段）训练
LightGBM(huber, α=0.5)，返回 **加权平均 OOS Spearman**（最大化）。剪枝信号用 in-fold
验证 R² 经 `trial.report` 上报，交给 `MedianPruner`。

Study 配置（§5 / §7）
--------------------
- direction=maximize；sampler=`TPESampler(seed=42, multivariate=True)`；
- pruner=`MedianPruner(n_warmup_steps=15, n_min_trials=20)`；
- storage=`sqlite:///logs/stage1_optuna.db`（load_if_exists，可续跑）；
- n_trials=150（可调），支持 `--timeout` 早停与 `--n-jobs` 并行。

产物
----
- logs/stage1_optuna_best.json   最佳超参 + 特征掩码
- logs/stage1_optuna_trials.csv  优化历史（trials_dataframe）
- logs/stage1_optuna_{param_importances,parallel,slice,contour}.html  可视化
- logs/stage1_optuna_best_model.txt  最佳模型权重（在全集上 refit）
- docs/stage1_optuna_report.md   报告（核心指标 / Top 试验 / 因子入选频率 / 风险 / 下一步）

用法
----
    # 默认：all35 开关 + GBDT 超参，150 trial，sqlite 续跑
    venv/bin/python scripts/stage1_optuna.py

    # 更快更稳：仅 9 候选开关（base26 固定），30 trial 烟测
    venv/bin/python scripts/stage1_optuna.py --pool candidates --n-trials 30 --smoke

    # 续跑（同一 study 名，自动 load_if_exists）
    venv/bin/python scripts/stage1_optuna.py --n-trials 50
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lightgbm as lgb
import optuna
from optuna.pruners import MedianPruner
from optuna.samplers import TPESampler
from stage1_baseline import (  # noqa: E402
    ALL35,
    BASE26,
    CANDIDATES,
    DEFAULT_LGBM,
    _eval_fold,
    load_and_validate,
)

# 复用 Step 3.5 的基础设施（单一来源，保证接口一致）

log = logging.getLogger("stage1_optuna")

# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #
STORAGE = f"sqlite:///{(ROOT / 'logs' / 'stage1_optuna.db').as_posix()}"
STUDY_NAME = "stage1_feature_sel"
DEFAULT_CUTOFFS = ["2024-01-01", "2025-01-01"]
DEFAULT_TOP_NS = [10, 20, 50]
DEFAULT_N_TRIALS = 150
PRUNER = MedianPruner(n_warmup_steps=15, n_min_trials=20)


# --------------------------------------------------------------------------- #
# 搜索空间
# --------------------------------------------------------------------------- #
def suggest_feature_mask(trial: optuna.trial.Trial, pool: str) -> list[str]:
    """采样特征子集。

    pool='all35'：35 维全部 on/off 开关（设计文档口径）。
    pool='candidates'：26 基础固定 + 9 候选 on/off 开关（更稳，推荐增量验证）。
    """
    if pool == "candidates":
        selected = list(BASE26)
        for c in CANDIDATES:
            if trial.suggest_categorical(f"f__{c}", [0, 1]) == 1:
                selected.append(c)
        return selected
    # all35
    selected = []
    for name in ALL35:
        if trial.suggest_categorical(f"f__{name}", [0, 1]) == 1:
            selected.append(name)
    return selected


def suggest_hyperparams(trial: optuna.trial.Trial, max_estimators: int) -> dict:
    """采样 GBDT 超参（NN 超参的 GBDT 等价映射，详见模块 docstring）。"""
    return {
        "learning_rate": trial.suggest_float("learning_rate", 1e-3, 3e-1, log=True),
        "n_estimators": trial.suggest_int("n_estimators", min(80, max_estimators), max_estimators),
        "num_leaves": trial.suggest_int("num_leaves", 15, 63),
        "max_depth": trial.suggest_int("max_depth", 3, 10),
        "min_child_samples": trial.suggest_int("min_child_samples", 10, 60),
        "subsample": trial.suggest_float("subsample", 0.6, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-2, 10.0, log=True),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-3, 10.0, log=True),
    }


# --------------------------------------------------------------------------- #
# 带剪枝信号的 walk-forward 训练
# --------------------------------------------------------------------------- #
def _pruning_callback(trial: optuna.trial.Trial, counter: list[int], interval: int = 25):
    """LightGBM 回调：每隔 interval 轮，把 in-fold 验证表现上报给 Optuna 用于剪枝。

    上报值取验证集指标的负值（损失越低越好 → 负值越高越好，与 maximize 方向一致）。
    `counter` 为可变步计数器，保证 walk-forward 多折之间 step 不重复（否则同 trial
    折间 step 归零会触发 "already reported" 并扰乱 MedianPruner）。
    """

    def _cb(env):
        if env.iteration % interval != 0:
            return
        if not getattr(env, "evaluation_result_list", None):
            return
        step = counter[0]
        counter[0] += 1
        for _data, metric, value, _better in env.evaluation_result_list:
            trial.report(-float(value), step=step)
            break

    return _cb


def _walk_forward_trial(
    df: pd.DataFrame, feature_cols: list[str], cutoffs: list[pd.Timestamp],
    top_ns: list[int], hyperparams: dict, trial: optuna.trial.Trial,
) -> dict:
    """单 trial 的时序前向评估（与 stage1_baseline 同源，增加剪枝上报）。"""
    X = df[feature_cols].astype(float).values
    y = df["hypothetical_pnl"].astype(float).values
    dates = df["_date"].values
    boundaries = list(cutoffs) + [pd.Timestamp.max]

    params = dict(DEFAULT_LGBM)
    params.update(hyperparams)
    params["objective"] = "huber"
    params["alpha"] = 0.5

    folds, w_sum = [], 0.0
    step_counter = [0]  # 跨折共享，保证 trial.report 的 step 单调递增
    for i in range(len(cutoffs)):
        cut, nxt = cutoffs[i], boundaries[i + 1]
        tr = dates < np.datetime64(cut)
        te = (dates >= np.datetime64(cut)) & (dates < np.datetime64(nxt))
        if tr.sum() < 200 or te.sum() < 10:
            continue
        model = lgb.LGBMRegressor(**params)
        model.fit(
            X[tr], y[tr], eval_set=[(X[te], y[te])],
            callbacks=[
                lgb.early_stopping(50, verbose=False),
                lgb.log_evaluation(0),
                _pruning_callback(trial, step_counter),
            ],
        )
        pred = model.predict(X[te], num_iteration=model.best_iteration_)
        res = _eval_fold(y[te], pred, top_ns)
        res["cutoff"] = str(cut.date())
        res["train_n"] = int(tr.sum())
        folds.append(res)
        w_sum += res["n"]

    if not folds:
        raise optuna.TrialPruned("所有折样本不足")

    w = np.array([f["n"] for f in folds], dtype=float) / w_sum
    sp = float(np.nansum([f["spearman"] * wi for f, wi in zip(folds, w)]))
    top = {k: float(np.nansum([f["top"][k]["mean_pnl"] * wi for f, wi in zip(folds, w)]))
           for k in top_ns}
    return {"spearman": sp, "top": top, "folds": folds, "n": int(w_sum)}


# --------------------------------------------------------------------------- #
# 目标函数
# --------------------------------------------------------------------------- #
def make_objective(df, cutoffs, top_ns, pool, min_features, max_estimators):
    """构造 Optuna objective 闭包（捕获固定参数，df 仅加载一次）。"""

    def objective(trial: optuna.trial.Trial) -> float:
        selected = suggest_feature_mask(trial, pool)
        if len(selected) < min_features:
            raise optuna.TrialPruned(f"特征过少 {len(selected)} < {min_features}")
        hp = suggest_hyperparams(trial, max_estimators)
        try:
            res = _walk_forward_trial(df, selected, cutoffs, top_ns, hp, trial)
        except optuna.TrialPruned:
            raise
        except Exception as e:
            log.warning("trial %d 失败: %s（剪枝）", trial.number, e)
            raise optuna.TrialPruned(str(e))

        trial.set_user_attr("selected", json.dumps(selected))
        trial.set_user_attr("n_features", len(selected))
        trial.set_user_attr("top20_pnl", res["top"].get(20, float("nan")))
        trial.set_user_attr("top50_pnl", res["top"].get(50, float("nan")))
        log.info("trial %d | n_feat=%d | Spearman=%.4f | Top20=%.4f",
                 trial.number, len(selected), res["spearman"], res["top"].get(20, float("nan")))
        return res["spearman"]

    return objective


# --------------------------------------------------------------------------- #
# 最佳模型 refit（在全集上，权重可部署）
# --------------------------------------------------------------------------- #
def refit_best(df: pd.DataFrame, selected: list[str], hyperparams: dict,
               out_model: Path, refit_estimators: int = 800) -> None:
    """用最佳超参 + 选定特征，在全集上 refit 并保存 LightGBM booster。"""
    X = df[selected].astype(float).values
    y = df["hypothetical_pnl"].astype(float).values
    params = dict(DEFAULT_LGBM)
    params.update(hyperparams)
    params["objective"] = "huber"
    params["alpha"] = 0.5
    params["n_estimators"] = refit_estimators
    model = lgb.LGBMRegressor(**params)
    model.fit(X, y)
    model.booster_.save_model(str(out_model))
    log.info("最佳模型已保存: %s（%d 特征, %d 轮）", out_model, len(selected), refit_estimators)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def feature_selection_freq(study: optuna.study.Study, pool: str) -> dict:
    """统计各特征在全部 trial 与 Top-10 trial 中的入选频率。"""
    pool_list = CANDIDATES if pool == "candidates" else ALL35
    counts_all = {n: 0 for n in pool_list}
    n_all = 0
    top = sorted([t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE],
                 key=lambda t: t.value if t.value is not None else -1e9, reverse=True)[:10]
    counts_top = {n: 0 for n in pool_list}
    for t in study.trials:
        sel = t.user_attrs.get("selected")
        if not sel:
            continue
        n_all += 1
        chosen = set(json.loads(sel))
        for n in pool_list:
            if n in chosen:
                counts_all[n] += 1
    for t in top:
        chosen = set(json.loads(t.user_attrs.get("selected", "[]")))
        for n in pool_list:
            if n in chosen:
                counts_top[n] += 1
    return {
        "pool": pool_list,
        "all": {n: counts_all[n] / max(n_all, 1) for n in pool_list},
        "top10": {n: counts_top[n] / max(len(top), 1) for n in pool_list},
        "n_all": n_all, "n_top": len(top),
    }


def build_report(study, best, freq, meta) -> str:
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    freq_all = freq["all"]
    freq_top = freq["top10"]
    # 按 Top-10 入选频率排序
    ordered = sorted(freq["pool"], key=lambda n: freq_top[n], reverse=True)
    lines = [
        f"# 阶段1 Step 4 Optuna 特征选择报告 ({ts})",
        "",
        "## 📊 核心指标",
        f"- Study：`{meta['study_name']}`（pool={meta['pool']}, min_features={meta['min_features']}）",
        f"- 试验数：{meta['n_trials_done']}（完成 {meta['n_complete']} / 剪枝 {meta['n_pruned']}）",
        f"- **最佳 OOS Spearman：{best['value']:.4f}**（闸门=不退化 ≥ Step3.5 基线 0.315）",
        f"- 最佳特征数：**{best['n_features']}**",
        f"- 最佳 Top-20 PnL：{best['top20']:.4f} / Top-50：{best['top50']:.4f}",
        f"- 达标判定：{'✅ 不退化' if best['value'] >= meta['gate_spearman'] else '⚠️ 未达基线'}",
        "",
        "## 🎯 Top 10 试验明细",
        "",
        "| # | Spearman | n_feat | Top20 PnL | Top50 PnL |",
        "|---|---:|---:|---:|---:|",
    ]
    top10 = sorted(
        [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE],
        key=lambda t: t.value if t.value is not None else -1e9, reverse=True)[:10]
    for t in top10:
        lines.append(
            f"| {t.number} | {t.value:.4f} | {t.user_attrs.get('n_features')} | "
            f"{t.user_attrs.get('top20_pnl', float('nan')):.4f} | "
            f"{t.user_attrs.get('top50_pnl', float('nan')):.4f} |")
    lines += [
        "",
        "## 📈 因子入选频率（Top-10 试验）",
        "",
        "| 特征 | 全部入选率 | Top-10 入选率 |",
        "|---|---:|---:|",
    ]
    for n in ordered:
        lines.append(f"| `{n}` | {freq_all[n]*100:.1f}% | {freq_top[n]*100:.1f}% |")
    lines += [
        "",
        "## ⚠️ 风险提示",
        "1. 历史回测不代表未来收益，OOS 数字仅供 Step 5 闸门参考。",
        "2. 翻倍策略属激进策略，建议小资金启动，严格止损(-10%)。",
        "3. 北交所(920XXX)流动性低，滑点可能放大，评估集若含相关样本需注意。",
        "4. 150 trial 对 35 维×9 超参的联合空间仍偏薄（multivariate TPE 缓解），"
        "建议 `--pool candidates` 先确认候选因子增益，再用 all35 全量搜索。",
        "5. `all35` 全开关可能丢弃基础特征导致退化，务必以 Step 3.5 基线为闸门复核。",
        "",
        "## 🔧 下一步建议",
        "1. **Step 5 闸门**：若最佳 Spearman ≥ 0.315 且 Top-20 ≥ 0.21，进入 Step 6 重训。",
        "2. 若未达标：检查是否误删关键基础特征，或放宽 `--min-features`。",
        "3. 用选定特征集 refit 最佳模型（已存 logs/stage1_optuna_best_model.txt），"
        "接入 `predict.py` 前先跑方案E 实盘观测。",
        "4. 候选因子入选率低 → 考虑从池移除，精简特征工程。",
        "",
        "---",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "生成者：scripts/stage1_optuna.py",
        "模型版本：vpnl_huber (Step4 tuning)",
        f"数据日期：{meta['data_date']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="阶段1 Step 4 Optuna 特征选择 + 超参优化")
    p.add_argument("--wide-table", type=str,
                   default=str(ROOT / "data" / "double_up" / "stage1_wide_table.parquet"))
    p.add_argument("--pool", type=str, choices=["all35", "candidates"], default="all35",
                   help="特征开关池：all35(35维全开关,设计文档口径) / candidates(26基础固定+9候选)")
    p.add_argument("--min-features", type=int, default=8, help="最少入选特征数（低于则剪枝）")
    p.add_argument("--cutoffs", type=str, default=",".join(DEFAULT_CUTOFFS))
    p.add_argument("--top-n", type=str, default="10,20,50")
    p.add_argument("--n-trials", type=int, default=DEFAULT_N_TRIALS)
    p.add_argument("--timeout", type=int, default=7200, help="总超时秒数（0=不限）")
    p.add_argument("--n-jobs", type=int, default=1, help="并行 trial 数（>1 时建议设 LightGBM n_jobs=1）")
    p.add_argument("--max-search-estimators", type=int, default=400,
                   help="搜索阶段 n_estimators 上限（refit 用 800）")
    p.add_argument("--smoke", action="store_true", help="烟测：max-estimators 降到 80")
    p.add_argument("--study-name", type=str, default=STUDY_NAME)
    p.add_argument("--fresh", action="store_true",
                   help="清空同名 study 后重新优化（默认 load_if_exists 续跑）")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "logs"))
    p.add_argument("--report-dir", type=str, default=str(ROOT / "docs"))
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.smoke:
        args.max_search_estimators = 80
        log.info("烟测模式：max-search-estimators=%d", args.max_search_estimators)

    try:
        cutoffs = sorted(pd.Timestamp(x.strip()) for x in args.cutoffs.split(",") if x.strip())
        top_ns = [int(x) for x in args.top_n.split(",") if x.strip()]
        # 加载一次（用全 35 维保证所有 trial 列可用）
        df = load_and_validate(Path(args.wide_table), ALL35)

        if args.fresh:
            try:
                optuna.delete_study(study_name=args.study_name, storage=STORAGE)
                log.info("已清空旧 study: %s", args.study_name)
            except (KeyError, ValueError) as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
                print(f"[WARN] swallowed: {e!r}")  # 尚不存在，无需删除
        study = optuna.create_study(
            study_name=args.study_name, direction="maximize",
            sampler=TPESampler(seed=42, multivariate=True),
            pruner=PRUNER, storage=STORAGE, load_if_exists=True,
        )
        log.info("Study 已就绪（续跑则自动 load_if_exists）: %s", args.study_name)

        objective = make_objective(df, cutoffs, top_ns, args.pool,
                                   args.min_features, args.max_search_estimators)
        t0 = time.time()
        study.optimize(
            objective, n_trials=args.n_trials,
            timeout=args.timeout if args.timeout else None,
            n_jobs=args.n_jobs,
        )
        log.info("优化结束，耗时 %.0fs", time.time() - t0)

        # ---- 输出 ----
        best = study.best_trial
        best_selected = json.loads(best.user_attrs["selected"])
        best_hp = {k: v for k, v in best.params.items() if not k.startswith("f__")}
        best_out = {
            "study_name": args.study_name, "pool": args.pool,
            "value_spearman": best.value,
            "n_features": best.user_attrs["n_features"],
            "selected_features": best_selected,
            "top20_pnl": best.user_attrs["top20_pnl"],
            "top50_pnl": best.user_attrs["top50_pnl"],
            "hyperparams": best_hp,
        }
        best_json = out_dir / "stage1_optuna_best.json"
        best_json.write_text(json.dumps(best_out, ensure_ascii=False, indent=2), encoding="utf-8")

        # 优化历史
        trials_csv = out_dir / "stage1_optuna_trials.csv"
        study.trials_dataframe().to_csv(trials_csv, index=False)

        # 最佳模型 refit（全集）
        best_model = out_dir / "stage1_optuna_best_model.txt"
        refit_best(df, best_selected, best_hp, best_model, refit_estimators=800)

        # 可视化（optuna 5.x 部分绘图函数名有变动；逐个按名取用，缺失则跳过）
        try:
            import importlib

            viz = importlib.import_module("optuna.visualization")
            for k, attr in {
                "param_importances": "plot_param_importances",
                "parallel": "plot_parallel",
                "slice": "plot_slice",
                "contour": "plot_contour",
            }.items():
                fn = getattr(viz, attr, None)
                if fn is None:
                    continue
                try:
                    fn(study).write_html(str(out_dir / f"stage1_optuna_{k}.html"))
                except Exception as e:
                    log.warning("可视化 %s 生成失败: %s", k, e)
        except Exception as e:
            log.warning("可视化模块加载失败: %s", e)

        # 报告
        n_complete = sum(1 for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE)
        n_pruned = sum(1 for t in study.trials if t.state == optuna.trial.TrialState.PRUNED)
        freq = feature_selection_freq(study, args.pool)
        meta = {
            "study_name": args.study_name, "pool": args.pool,
            "min_features": args.min_features,
            "n_trials_done": len(study.trials), "n_complete": n_complete,
            "n_pruned": n_pruned,
            "gate_spearman": 0.315,
            "data_date": str(df["_date"].max().date()),
        }
        report = build_report(study, {
            "value": best.value, "n_features": best.user_attrs["n_features"],
            "top20": best.user_attrs["top20_pnl"], "top50": best.user_attrs["top50_pnl"],
        }, freq, meta)
        (Path(args.report_dir) / "stage1_optuna_report.md").write_text(report, encoding="utf-8")

        print("\n=== Step 4 优化完成 ===")
        print(f"最佳 OOS Spearman = {best.value:.4f}  （闸门 0.315）")
        print(f"最佳特征数 = {best.user_attrs['n_features']}  Top20 PnL = {best.user_attrs['top20_pnl']:.4f}")
        print(f"最佳特征: {best_selected}")
        print(f"最佳超参: {best_hp}")
        print(f"\n最佳配置: {best_json}")
        print(f"试验历史: {trials_csv}")
        print(f"最佳模型: {best_model}")
        print(f"报告: {Path(args.report_dir) / 'stage1_optuna_report.md'}")
        return 0
    except Exception as e:
        log.error("Step 4 优化失败: %s", e, exc_info=True)
        print(f"\n❌ Step 4 优化失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
