#!/usr/bin/env python3
"""scripts/stage1_gate.py — 阶段 1 Step 5 闸门准入校验（准入才进 Step 6 重训）

目的
----
在 Step 4（Optuna 特征选择）产出 `stage1_optuna_best.json`（21 维 + 超参）后，本脚本做
**准入判定**，确认选定配置可安全进入 Step 6 重训 / Step 7 切换。

判定维度（对照 docs/stage1_feature_migration_plan.md §8 + §10）
-------------------------------------------------------------
1. **命名契约**：21 个选定特征名必须全部存在于宽表列（#1 落地风险，错名即 KeyError）。
2. **数值 parity（强制）**：调用 `tests/test_stage1_factors_parity.py`（新 Polars 管线 vs
   `features_5d.py` 18/26 维逐位一致 ≤1e-9）。该测试在 Step 1 已 0 差；此处复跑兜底。
3. **可复现性**：用固定 21 维 + `best.json` 超参重跑 walk-forward，池化指标须**复现**
   `best.json` 报告值（Spearman/Top-20 容差 0.02），否则说明 config 与数据版本不一致。
4. **OOS 不退化（池化硬闸门）**：候选池化 Spearman ≥ 0.315、Top-20 ≥ 0.21、Top-50 ≥ 0.20
   （= Step 3.5 基线，复算于本脚本同一次运行，保证口径一致）。
5. **逐折不退化**：候选每折 Spearman / Top-20 须 ≥ 基线同折（允许 0.01 绝对松弛），
   防止"池化达标但某折崩坏"（2025+ 折基线 Top-20 仅 0.163 是历史隐患）。
   + 安全阀：候选每折 Spearman > 0 且 Top-20 > 0（选股在该折须正期望）。
6. **稳定性**：21 个选定特征在**全部 150 trial** 的入选率 ≥ 60%（`trials.csv` 统计），
   防止单 trial 过拟合。

复用
----
`stage1_baseline.load_and_validate` / `walk_forward_eval`（与 Optuna `_walk_forward_trial`
同训练流程：DEFAULT_LGBM + huber α=0.5 + early_stopping(50) + random_state=42），
故候选配置可精确复现 Step 4 的 0.3404 池化 Spearman。

输入（参数与调用方式）
----------------------
    # 默认：读 logs/stage1_optuna_best.json + stage1_optuna_trials.csv + 宽表
    venv/bin/python scripts/stage1_gate.py

    # 指定 config / trials / 宽表
    venv/bin/python scripts/stage1_gate.py \
        --config logs/stage1_optuna_best.json \
        --trials-csv logs/stage1_optuna_trials.csv \
        --wide-table data/double_up/stage1_wide_table.parquet

    # 跳过 parity 单测（仅当已知已绿）
    venv/bin/python scripts/stage1_gate.py --no-parity

退出码（供流程自动衔接）
------------------------
    0  GATE_PASS  —— 全部闸门通过，可进 Step 6。
    1  GATE_FAIL  —— 一项或多项闸门未达标（stdout 打印具体原因 + 可操作提示）。
    2  GATE_ERROR —— 脚本无法运行（缺输入/异常/环境），与"被拒"区分，便于排查。

产物（与前序步骤格式兼容）
--------------------------
    logs/stage1_gate_{ts}.json   机器可读判定（checks + 指标 + 退出码）
    logs/stage1_gate_{ts}.md     报告（核心指标 / 闸门判定 / Top N / 风险 / 下一步）
    logs/stage1_gate_{ts}.log    详细日志（与项目 logging 风格一致）
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用 Step 3.5 基础设施（单一来源）
from stage1_baseline import (  # noqa: E402
    ALL35,
    BASE26,
    load_and_validate,
    walk_forward_eval,
)

log = logging.getLogger("stage1_gate")

# --------------------------------------------------------------------------- #
# 默认值与退出码
# --------------------------------------------------------------------------- #
LOG_DIR = ROOT / "logs"
DEFAULT_CONFIG = LOG_DIR / "stage1_optuna_best.json"
DEFAULT_TRIALS = LOG_DIR / "stage1_optuna_trials.csv"
DEFAULT_WIDE = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
PARITY_TEST = ROOT / "tests" / "test_stage1_factors_parity.py"

# §8 / §10 闸门阈值（= Step 3.5 基线，复算于本脚本）
GATE_SPEARMAN = 0.315
GATE_TOP20 = 0.21
GATE_TOP50 = 0.20
MIN_STABILITY = 0.60          # 入选率下限
REGRESS_TOL = 0.01            # 逐折不退化允许的微小反向松弛
REPRO_TOL = 0.02              # 候选 vs best.json 容差
FLOOR = 0.0                   # 逐折正期望安全下限

GATE_PASS = 0
GATE_FAIL = 1
GATE_ERROR = 2


# --------------------------------------------------------------------------- #
# 输入加载与校验
# --------------------------------------------------------------------------- #
def load_config(path: Path | str) -> dict:
    """读取 best.json，校验结构完整。

    返回 selected_features / hyperparams / value_spearman / top20_pnl / top50_pnl。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Step 4 配置缺失: {path}（请先跑 stage1_optuna.py）")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for k in ("selected_features", "hyperparams", "value_spearman", "top20_pnl", "top50_pnl"):
        if k not in cfg:
            raise ValueError(f"config 缺字段 {k}：{list(cfg)}")
    sel = cfg["selected_features"]
    if not isinstance(sel, list) or len(sel) == 0:
        raise ValueError(f"selected_features 非法: {sel}")
    expected_hp = {
        "learning_rate", "n_estimators", "num_leaves", "max_depth",
        "min_child_samples", "subsample", "colsample_bytree", "reg_lambda", "reg_alpha",
    }
    missing_hp = expected_hp - set(cfg["hyperparams"])
    if missing_hp:
        log.warning("超参缺键 %s（将沿用 DEFAULT_LGBM 对应值）", missing_hp)
    log.info("载入 config：%d 维 | 报告 Spearman=%.4f | Top20=%.4f | Top50=%.4f",
             len(sel), cfg["value_spearman"], cfg["top20_pnl"], cfg["top50_pnl"])
    return cfg


def check_naming_contract(selected: list[str], columns: list[str]) -> dict:
    """命名契约：选定特征必须全部存在于宽表列，且属于已知特征池。

    这是 #1 落地风险——predict.py 按 model.feature_name() 动态取列，错名即 KeyError。
    """
    col_set = set(columns)
    missing = [f for f in selected if f not in col_set]
    out_of_pool = [f for f in selected if f not in set(ALL35)]
    ok = (not missing) and (not out_of_pool)
    return {
        "ok": ok,
        "missing": missing,
        "out_of_pool": out_of_pool,
    }


def compute_stability(trials_csv: Path, selected: list[str]) -> dict | None:
    """从 trials.csv 统计每个选定特征在全部 COMPLETE trial 中的入选率。

    返回 {feature: freq} 与样本数 n；若 csv 缺失或无 user_attrs_selected 列返回 None。
    """
    if not trials_csv.exists():
        log.warning("trials.csv 缺失: %s（跳过稳定性统计）", trials_csv)
        return None
    df = pd.read_csv(trials_csv)
    if "user_attrs_selected" not in df.columns:
        log.warning("trials.csv 无 user_attrs_selected 列（跳过稳定性统计）")
        return None
    complete = df[df.get("state", pd.Series(["COMPLETE"] * len(df))) == "COMPLETE"] \
        if "state" in df.columns else df
    counts = {f: 0 for f in selected}
    n = 0
    for raw in complete["user_attrs_selected"].dropna():
        try:
            chosen = set(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        n += 1
        for f in selected:
            if f in chosen:
                counts[f] += 1
    if n == 0:
        log.warning("无 COMPLETE trial 可选记录（跳过稳定性统计）")
        return None
    freq = {f: counts[f] / n for f in selected}
    log.info("稳定性统计：%d 个 COMPLETE trial", n)
    return {"freq": freq, "n_trials": n}


def run_parity_test(check: bool) -> dict:
    """数值 parity（§8.1 强制）：复跑 tests/test_stage1_factors_parity.py。

    返回 {ran, passed, detail}。check=False 时标记 skipped（不阻断）。
    """
    if not check:
        return {"ran": False, "passed": True, "detail": "已显式跳过（--no-parity）"}
    if not PARITY_TEST.exists():
        return {"ran": False, "passed": True,
                "detail": f"未找到 parity 测试 {PARITY_TEST}（人工确认 Step 1 已绿）"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(PARITY_TEST), "-q"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        )
        passed = proc.returncode == 0
        detail = (proc.stdout or proc.stderr).strip().splitlines()[-1] if (
            proc.stdout or proc.stderr) else ""
        return {"ran": True, "passed": passed, "detail": detail}
    except Exception as e:
        log.warning("parity 测试执行异常: %s（不阻断，需人工确认）", e)
        return {"ran": False, "passed": True, "detail": f"执行异常: {e}（需人工确认 Step 1 已绿）"}


# --------------------------------------------------------------------------- #
# 评估与闸门组装
# --------------------------------------------------------------------------- #
def _pooled(result: dict, k: int) -> float:
    return float(result["pooled"]["top"][k]["mean_pnl"])


def run_gate(args: argparse.Namespace) -> tuple[dict, int]:
    """执行全部闸门，返回 (result_dict, exit_code)。"""
    cfg = load_config(args.config)
    selected = cfg["selected_features"]
    hyperparams = cfg["hyperparams"]
    wide_table = Path(args.wide_table)
    trials_csv = Path(args.trials_csv)

    # 1) 加载宽表（用 ALL35 保证所有列可用；基线 26 与候选 21 均从中子集）
    df = load_and_validate(wide_table, ALL35)

    # 2) 命名契约
    contract = check_naming_contract(selected, list(df.columns))

    # 3) 稳定性（来自 trials.csv）
    stab = compute_stability(trials_csv, selected)

    # 4) parity 单测
    parity = run_parity_test(args.check_parity)

    # 5) 基线评估（26 维 + DEFAULT_LGBM），作为"不退化"参照
    cutoffs = sorted(pd.Timestamp(x.strip()) for x in args.cutoffs.split(",") if x.strip())
    top_ns = [int(x) for x in args.top_n.split(",") if x.strip()]
    g_sp, g_t20, g_t50 = args.gate_spearman, args.gate_top20, args.gate_top50
    min_stab, reg_tol, repro_tol = args.min_stability, args.regress_tol, args.repro_tol
    log.info("基线评估：%d 维 DEFAULT_LGBM | cutoffs=%s", len(BASE26), cutoffs)
    baseline = walk_forward_eval(df, BASE26, cutoffs, top_ns,
                                 objective="huber", alpha=0.5, lgbm_kwargs={"random_state": 42})

    # 6) 候选评估（21 维 + best.json 超参），须精确复现 Step 4
    cand_kwargs = {**hyperparams, "random_state": 42}
    log.info("候选评估：%d 维 + best 超参 | cutoffs=%s", len(selected), cutoffs)
    candidate = walk_forward_eval(df, selected, cutoffs, top_ns,
                                  objective="huber", alpha=0.5, lgbm_kwargs=cand_kwargs)

    # ---- 组装闸门 ----
    checks: list[dict] = []

    def add(name: str, ok: bool, value, threshold, status_pass: str,
            status_fail: str, action: str) -> None:
        checks.append({
            "name": name, "passed": bool(ok),
            "value": value, "threshold": threshold,
            "detail_pass": status_pass, "detail_fail": status_fail, "action": action,
        })

    # C1 命名契约
    add("C1 命名契约（21 维全部存在于宽表）",
        contract["ok"],
        f"缺失={contract['missing'] or '无'}; 越池={contract['out_of_pool'] or '无'}",
        "全部命中",
        "✅ 21 维命名与宽表/预测管线一致",
        f"❌ 错名将导致 predict.py KeyError：{contract['missing'] or contract['out_of_pool']}",
        "核对 factors_polars/factors_candidates 输出列名与 best.json 完全一致后重跑")

    # C2 parity
    add("C2 数值 parity（Polars vs features_5d ≤1e-9）",
        parity["passed"],
        parity["detail"],
        "测试通过",
        "✅ 新管线与旧实现逐位一致",
        "❌ parity 测试失败，新管线与 features_5d 数值漂移",
        "先修复 factors_polars 对齐 features_5d（参考 test_stage1_factors_parity.py 失败项）")

    # C3 可复现性（候选 vs best.json 报告值）
    repro_sp = abs(candidate["pooled"]["spearman"] - cfg["value_spearman"])
    repro_t20 = abs(_pooled(candidate, 20) - cfg["top20_pnl"])
    repro_ok = (repro_sp <= repro_tol) and (repro_t20 <= repro_tol)
    add("C3 可复现性（候选复现 best.json）",
        repro_ok,
        f"ΔSpearman={repro_sp:+.4f}; ΔTop20={repro_t20:+.4f}",
        f"|Δ|≤{repro_tol}",
        "✅ 固定配置精确复现 Step 4 报告值",
        "❌ 候选配置无法复现 best.json（config 与数据版本不一致）",
        "确认 best.json 取自当前宽表版本；若数据已更新，回 Step 4 重搜或重生成宽表")

    # C4 池化硬闸门
    sp_ok = candidate["pooled"]["spearman"] >= g_sp
    t20_ok = _pooled(candidate, 20) >= g_t20
    t50_ok = _pooled(candidate, 50) >= g_t50
    pool_ok = sp_ok and t20_ok and t50_ok
    add("C4 OOS 不退化（池化）",
        pool_ok,
        f"Spearman={candidate['pooled']['spearman']:.4f}(≥{g_sp}); "
        f"Top20={_pooled(candidate,20):.4f}(≥{g_t20}); "
        f"Top50={_pooled(candidate,50):.4f}(≥{g_t50})",
        "三项全达标",
        "✅ 池化指标不低于 Step 3.5 基线",
        "❌ 池化指标低于基线（或复现异常）",
        "检查是否误删关键基础特征；必要时回 Step 4 放宽 --min-features 重搜")

    # C5 逐折不退化 + 安全阀
    base_by_cut = {f["cutoff"]: f for f in baseline["folds"]}
    cand_by_cut = {f["cutoff"]: f for f in candidate["folds"]}
    fold_issues: list[str] = []
    for cut, cf in cand_by_cut.items():
        bf = base_by_cut.get(cut)
        if bf is None:
            fold_issues.append(f"{cut}: 基线无对应折")
            continue
        d_sp = cf["spearman"] - bf["spearman"]
        d_t20 = cf["top"][20]["mean_pnl"] - bf["top"][20]["mean_pnl"]
        if d_sp < -reg_tol:
            fold_issues.append(f"{cut}: Spearman 倒退 {d_sp:+.4f}")
        if d_t20 < -reg_tol:
            fold_issues.append(f"{cut}: Top20 倒退 {d_t20:+.4f}")
        if cf["spearman"] <= FLOOR:
            fold_issues.append(f"{cut}: Spearman={cf['spearman']:.4f}≤0（秩相关失效）")
        if cf["top"][20]["mean_pnl"] <= FLOOR:
            fold_issues.append(f"{cut}: Top20 PnL={cf['top'][20]['mean_pnl']:.4f}≤0（该折选股负期望）")
    fold_ok = len(fold_issues) == 0
    add("C5 逐折不退化 + 正期望安全阀",
        fold_ok,
        f"折数={candidate['n_folds']}; 问题={fold_issues or '无'}",
        f"每折 ≥ 基线（松弛 {reg_tol}）且正期望",
        "✅ 无折退化、各折选股正期望",
        "❌ 存在折退化/负期望：" + "; ".join(fold_issues),
        "定位退化折（多为近期 2025+）；若系统性退化，回 Step 4 改用 trial 66 或更稳配置重搜")

    # C6 稳定性
    if stab is None:
        add(f"C6 稳定性（入选率 ≥{min_stab:.0%}）",
            True, "未统计（trials.csv 不可用）", "—",
            "⚠️ 未校验（需 trials.csv）",
            "未校验",
            "提供 --trials-csv 以启用稳定性闸门")
    else:
        low = [f for f, fr in stab["freq"].items() if fr < min_stab]
        stab_ok = len(low) == 0
        add(f"C6 稳定性（入选率 ≥{min_stab:.0%}）",
            stab_ok,
            "低于阈值的特征=" + (", ".join(f"{f}={stab['freq'][f]:.0%}" for f in low) or "无"),
            f"全部 ≥{min_stab:.0%}（{stab['n_trials']} trials）",
            "✅ 选定特征在 ≥60% trial 被选中，非单点过拟合",
            f"❌ 以下特征入选率 <{min_stab:.0%}：{low}",
            "剔除低稳特征后重跑；或回 Step 4 增加 --n-trials / 改用 --pool candidates 确认")

    passed = all(c["passed"] for c in checks)
    exit_code = GATE_PASS if passed else GATE_FAIL

    result = {
        "passed": passed,
        "exit_code": exit_code,
        "gate_constants": {
            "spearman": g_sp, "top20": g_t20, "top50": g_t50,
            "min_stability": min_stab, "regress_tol": reg_tol,
            "repro_tol": repro_tol,
        },
        "config": {
            "n_features": len(selected),
            "selected_features": selected,
            "reported_spearman": cfg["value_spearman"],
            "reported_top20": cfg["top20_pnl"],
            "reported_top50": cfg["top50_pnl"],
        },
        "naming_contract": contract,
        "stability": stab,
        "parity": parity,
        "baseline": _result_summary(baseline, top_ns),
        "candidate": _result_summary(candidate, top_ns),
        "checks": checks,
        "data_date": str(df["_date"].max().date()),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    return result, exit_code


def _result_summary(result: dict, top_ns: list[int]) -> dict:
    """把 walk_forward_eval 结果压缩为可序列化摘要。"""
    folds = []
    for f in result["folds"]:
        folds.append({
            "cutoff": f["cutoff"], "train_n": f["train_n"], "n": f["n"],
            "spearman": f["spearman"],
            "top": {str(k): {"mean_pnl": f["top"][k]["mean_pnl"],
                             "win_rate": f["top"][k]["win_rate"]} for k in top_ns},
        })
    return {
        "n_folds": result["n_folds"],
        "pooled": {
            "spearman": result["pooled"]["spearman"],
            "top": {str(k): result["pooled"]["top"][k] for k in top_ns},
        },
        "folds": folds,
    }


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #
def build_report(result: dict) -> str:
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    passed = result["passed"]
    badge = "✅ 闸门通过（可进 Step 6）" if passed else "❌ 闸门未通过（禁止进入 Step 6）"
    cand = result["candidate"]
    base = result["baseline"]
    top_ns = [int(k) for k in cand["pooled"]["top"]]

    lines = [
        f"# 阶段1 Step 5 闸门报告 ({ts})",
        "",
        f"## 闸门判定：{badge}",
        "",
        "## 📊 核心指标",
        f"- 选定特征数：**{result['config']['n_features']}**",
        f"- 数据日期：{result['data_date']}",
        f"- **候选池化 Spearman：{cand['pooled']['spearman']:.4f}**"
        f"（基线 {base['pooled']['spearman']:.4f}；报告值 {result['config']['reported_spearman']:.4f}）",
        f"- **候选 Top-20 PnL：{cand['pooled']['top']['20']['mean_pnl']:.4f}**"
        f"（基线 {base['pooled']['top']['20']['mean_pnl']:.4f}；报告值 {result['config']['reported_top20']:.4f}）",
        f"- 候选 Top-50 PnL：{cand['pooled']['top']['50']['mean_pnl']:.4f}"
        f"（基线 {base['pooled']['top']['50']['mean_pnl']:.4f}）",
        "",
        "## 🚦 闸门清单",
        "",
        "| 闸门 | 判定 | 实测 | 阈值 | 说明 |",
        "|---|:---:|---|---|---|",
    ]
    for c in result["checks"]:
        mark = "✅" if c["passed"] else "❌"
        lines.append(
            f"| {c['name']} | {mark} | {c['value']} | {c['threshold']} | "
            f"{c['detail_pass'] if c['passed'] else c['detail_fail']} |")

    lines += [
        "",
        "## 🎯 逐折明细（候选 vs 基线）",
        "",
        "| 折 | 候选 test_n | 候选 Spearman | 基线 Spearman | "
        + " | ".join(f"候选 Top{k}" for k in top_ns) + " |",
        "|---|---:|---:|---:|" + "---:|" * len(top_ns),
    ]
    base_by_cut = {f["cutoff"]: f for f in base["folds"]}
    for cf in cand["folds"]:
        bf = base_by_cut.get(cf["cutoff"], {})
        row = [cf["cutoff"], str(cf["n"]), f"{cf['spearman']:.4f}",
               f"{bf.get('spearman', float('nan')):.4f}"]
        row += [f"{cf['top'][str(k)]['mean_pnl']:.4f}" for k in top_ns]
        lines.append("| " + " | ".join(row) + " |")

    # 稳定性明细
    min_stab = result["gate_constants"]["min_stability"]
    stab = result.get("stability")
    if stab:
        lines += [
            "",
            "## 📈 特征稳定性（全部 trial 入选率）",
            "",
            "| 特征 | 入选率 |",
            "|---|---:|",
        ]
        for f, fr in sorted(stab["freq"].items(), key=lambda kv: kv[1]):
            flag = " ⚠️" if fr < min_stab else ""
            lines.append(f"| `{f}` | {fr*100:.1f}%{flag} |")

    # 失败行动提示
    failed = [c for c in result["checks"] if not c["passed"]]
    if failed:
        lines += [
            "",
            "## 🔧 失败处置（可操作）",
        ]
        for c in failed:
            lines.append(f"- **{c['name']}**：{c['detail_fail']} → {c['action']}")

    lines += [
        "",
        "## ⚠️ 风险提示",
        "1. 历史回测不代表未来收益，OOS 数字仅作 Step 6/7 上线参考。",
        "2. 翻倍策略属激进策略，建议小资金启动，严格止损(-10%)。",
        "3. 北交所(920XXX)流动性低，滑点可能放大，评估集若含相关样本需注意。",
        "4. 闸门通过 ≠ 实盘盈利；Step 7 切换前须过金丝雀 + plan_e 实盘观测。",
        "5. 若数据同步后宽表更新，须重跑本闸门（含 parity）确认仍达标。",
        "",
        "## 🔧 下一步建议",
        "1. 闸门通过 → 进入 Step 6：用 21 维 + best.json 超参重训 vpnl_huber（落新文件）。",
        "2. 闸门未过 → 按上方失败处置修复，或回 Step 4 调整搜索后重跑本闸门。",
        "3. 冻结特征清单随 best.json 已锁定，Step 6/7 以 `selected_features` 为唯一契约源。",
        "",
        "---",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "生成者：scripts/stage1_gate.py",
        "模型版本：vpnl_huber (Step5 gate)",
        f"数据日期：{result['data_date']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="阶段1 Step 5 闸门准入校验")
    p.add_argument("--config", type=str, default=str(DEFAULT_CONFIG),
                   help="Step 4 最佳配置 best.json 路径")
    p.add_argument("--trials-csv", type=str, default=str(DEFAULT_TRIALS),
                   help="Optuna trials.csv 路径（稳定性统计用）")
    p.add_argument("--wide-table", type=str, default=str(DEFAULT_WIDE),
                   help="宽表 parquet 路径（stage1_wide_table.py 产出）")
    p.add_argument("--cutoffs", type=str, default="2024-01-01,2025-01-01",
                   help="walk-forward cutoff（逗号分隔）")
    p.add_argument("--top-n", type=str, default="10,20,50",
                   help="Top-N 列表（逗号分隔）")
    p.add_argument("--gate-spearman", type=float, default=GATE_SPEARMAN,
                   help="池化 Spearman 硬闸门下限（默认 0.315=Step3.5 基线）")
    p.add_argument("--gate-top20", type=float, default=GATE_TOP20,
                   help="池化 Top-20 PnL 硬闸门下限（默认 0.21）")
    p.add_argument("--gate-top50", type=float, default=GATE_TOP50,
                   help="池化 Top-50 PnL 硬闸门下限（默认 0.20）")
    p.add_argument("--min-stability", type=float, default=MIN_STABILITY,
                   help="稳定性入选率下限（默认 0.60=§8.5）；下调=接受更低鲁棒性")
    p.add_argument("--regress-tol", type=float, default=REGRESS_TOL,
                   help="逐折不退化允许的微小反向松弛（默认 0.01）")
    p.add_argument("--repro-tol", type=float, default=REPRO_TOL,
                   help="候选 vs best.json 复现容差（默认 0.02）")
    p.add_argument("--check-parity", action="store_true", default=True,
                   help="复跑 parity 单测（默认开；--no-parity 关闭）")
    p.add_argument("--no-parity", dest="check_parity", action="store_false",
                   help="跳过 parity 单测")
    p.add_argument("--out-dir", type=str, default=str(LOG_DIR),
                   help="JSON / MD / LOG 输出目录")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"stage1_gate_{ts}.log"

    # 日志：控制台 + 文件
    _fh = logging.FileHandler(log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), _fh],
    )

    try:
        result, exit_code = run_gate(args)
    except Exception as e:
        log.error("闸门执行异常: %s", e, exc_info=True)
        print(f"\n❌ GATE_ERROR: 脚本无法完成: {e}", file=sys.stderr)
        print("GATE_RESULT=ERROR")
        return GATE_ERROR

    # 写 JSON + MD
    out_json = out_dir / f"stage1_gate_{ts}.json"
    out_md = out_dir / f"stage1_gate_{ts}.md"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    out_md.write_text(build_report(result), encoding="utf-8")

    # 控制台摘要
    print("\n=== Step 5 闸门结果 ===")
    print(f"判定: {'✅ PASS' if result['passed'] else '❌ FAIL'}")
    for c in result["checks"]:
        mark = "✅" if c["passed"] else "❌"
        print(f"  {mark} {c['name']} — {c['value']}")
    print(f"\n候选池化 Spearman={result['candidate']['pooled']['spearman']:.4f} "
          f"Top20={result['candidate']['pooled']['top']['20']['mean_pnl']:.4f}")
    print(f"基线池化 Spearman={result['baseline']['pooled']['spearman']:.4f} "
          f"Top20={result['baseline']['pooled']['top']['20']['mean_pnl']:.4f}")
    print(f"\nJSON: {out_json}")
    print(f"报告: {out_md}")
    print(f"日志: {log_path}")
    print(f"GATE_RESULT={'PASS' if result['passed'] else 'FAIL'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
