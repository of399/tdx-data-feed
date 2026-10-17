#!/usr/bin/env python3
"""scripts/stage1_retrain.py — 阶段 1 Step 6 重训（vpnl_huber 复刻 + 21 维精选集）

目的
----
用 Step 4 选出的 **21 维精选特征 + best.json 超参**，复刻已部署 `vpnl_huber`
（LGBMRegressor, objective=huber, alpha=0.5, 目标 hypothetical_pnl），在**全量**
宽表上重训，并把新权重保存到**独立新文件**：
    data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt

安全约束（最高优先级）
----------------------
1. **绝不覆盖 / 修改任何旧模型文件**。旧 `d_signal_5d_model_vpnl_huber.txt`
   （部署默认）原样保留，作为回滚锚点。
2. **硬守卫**：脚本只会写 `--out-model`（默认 *_stage1.txt）；若输出路径解析后命中
   任何已存在的 `d_signal_5d_model*.txt`（除自身 stage1 产物外），立即 abort（exit 1），
   即便只读保护失效也不会动旧文件。
3. **旧模型保护**：执行前对部署旧模型做 (a) 时间戳备份 `.bak`；(b) `chmod 0444`
   只读保护（可 `chmod 644` 还原）。均可 `--no-* ` 关闭。
4. **落盘校验**：保存后立即回读 booster，确认 num_feature == 21 且 feature_name()
   与精选集完全一致（保证 predict.py 按名取列成功）。

训练协议（与 stage1_baseline / stage1_optuna 同款，保证可复现）
-----------------------------------------------------------
- params = dict(DEFAULT_LGBM) 再 update best.json 超参（含 random_state=42）。
- objective=huber, alpha=0.5（与 vpnl_huber 一致）。
- 迭代轮数：在"末折 holdout"（train < 末 cutoff, valid ≥ 末 cutoff，即 2025+）上
  以 early_stopping(50) 选定 best_iteration_；最终在全量上以该轮数训练（无早停，确定）。
- 评估：复刻"同款 walk-forward"精神——除落盘模型外，额外用"仅 <holdout 训练、
  ≥holdout 测试"给出**纯 OOS** 最近期数字（对照 Step 5 闸门 fold1≈0.3475/0.2045），
  并给全量模型在 holdout 上的 sanity（曾用于选轮，偏乐观，仅作地板参考）。

输入
----
    venv/bin/python scripts/stage1_retrain.py
    venv/bin/python scripts/stage1_retrain.py --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt
    venv/bin/python scripts/stage1_retrain.py --no-protect-old   # 跳过只读保护（不推荐）

产物
----
- data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt   ★ 新模型（唯一新写文件）
- data/double_up/stage1_selected_features.json            冻结契约（21 维+超参快照，Step 7 唯一契约源）
- data/double_up/d_signal_5d_model_vpnl_huber.txt.bak      旧模型备份（若启用）
- logs/stage1_retrain_{ts}.json / .md / .log              报告与日志

退出码
------
    0  RETRAIN_OK   重训成功、校验通过、旧模型完好
    1  RETRAIN_FAIL 守卫/保护失败、命名契约破坏、落盘校验失败（旧模型不受影响）
    2  RETRAIN_ERROR 脚本异常（缺输入/环境），旧模型不受影响
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用 Step 3.5 基础设施（单一来源：数据加载 / 指标 / 训练默认）
from stage1_baseline import (  # noqa: E402
    ALL35,
    DEFAULT_LGBM,
    _eval_fold,
    load_and_validate,
)

log = logging.getLogger("stage1_retrain")

# --------------------------------------------------------------------------- #
# 路径常量（单一来源，避免硬编码漂移）
# --------------------------------------------------------------------------- #
DOUBLE_UP = ROOT / "data" / "double_up"
OLD_MODEL = DOUBLE_UP / "d_signal_5d_model_vpnl_huber.txt"          # 部署默认，须保护
OUT_MODEL_DEFAULT = DOUBLE_UP / "d_signal_5d_model_vpnl_huber_stage1.txt"  # 唯一新写文件
FROZEN_DEFAULT = DOUBLE_UP / "stage1_selected_features.json"        # Step 5/7 冻结契约
DEFAULT_CONFIG = ROOT / "logs" / "stage1_optuna_best.json"          # Step 4 产物
DEFAULT_WIDE = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"

RETRAIN_OK = 0
RETRAIN_FAIL = 1
RETRAIN_ERROR = 2


# --------------------------------------------------------------------------- #
# 旧模型保护（备份 + 只读）
# --------------------------------------------------------------------------- #
def backup_old_model(old_path: Path, enabled: bool) -> Path | None:
    """拷贝旧部署模型到 .bak（固定名，可重复覆盖），返回备份路径或 None。

    注意：必须用 copyfile（仅内容）而非 copy2，否则会继承源文件的只读位（0444），
    导致后续重训的备份步骤因 .bak 只读而 Permission denied。备份文件始终保持可写。
    """
    if not enabled:
        log.info("跳过旧模型备份（--no-backup-old）")
        return None
    if not old_path.exists():
        log.warning("旧模型不存在，无需备份: %s", old_path)
        return None
    bak = old_path.with_suffix(old_path.suffix + ".bak")  # .txt -> .txt.bak
    if bak.exists():
        try:
            os.chmod(bak, 0o644)  # 先解除可能存在的只读位
        except OSError as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
            print(f"[WARN] swallowed: {e!r}")
    shutil.copyfile(old_path, bak)
    os.chmod(bak, 0o644)  # 备份保持可写，便于下次覆盖
    log.info("旧模型已备份: %s -> %s", old_path.name, bak.name)
    return bak


def protect_old_model(old_path: Path, enabled: bool) -> None:
    """对旧部署模型加只读保护（chmod 0444）。可 chmod 644 还原。"""
    if not enabled:
        log.info("跳过旧模型只读保护（--no-protect-old）")
        return
    if not old_path.exists():
        return
    try:
        os.chmod(old_path, 0o444)
        log.info("旧模型已设为只读(0444): %s（还原: chmod 644 %s）",
                 old_path.name, old_path)
    except OSError as e:
        log.warning("旧模型只读保护失败（不影响安全守卫）: %s", e)


def guard_output_path(out_model: Path) -> None:
    """硬守卫：禁止输出路径命中任何已存在的旧模型文件（除自身 stage1 产物外）。

    这是保护旧模型的核心安全网：无论只读保护是否生效，只要输出路径解析后等于
    任一已存在的 `d_signal_5d_model*.txt` 且**不是** stage1 产物，就拒绝写入。
    """
    out_r = out_model.resolve()
    if out_r == OLD_MODEL.resolve():
        raise PermissionError(
            f"❌ 安全守卫：输出路径与旧部署模型相同 {OLD_MODEL.name}，禁止覆盖旧模型")
    if not DOUBLE_UP.exists():
        return
    for p in sorted(DOUBLE_UP.glob("d_signal_5d_model*.txt")):
        if p.resolve() == out_r:
            if "stage1" not in p.name:
                raise PermissionError(
                    f"❌ 安全守卫：输出路径 {out_model.name} 命中已存在旧模型 {p.name}，"
                    f"拒绝写入以保护旧模型。请改用独立的 stage1 文件名。")
            log.info("输出命中自身 stage1 产物（允许覆盖重训）: %s", p.name)


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Step 4 配置缺失: {path}（请先跑 stage1_optuna.py）")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for k in ("selected_features", "hyperparams"):
        if k not in cfg:
            raise ValueError(f"config 缺字段 {k}")
    return cfg


def write_frozen_contract(path: Path, selected: list[str], hyperparams: dict,
                         out_model: Path) -> None:
    """落 Step 5/7 冻结契约：21 维顺序 + 超参快照 + 产物路径。"""
    contract = {
        "selected_features": selected,
        "n_features": len(selected),
        "hyperparams": hyperparams,
        "objective": "huber",
        "alpha": 0.5,
        "target": "hypothetical_pnl",
        "out_model": str(out_model),
        "created": datetime.now().isoformat(timespec="seconds"),
        "source": "logs/stage1_optuna_best.json (Step 4)",
        "note": "Step 6/7 的唯一特征契约源；predict.py 的 polars 路径须产出这些列",
    }
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("冻结契约已写: %s（%d 维）", path.name, len(selected))


# --------------------------------------------------------------------------- #
# 训练
# --------------------------------------------------------------------------- #
def _build_params(hyperparams: dict) -> dict:
    params = dict(DEFAULT_LGBM)
    params.update(hyperparams)
    params["objective"] = "huber"
    params["alpha"] = 0.5
    params["random_state"] = 42
    return params


def _fit(X, y, params, early_stopping_rounds=None, X_valid=None, y_valid=None):
    """统一拟合封装。早停时返回 (model, best_iteration)；否则 (model, n_estimators)。"""
    import lightgbm as lgb

    model = lgb.LGBMRegressor(**params)
    if early_stopping_rounds is not None and X_valid is not None:
        model.fit(
            X, y, eval_set=[(X_valid, y_valid)],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
        )
        return model, int(model.best_iteration_)
    model.fit(X, y)
    return model, int(params.get("n_estimators", model.n_estimators_))


def train_and_save(
    df: pd.DataFrame,
    selected: list[str],
    hyperparams: dict,
    out_model: Path,
    holdout_cutoff: pd.Timestamp,
    top_ns: list[int],
) -> dict:
    """重训并落盘，返回评估/元信息字典。"""
    import lightgbm as lgb

    params = _build_params(hyperparams)
    log.info("训练协议: LGBMRegressor huber α=0.5 | 超参=%s",
             {k: params[k] for k in hyperparams})

    # 必须保留为 DataFrame（而非 .values），否则 LightGBM 会把特征名记为
    # Column_0..N，导致 predict.py 按 model.feature_name() 取列时 KeyError。
    X_all = df[selected].astype(float)
    y_all = df["hypothetical_pnl"].astype(float).values
    dates = df["_date"].values

    # ---- 1) 末折 holdout 选轮（train < cutoff, valid ≥ cutoff）----
    tr = dates < np.datetime64(holdout_cutoff)
    va = dates >= np.datetime64(holdout_cutoff)
    if tr.sum() < 500 or va.sum() < 50:
        raise ValueError(
            f"holdout 切分样本不足（train={int(tr.sum())}, valid={int(va.sum())}）；"
            f"请检查 cutoff={holdout_cutoff.date()}")
    log.info("holdout 选轮: train=%d, valid(>=%s)=%d",
             int(tr.sum()), holdout_cutoff.date(), int(va.sum()))
    _, best_iter = _fit(X_all[tr], y_all[tr], params,
                        early_stopping_rounds=50,
                        X_valid=X_all[va], y_valid=y_all[va])
    log.info("早停选定 best_iteration = %d", best_iter)

    # ---- 2) 全量模型（生产权重）----
    params_final = dict(params)
    params_final["n_estimators"] = best_iter
    prod_model, _ = _fit(X_all, y_all, params_final)
    log.info("全量模型训练完成（%d 棵树）", best_iter)

    # ---- 3) 落盘（先守卫，再保存）----
    guard_output_path(out_model)
    out_model.parent.mkdir(parents=True, exist_ok=True)
    if out_model.exists():
        log.warning("覆盖旧的 stage1 产物（不影响旧 vpnl_huber）: %s", out_model.name)
    prod_model.booster_.save_model(str(out_model))
    log.info("★ 新模型已保存: %s", out_model)

    # ---- 4) 落盘校验：回读确认 21 维 + 列名一致 ----
    reloaded = lgb.Booster(model_file=str(out_model))
    reloaded_feats = reloaded.feature_name()
    if reloaded.num_feature() != len(selected):
        raise RuntimeError(
            f"落盘校验失败：模型维数 {reloaded.num_feature()} ≠ 精选集 {len(selected)}")
    if reloaded_feats != selected:
        raise RuntimeError(
            f"落盘校验失败：列名顺序/集合不一致\n  模型: {reloaded_feats}\n  精选: {selected}")
    log.info("落盘校验通过：num_feature=%d，feature_name 与精选集一致",
             reloaded.num_feature())

    # ---- 5) 评估：holdout sanity（全量模型 on valid，偏乐观）+ 纯 OOS（仅<h 训练）----
    pred_valid = prod_model.predict(X_all[va], num_iteration=best_iter)
    sanity = _eval_fold(y_all[va], pred_valid, top_ns)
    sanity["cutoff"] = str(holdout_cutoff.date())

    # 纯 OOS：仅用 <holdout 训练，≥holdout 测试（clean 最近期 OOS，对照 Step5 fold1）
    oos_model, _ = _fit(X_all[tr], y_all[tr], params_final)
    pred_oos = oos_model.predict(X_all[va], num_iteration=best_iter)
    oos = _eval_fold(y_all[va], pred_oos, top_ns)
    oos["cutoff"] = str(holdout_cutoff.date())

    # ---- 6) 特征重要性（Top 15，gain）----
    imp = pd.Series(reloaded.feature_importance(importance_type="gain"), index=reloaded_feats) \
        .sort_values(ascending=False)
    importances = [(name, int(v)) for name, v in imp.head(15).items()]

    return {
        "out_model": str(out_model),
        "n_trees": best_iter,
        "n_samples": len(df),
        "data_date": str(df["_date"].max().date()),
        "holdout_sanity": sanity,   # 全量模型 on valid（曾选轮，偏乐观）
        "honest_oos": oos,         # 仅<h 训练 on valid（clean 最近期 OOS）
        "importances": importances,
        "feature_names": reloaded_feats,
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_report(info: dict, meta: dict) -> str:
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    sanity, oos = info["holdout_sanity"], info["honest_oos"]
    top_ns = meta["top_ns"]

    lines = [
        f"# 阶段1 Step 6 重训报告 (vpnl_huber_stage1) ({ts})",
        "",
        "## 📊 核心指标",
        "- 新模型：`vpnl_huber_stage1`（LGBMRegressor, huber α=0.5, 目标 hypothetical_pnl）",
        "- 特征：**21 维精选**（Step 4 选定，见冻结契约）",
        f"- 训练样本：**{info['n_samples']:,}**（宽表校验后全量）",
        f"- 树数（早停选定）：**{info['n_trees']}**",
        f"- **纯 OOS（仅<h 训练, ≥{oos['cutoff']} 测试）：Spearman={oos['spearman']:.4f}**",
        f"- 纯 OOS Top-20 PnL：**{oos['top'].get(20, {}).get('mean_pnl', float('nan')):.4f}**"
        f"（胜率 {oos['top'].get(20, {}).get('win_rate', float('nan'))*100:.1f}%）",
        f"- holdout sanity（全量模型 on valid，偏乐观）：Spearman={sanity['spearman']:.4f} / "
        f"Top20={sanity['top'].get(20, {}).get('mean_pnl', float('nan')):.4f}",
        f"- 落盘校验：num_feature={len(info['feature_names'])} 与精选集一致 ✅",
        f"- 旧模型：{meta['old_status']}",
        "",
        "## 🎯 Top N 明细（纯 OOS 最近期）",
        "",
        "| 指标 | test_n | Spearman | "
        + " | ".join(f"Top{k} PnL" for k in top_ns) + " |",
        "|---|---:|---:|" + "---:|" * len(top_ns),
    ]
    row = [f"纯 OOS (≥{oos['cutoff']})", str(oos["n"]), f"{oos['spearman']:.4f}"]
    row += [f"{oos['top'][k]['mean_pnl']:.4f}" for k in top_ns]
    lines.append("| " + " | ".join(row) + " |")
    srow = ["holdout sanity", str(sanity["n"]), f"{sanity['spearman']:.4f}"]
    srow += [f"{sanity['top'][k]['mean_pnl']:.4f}" for k in top_ns]
    lines.append("| " + " | ".join(srow) + " |")
    lines += [
        "",
        "## 📈 统计分布（特征重要性 Top 15）",
        "",
        "| 排名 | 特征 | 重要性 |",
        "|---|---|---:|",
    ]
    for i, (name, v) in enumerate(info["importances"], 1):
        lines.append(f"| {i} | `{name}` | {v} |")
    lines += [
        "",
        "## ⚠️ 风险提示",
        "1. 重训仅替换模型权重；旧 `vpnl_huber.txt` 已备份+只读，可随时回滚。",
        "2. 纯 OOS 数字为最近期（≥2025）单折估计，样本量有限，置信度低于 Step 5 多折池化。",
        "3. 历史回测不代表未来收益；2026 年新模式可能失效。",
        "4. 翻倍策略属激进策略，建议小资金启动，严格止损(-10%)。",
        "5. 北交所(920XXX)流动性低，滑点可能放大。",
        "6. **未切换**：本步仅产出新权重，predict.py 仍默认 `vpnl_huber`；Step 7 才切换（金丝雀后）。",
        "7. Step 7 必须用 **polars 特征路径**（候选因子 ret_skew_20/vol_term/vp_corr_20/amihud_20 "
        "仅该路径产出；legacy features_5d 缺这些列 → KeyError）。",
        "",
        "## 🔧 下一步建议",
        "1. **Step 7**：predict.py 加 `--features polars` 路径算全 35 维后按冻结契约截 21 列；"
        "注册表加 `vpnl_huber_stage1` 指向本新模型。",
        "2. **金丝雀**：`predict.py --model vpnl_huber_stage1 --features polars` 跑若干日，"
        "与旧模型 Top-20 分布/排序对比，确认无灾难性偏移。",
        "3. 通过后翻 `DEFAULT_MODEL`；`git tag stage1-switch`。",
        "4. 回滚：保留 `--model vpnl_huber`（旧文件只读，完好）。",
        "",
        "---",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "生成者：scripts/stage1_retrain.py",
        "模型版本：vpnl_huber_stage1",
        f"数据日期：{info['data_date']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="阶段1 Step 6 重训（vpnl_huber + 21 维精选）")
    p.add_argument("--config", type=str, default=str(DEFAULT_CONFIG),
                   help="Step 4 best.json 路径（selected_features + hyperparams）")
    p.add_argument("--out-model", type=str, default=str(OUT_MODEL_DEFAULT),
                   help="★ 新模型输出路径（唯一新写文件，默认 *_stage1.txt）")
    p.add_argument("--frozen", type=str, default=str(FROZEN_DEFAULT),
                   help="冻结契约输出路径（Step 5/7 契约源）")
    p.add_argument("--wide-table", type=str, default=str(DEFAULT_WIDE),
                   help="宽表 parquet 路径")
    p.add_argument("--cutoffs", type=str, default="2024-01-01,2025-01-01",
                   help="walk-forward cutoff；默认末 cutoff(2025-01-01) 作 holdout 选轮/测试")
    p.add_argument("--top-n", type=str, default="10,20,50", help="Top-N 列表")
    p.add_argument("--backup-old", action="store_true", default=True,
                   help="备份旧部署模型为 .bak（默认开；--no-backup-old 关闭）")
    p.add_argument("--no-backup-old", dest="backup_old", action="store_false",
                   help="不备份旧模型")
    p.add_argument("--protect-old", action="store_true", default=True,
                   help="旧部署模型加只读保护 0444（默认开；--no-protect-old 关闭）")
    p.add_argument("--no-protect-old", dest="protect_old", action="store_false",
                   help="不给旧模型加只读保护")
    p.add_argument("--require-gate", action="store_true", default=False,
                   help="要求最新 Step 5 闸门 JSON passed=true 才重训（默认不强制）")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "logs"), help="报告/日志目录")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def _latest_gate_passed() -> bool | None:
    """读取 logs/stage1_gate_*.json 最新一份，判断是否 passed。None=无闸门记录。"""
    jsons = sorted(ROOT.glob("logs/stage1_gate_*.json"))
    if not jsons:
        return None
    try:
        data = json.loads(jsons[-1].read_text(encoding="utf-8"))
        return bool(data.get("passed"))
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"stage1_retrain_{ts}.log"

    _fh = logging.FileHandler(log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), _fh],
    )

    try:
        # 0) 闸门前置（可选）
        if args.require_gate:
            gp = _latest_gate_passed()
            if gp is False:
                print("\n❌ RETRAIN_FAIL: 最新 Step 5 闸门未通过，拒绝重训", file=sys.stderr)
                return RETRAIN_FAIL
            if gp is None:
                log.warning("未找到 Step 5 闸门记录（--require-gate），继续")

        out_model = Path(args.out_model)
        old_status = "未改动（已备份+只读）"
        if out_model.resolve() == OLD_MODEL.resolve():
            print(f"\n❌ RETRAIN_FAIL: 输出路径与旧模型相同 {OLD_MODEL}，禁止覆盖旧模型",
                  file=sys.stderr)
            return RETRAIN_FAIL

        # 1) 旧模型保护（备份 + 只读）—— 在任何写入前完成
        backup_old_model(OLD_MODEL, args.backup_old)
        protect_old_model(OLD_MODEL, args.protect_old)

        # 2) 硬守卫：输出不命中任何旧模型
        guard_output_path(out_model)

        # 3) 配置 + 宽表
        cfg = load_config(Path(args.config))
        selected = cfg["selected_features"]
        hyperparams = cfg["hyperparams"]
        df = load_and_validate(Path(args.wide_table), ALL35)
        missing = [f for f in selected if f not in df.columns]
        if missing:
            raise ValueError(f"命名契约破坏：精选特征不在宽表列中 {missing}")
        log.info("命名契约 OK：%d 维全部存在于宽表", len(selected))

        cutoffs = sorted(pd.Timestamp(x.strip()) for x in args.cutoffs.split(",") if x.strip())
        holdout = cutoffs[-1]
        top_ns = [int(x) for x in args.top_n.split(",") if x.strip()]

        # 4) 重训 + 落盘 + 校验
        info = train_and_save(df, selected, hyperparams, out_model, holdout, top_ns)

        # 5) 冻结契约
        write_frozen_contract(Path(args.frozen), selected, hyperparams, out_model)

        # 6) 报告
        meta = {
            "top_ns": top_ns, "old_status": old_status,
            "holdout": str(holdout.date()),
        }
        out_json = out_dir / f"stage1_retrain_{ts}.json"
        out_md = out_dir / f"stage1_retrain_{ts}.md"
        result = {"meta": meta, "info": _jsonable(info)}
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        out_md.write_text(build_report(info, meta), encoding="utf-8")

        print("\n=== Step 6 重训完成 ===")
        print(f"新模型: {out_model}")
        print(f"树数: {info['n_trees']} | 样本: {info['n_samples']:,} | 维数: {len(selected)}")
        print(f"纯 OOS (≥{info['honest_oos']['cutoff']}): "
              f"Spearman={info['honest_oos']['spearman']:.4f} "
              f"Top20={info['honest_oos']['top'][20]['mean_pnl']:.4f}")
        print(f"旧模型: 完好（{OLD_MODEL.name} 已备份+只读）")
        print(f"报告: {out_md}")
        print(f"JSON: {out_json}")
        print("GATE_RESULT=RETRAIN_OK")
        return RETRAIN_OK

    except PermissionError as e:  # 安全守卫触发
        log.error("安全守卫拦截: %s", e)
        print(f"\n❌ RETRAIN_FAIL: {e}", file=sys.stderr)
        return RETRAIN_FAIL
    except Exception as e:
        log.error("重训异常: %s", e, exc_info=True)
        print(f"\n❌ RETRAIN_ERROR: {e}", file=sys.stderr)
        return RETRAIN_ERROR


def _jsonable(info: dict) -> dict:
    out = {k: v for k, v in info.items() if k not in ("holdout_sanity", "honest_oos", "importances")}
    out["holdout_sanity"] = {
        "cutoff": info["holdout_sanity"]["cutoff"], "n": info["holdout_sanity"]["n"],
        "spearman": info["holdout_sanity"]["spearman"],
        "top": {str(k): v for k, v in info["holdout_sanity"]["top"].items()},
    }
    out["honest_oos"] = {
        "cutoff": info["honest_oos"]["cutoff"], "n": info["honest_oos"]["n"],
        "spearman": info["honest_oos"]["spearman"],
        "top": {str(k): v for k, v in info["honest_oos"]["top"].items()},
    }
    out["importances"] = info["importances"]
    return out


if __name__ == "__main__":
    raise SystemExit(main())
