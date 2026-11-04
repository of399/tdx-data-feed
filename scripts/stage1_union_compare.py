#!/usr/bin/env python3
"""scripts/stage1_union_compare.py — stage1 特征集合的对照评测（B0 / B1 / B2）

要回答的问题只有一个：**加进 Alpha158 的净新增维，在纯 OOS 上有没有让排序变好**。
所以对照必须只动一条轴：

- 同一张宽表、同一批行（行集由 `load_and_validate(..., ALL35)` 固定，与契约无关）；
- 同一组超参与协议（`_build_params` + huber α=0.5 + `random_state=42` + 早停 50）；
- 同一个 cutoff 与同一段 OOS（train < cutoff，评 ≥ cutoff）；
- 只有 `selected_features` 不同。

判据不是「谁的数字大」，而是**逐日配对的差**：把每个契约在同一个交易日的横截面 Rank IC
算出来，同日相减得到 Δ 序列，再对 Δ 做 Newey-West（`maxlags` 跟标签的前瞻天数一致，
重叠标签的自相关长度就是 horizon）。池化的 Spearman 只说明整段平均，配不上任何显著性——
这与「IC 必须逐日算」是同一个坑（见 `tdxfeed/alpha158/ic.py` 与 AGENTS.md）。

Top-N 同样配对口径：每只契约按当日预测降序取前 k，统计**实际** `hypothetical_pnl` 的日均值，
再同日相减。池化 Top-N（`_eval_fold` 的口径）也一并给出，用来与 `stage1_retrain` 的报告
逐字对账——两边对不上就说明协议漂了，那次数值差不能当结论。

q 值是**比较集内**的 q：本脚本对 Δ 序列做 BH 时，比较集 = 传进来的非基线契约数。
2 个比较与 184 个比较下的 q 不是同一个数（KI-DATA-005 踩过），所以报告里连比较集大小一起写。

用法：
    venv/bin/python scripts/stage1_union_compare.py \\
        --wide-table data/double_up/stage1_wide_table_union47_hfq.parquet \\
        --contracts data/double_up/stage1_contract_b0.json \\
                  data/double_up/stage1_contract_b1.json \\
                  data/double_up/stage1_contract_b2.json \\
        --baseline b0 --tag hfq
退出码：0 成功；1 契约/宽表口径不符（缺维、行集不一致、样本不足）；2 异常。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stage1_retrain as sr  # noqa: E402  复用同一套协议（_build_params / _fit）
from stage1_baseline import ALL35, _eval_fold, load_and_validate  # noqa: E402

from quant.multifactor import cv_utils  # noqa: E402  全仓唯一的 Rank IC 实现
from tdxfeed.alpha158.ic import IcConfig, daily_ic, summarize_ic  # noqa: E402
from tdxfeed.atomic import atomic_write_text  # noqa: E402

log = logging.getLogger("stage1_union_compare")

# 吞错计数（SEM002）：与 stage1_retrain 同一形状——被交代的错误按函数名分桶，不静默
_ERROR_COUNT: Counter[str] = Counter()

LABEL = "label"

# 逐日横截面的下限：与 `tdxfeed/alpha158/ic.py::IcConfig.min_codes` 同一个数（30 只）。
# 事件样本每天只有几笔，达不到这条线时逐日配对 IC 直接判"不可用"，不许输出 NaN 表。
MIN_CODES_FOR_DAILY_IC = 30
# HAC 的天数下限：`cv_utils.ic_newey_west` 在 n<10 时直接返回 NaN（不是"不显著"，是"算不出"）
MIN_DAYS_FOR_HAC = 10
MetricFn = Callable[[pd.DataFrame, str], float]


def metric_rank_ic(oos: pd.DataFrame, cid: str) -> float:
    """池化 Rank IC（整段一起排）——与 `_eval_fold` 的 Spearman 同口径，便于对账。"""
    return float(cv_utils.rank_ic(oos[f"pred_{cid}"], oos[LABEL]))


def make_top_metric(k: int) -> MetricFn:
    """池化 Top-k 实际收益：整段按预测降序取前 k 笔，均值即"这一版模型挑的那几笔赚多少"。

    门槛看的是**样本**而不是 k：`_eval_fold` 的 `kk = min(k, n)` 在样本比 k 少时会缩档，
    缩到 1 笔也照样是个数（那是"没样本"，不是"收益小"），所以按 n 设下限，与 `rank_ic`
    「有效对不足 5 给 NaN」同一取向。
    """
    def fn(oos: pd.DataFrame, cid: str) -> float:
        if len(oos) < 5:
            return float("nan")
        col = f"pred_{cid}"
        take = min(k, len(oos))
        return float(oos.nlargest(take, col)[LABEL].mean())
    return fn


def contract_id(path: Path) -> str:
    """契约 id 取自 JSON 里的 `contract_id`，不取文件名——文件名可以随便改。"""
    body = json.loads(path.read_text(encoding="utf-8"))
    cid = body.get("contract_id") or path.stem
    return str(cid)


def fit_oos_predictions(df: pd.DataFrame, contracts: dict[str, dict],
                        cutoff: pd.Timestamp, cfg: IcConfig,
                        top_ns: list[int]) -> dict[str, Any]:
    """逐契约拟合「仅 <cutoff 训练」的 OOS 模型，返回预测面板与每契约的协议记录。

    行集只取一次（`df` 就是全部），所以三个契约天然打在同样的样本上；这一点必须当场
    断言，因为「B1 多出来的样本」与「B1 多出来的特征」在数字上长得一样。
    """
    dates = df["_date"].values
    y = df["hypothetical_pnl"].astype(float).values
    tr = dates < np.datetime64(cutoff)
    va = ~tr
    if int(va.sum()) < cfg.min_codes or int(tr.sum()) < 500:
        raise ValueError(f"OOS 切分样本不足（train={int(tr.sum())}, valid={int(va.sum())}）")

    panel = pd.DataFrame({
        "date": [str(d.date()) for d in df.loc[va, "_date"]],
        "code": df.loc[va, "code"].values,
        LABEL: y[va],
    })
    records: dict[str, dict] = {}
    for cid, cfg_body in contracts.items():
        selected = list(cfg_body["selected_features"])
        missing = [f for f in selected if f not in df.columns]
        if missing:
            raise ValueError(f"契约 {cid} 有 {len(missing)} 条维不在宽表：{missing[:8]}")
        params = sr._build_params(cfg_body["hyperparams"])
        X = df[selected].astype(float)
        _, best_iter = sr._fit(X[tr], y[tr], params, early_stopping_rounds=50,
                               X_valid=X[va], y_valid=y[va])
        params_final = dict(params)
        params_final["n_estimators"] = best_iter
        oos_model, _ = sr._fit(X[tr], y[tr], params_final)
        pred = oos_model.predict(X[va], num_iteration=best_iter)
        panel[f"pred_{cid}"] = pred
        pooled = _eval_fold(y[va], pred, top_ns)
        records[cid] = {
            "n_features": len(selected),
            "n_alpha158_added": int(cfg_body.get("n_alpha158_added", 0)),
            "best_iteration": best_iter,
            "pooled_oos": {
                "spearman": pooled["spearman"],
                "top": {str(k): v for k, v in pooled["top"].items()},
            },
            "label": cfg_body.get("label"),
        }
        log.info("契约 %s: %d 维（+%d Alpha158）best_iter=%d 池化 Spearman=%.4f",
                 cid, len(selected), records[cid]["n_alpha158_added"], best_iter,
                 pooled["spearman"])
    return {"panel": panel, "records": records, "n_train": int(tr.sum()),
            "n_valid": int(va.sum())}


def section_sizes(panel: pd.DataFrame) -> dict[str, int]:
    """逐日横截面的大小分布——决定「逐日配对 IC」这条判据在样本上到底能不能用。"""
    sizes = panel.groupby("date").size()
    return {"n_days": len(sizes), "median": int(sizes.median()),
            "p10": int(sizes.quantile(0.10)), "max": int(sizes.max()),
            "days_at_or_above_min_codes": int((sizes >= MIN_CODES_FOR_DAILY_IC).sum())}


def day_blocks(values: np.ndarray) -> list[np.ndarray]:
    """把行按「同一天」分块，块内保序。自助法只能整块抽，不能拆天。

    边界用逐元素 `!=` 判，不用 `np.diff`：日期在这一层是字符串（`panel["date"]` 就是
    ISO 串），`np.diff` 对 object/str 数组直接 TypeError——合成用例第一次跑就撞上。
    """
    arr = np.asarray(values)
    order = np.argsort(arr, kind="stable")
    sorted_vals = arr[order]
    if sorted_vals.dtype.kind in "OU":
        changed = sorted_vals[1:] != sorted_vals[:-1]
    else:
        changed = np.diff(sorted_vals) != 0
    boundaries = np.flatnonzero(changed) + 1
    return [order[start:stop] for start, stop in
            zip([0, *boundaries], [*boundaries, len(sorted_vals)], strict=True)]


def cluster_bootstrap_delta(oos: pd.DataFrame, cids: list[str], baseline: str,
                            metrics: dict[str, MetricFn], *, n_boot: int = 1000,
                            seed: int = 42) -> dict[str, dict]:
    """按交易日整块重抽的配对自助法：给每个「契约 − 基线」的指标差一个 CI 与 p。

    为什么不用逐行自助：同一天共享市场水位，逐行重抽把相关性当成独立性，CI 会窄到
    骗人。为什么不做逐日 t：这一层的样本是**事件**样本（每天几笔），横截面太薄，逐日
    Rank IC 给不出数（见 `section_sizes`）。整块重抽同时避开这两件事——天是抽样的最小
    单位，指标仍是池化的。

    p 是双侧经验 p：`2·min(P(Δ≤0), P(Δ≥0))`，并加 1 连续性校正（`1/(n_boot+1)` 是
    经验 p 的下界，报 0 等于声称"绝无可能"）。
    """
    blocks = day_blocks(oos["date"].to_numpy())
    n_days = len(blocks)
    rng = np.random.default_rng(seed)
    point = {cid: {name: fn(oos, cid) for name, fn in metrics.items()} for cid in cids}
    out: dict[str, dict] = {}
    for cid in cids:
        if cid == baseline:
            continue
        deltas: dict[str, list[float]] = {name: [] for name in metrics}
        for _ in range(n_boot):
            pick = rng.integers(0, n_days, n_days)
            sample = oos.iloc[np.concatenate([blocks[i] for i in pick])]
            for name, fn in metrics.items():
                deltas[name].append(fn(sample, cid) - fn(sample, baseline))
        entry: dict[str, Any] = {"point_delta": {
            name: point[cid][name] - point[baseline][name] for name in metrics}}
        ci: dict[str, dict] = {}
        for name, vals in deltas.items():
            arr = np.asarray(vals, dtype=float)
            arr = arr[np.isfinite(arr)]
            lo, hi = (np.percentile(arr, [2.5, 97.5]) if arr.size else (np.nan, np.nan))
            neg = float((arr <= 0).mean()) if arr.size else float("nan")
            pos = float((arr >= 0).mean()) if arr.size else float("nan")
            p = float(min(1.0, 2 * min(neg, pos) + 1.0 / (n_boot + 1))) if arr.size else float("nan")
            ci[name] = {"mean_delta": float(arr.mean()) if arr.size else float("nan"),
                        "ci_lo": float(lo), "ci_hi": float(hi), "p_boot": p,
                        "n_boot": int(arr.size)}
        entry["boot"] = ci
        entry["n_days_resampled"] = n_days
        out[f"{cid}_minus_{baseline}"] = entry
    return out


def paired_delta(ic_daily: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """逐日配对相减。某天基线或对照任一边给不出 IC 就是 NaN（不是 0）。"""
    base = ic_daily[baseline]
    out = {}
    for cid in ic_daily.columns:
        if cid == baseline:
            continue
        out[f"{cid}_minus_{baseline}"] = ic_daily[cid] - base
    return pd.DataFrame(out, index=ic_daily.index)


def summarize_pairing(ic_daily: pd.DataFrame, delta: pd.DataFrame,
                      baseline: str) -> dict[str, dict]:
    """每个契约：日均 IC；每个 Δ：均值 / HAC t / p / q（比较集 = 非基线契约数）。"""
    per_ic = summarize_ic(ic_daily).to_dict(orient="index")
    per_delta = summarize_ic(delta).to_dict(orient="index") if not delta.empty else {}
    for name, row in per_delta.items():
        row["comparison_set"] = int(delta.shape[1])
        # 配对完整性：Δ 有值的天必须两边都有值，天数不能多于任一侧
        a, b = name.split("_minus_")
        row["n_days_both_sides"] = int((ic_daily[a].notna() & ic_daily[b].notna()).sum())
        row["n_days_delta"] = int(row["n_days"])
    return {"ic": per_ic, "delta": per_delta}


def run(wide_table: Path, contract_paths: list[Path], baseline: str,
        cutoff: str, tag: str, top_ns: list[int], n_boot: int = 1000) -> dict[str, Any]:
    contracts = {contract_id(p): json.loads(p.read_text(encoding="utf-8"))
                 for p in contract_paths}
    if baseline not in contracts:
        raise ValueError(f"基线 {baseline!r} 不在契约里：{sorted(contracts)}")
    df = load_and_validate(wide_table, ALL35)
    cfg = IcConfig(label_col=LABEL, min_codes=MIN_CODES_FOR_DAILY_IC, maxlags=5)
    fitted = fit_oos_predictions(df, contracts, pd.Timestamp(cutoff), cfg, top_ns)
    panel = fitted["panel"]
    sizes = section_sizes(panel)

    pred_cols = [f"pred_{cid}" for cid in contracts]
    ic = daily_ic(panel, pred_cols, cfg)
    ic.columns = [c.removeprefix("pred_") for c in ic.columns]
    usable_days = int(ic.notna().any(axis=1).sum()) if not ic.empty else 0
    # 判据可用性要说出口，不许留一张 NaN 表：这条判据有两道门，任一不过都是"没验到"，
    # 不是"验出来是 0"（读者会把空表读成后者）——① 当天的横截面宽度（`min_codes`，同
    # `IcConfig`）；② 能给 HAC 的天数下限（`cv_utils.ic_newey_west` 在 n<10 时返回 NaN）。
    thin_sections = sizes["days_at_or_above_min_codes"] == 0
    if usable_days >= MIN_DAYS_FOR_HAC:
        reason = ""
    elif thin_sections:
        reason = (f"逐日横截面中位 {sizes['median']} 只、达标的天 {sizes['days_at_or_above_min_codes']}"
                  f"/{sizes['n_days']}，够不到 min_codes={MIN_CODES_FOR_DAILY_IC} ⇒ 逐日 Rank IC "
                  "一天都给不出。判定改用按交易日整块重抽的配对自助法：天是抽样的最小单位，"
                  "指标仍是池化的")
    else:
        reason = (f"横截面够宽（中位 {sizes['median']} 只）但只有 {usable_days} 天能算 IC，"
                  f"低于 HAC 的下限 {MIN_DAYS_FOR_HAC} 天 ⇒ 不做逐日判定，改用整块自助法")
    daily_avail = {
        "usable_days": usable_days,
        "min_days_for_hac": MIN_DAYS_FOR_HAC,
        "days_at_or_above_min_codes": sizes["days_at_or_above_min_codes"],
        "available": bool(usable_days >= MIN_DAYS_FOR_HAC),
        "reason": reason,
    }
    if daily_avail["available"]:
        delta = paired_delta(ic, baseline)
        stats = summarize_pairing(ic, delta, baseline)
    else:
        stats = {"ic": {}, "delta": {}}

    metrics: dict[str, MetricFn] = {"pooled_rank_ic": metric_rank_ic}
    metrics.update({f"top{k}": make_top_metric(k) for k in top_ns})
    boot = cluster_bootstrap_delta(panel, list(contracts), baseline, metrics,
                                   n_boot=n_boot)
    # q 依赖比较集（AGENTS 里那条坑）：本报告的家族 = 所有「非基线契约 × 指标」的 Δ 检验。
    # 不写清比较集，读者会把单个 p=0.034 当成家族无关的结论。
    names = sorted(boot)
    family = [boot[n]["boot"][m]["p_boot"] for n in names for m in metrics]
    reject, qvals = cv_utils.bh_fdr_correct(np.asarray(family, dtype=float), alpha=0.05)
    idx = 0
    for n in names:
        for m in metrics:
            cell = boot[n]["boot"][m]
            cell["q_boot"] = float(qvals[idx])
            cell["reject"] = bool(reject[idx])
            cell["comparison_set"] = len(family)
            idx += 1

    return {
        "meta": {
            "wide_table": str(wide_table), "tag": tag, "cutoff": cutoff,
            "baseline": baseline, "contracts": {cid: str(p) for cid, p in
                                                zip(contracts, contract_paths, strict=True)},
            "n_rows_all_contracts": len(df),
            "n_train": fitted["n_train"], "n_valid": fitted["n_valid"],
            "oos_first": str(panel["date"].min()), "oos_last": str(panel["date"].max()),
            "oos_days": int(panel["date"].nunique()),
            "section_sizes": sizes,
            "min_codes_per_day": cfg.min_codes, "hac_maxlags": cfg.maxlags,
            "n_boot": n_boot, "boot_seed": 42,
            "protocol": "LGBMRegressor huber α=0.5, random_state=42, 早停 50（与 "
                        "stage1_retrain 同一实现）",
        },
        "contracts": fitted["records"],
        "daily_ic_availability": daily_avail,
        "ic_daily": ({cid: {d: (None if pd.isna(v) else float(v))
                            for d, v in ic[cid].items()} for cid in contracts}
                     if daily_avail["available"] else {}),
        "ic": stats["ic"], "delta_ic": stats["delta"],
        "bootstrap": boot,
    }


def render_md(res: dict[str, Any]) -> str:
    m = res["meta"]
    sizes = m["section_sizes"]
    tops = sorted(int(k) for k in next(iter(res["contracts"].values()))["pooled_oos"]["top"])
    lines = [
        f"# stage1 特征集合对照（{m['tag']}）",
        "",
        f"- 宽表：`{m['wide_table']}`",
        f"- 行集：{m['n_rows_all_contracts']}（各契约同一批行，行集与契约无关）| "
        f"train={m['n_train']} / OOS={m['n_valid']}",
        f"- OOS 区间：{m['oos_first']} ~ {m['oos_last']}（{m['oos_days']} 天）",
        f"- 逐日横截面：中位 {sizes['median']} 只、p10 {sizes['p10']}、最大 {sizes['max']}"
        f"（判据线 min_codes={m['min_codes_per_day']}）",
        f"- 协议：{m['protocol']}",
        f"- 推断：按交易日整块重抽的配对自助法，n_boot={m['n_boot']}、seed={m['boot_seed']}",
        "",
        "## 池化 OOS（与 stage1_retrain 报告同口径，用于对账）",
        "",
        "| 契约 | 维数 | +Alpha158 | best_iter | Spearman | "
        + " | ".join(f"Top{k}" for k in tops) + " |",
        "|---|---:|---:|---:|---:|" + "---:|" * len(tops),
    ]
    for cid, r in res["contracts"].items():
        top = r["pooled_oos"]["top"]
        lines.append(
            f"| `{cid}` | {r['n_features']} | {r['n_alpha158_added']} | "
            f"{r['best_iteration']} | {r['pooled_oos']['spearman']:.4f} | "
            + " | ".join(f"{top[str(k)]['mean_pnl']:.4f}" for k in tops) + " |")

    lines += ["", "## 配对差：按交易日整块重抽（判定看这里）", ""]
    metric_names = list(next(iter(res["bootstrap"].values()))["boot"].keys())
    lines += ["| Δ | 指标 | 点差 | 自助均值 | 95% CI | p_boot | q_boot | 过线 |",
              "|---|---|---:|---:|---|---:|---:|---|"]
    for name, block in res["bootstrap"].items():
        for metric in metric_names:
            b = block["boot"][metric]
            lines.append(
                f"| `{name}` | {metric} | {block['point_delta'][metric]:+.4f} | "
                f"{b['mean_delta']:+.4f} | [{b['ci_lo']:+.4f}, {b['ci_hi']:+.4f}] | "
                f"{b['p_boot']:.3f} | {b['q_boot']:.3f} | "
                f"{'是' if b['reject'] else '否'} |")
    comp = next(iter(res["bootstrap"].values()))["boot"][metric_names[0]]["comparison_set"]
    lines += ["",
              f"> p_boot 是按交易日整块重抽的双侧经验 p；q_boot 是 BH 校正后的 q，"
              f"**比较集 = {comp}**（本报告的「非基线契约 × 指标」全体）。"
              "换比较集 q 就会变，引用时必须连着这个数一起写。"]

    avail = res["daily_ic_availability"]
    lines += ["", "## 逐日配对 Δ Rank IC", ""]
    if not avail["available"]:
        lines += [
            f"**本样本上不可用（能算 IC 的天数 {avail['usable_days']}，HAC 下限 "
            f"{avail['min_days_for_hac']} 天；横截面达标的天数 "
            f"{avail['days_at_or_above_min_codes']}）**",
            "",
            f"- {avail['reason']}。",
            "- 这张表不给数不是回避：横截面只有几笔时 Spearman 的抽样噪声比效应本身还大，"
            "把它平均出来只会得到一个看起来像结论的数（同 `IcConfig.min_codes` 的取舍）。",
            "- 因子层面的逐日横截面证据在**全市场面板**那一轮已经给过（见 "
            "`docs/research/2026-11-04-alpha158-vs-feat26-ic.md`）；本轮是**模型层**的对照。",
        ]
    else:
        lines += ["| Δ | n_days | 均值 Δ | HAC t | p | q（比较集） | 过线 |",
                  "|---|---:|---:|---:|---:|---:|---|"]
        for name, row in res["delta_ic"].items():
            lines.append(
                f"| `{name}` | {row['n_days']} | {row['mean_ic']:+.4f} | "
                f"{row['nw_t']:+.2f} | {row['nw_p']:.3f} | {row['q_value']:.3f}"
                f"（{row['comparison_set']}） | {'是' if row['reject'] else '否'} |")
        lines += ["", "### 各契约日均 Rank IC", "",
                  "| 契约 | n_days | 均值 IC | ICIR | 正率 |", "|---|---:|---:|---:|---:|"]
        for cid, row in res["ic"].items():
            lines.append(f"| `{cid}` | {row['n_days']} | {row['mean_ic']:+.4f} | "
                         f"{row['icir']:.3f} | {row['pos_rate']:.3f} |")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="stage1 特征集合对照评测")
    p.add_argument("--wide-table", type=str, required=True)
    p.add_argument("--contracts", type=str, nargs="+", required=True,
                   help="契约 JSON（含 contract_id / selected_features / hyperparams）")
    p.add_argument("--baseline", type=str, default="b0")
    p.add_argument("--cutoff", type=str, default="2025-01-01")
    p.add_argument("--top-n", type=str, default="10,20,50")
    p.add_argument("--n-boot", type=int, default=1000,
                   help="按交易日整块重抽的次数（配对差的 CI/p 由它给）")
    p.add_argument("--tag", type=str, default="hfq")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "logs"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        res = run(Path(args.wide_table), [Path(x) for x in args.contracts],
                  args.baseline, args.cutoff, args.tag,
                  [int(x) for x in args.top_n.split(",") if x.strip()],
                  n_boot=args.n_boot)
    except ValueError as e:
        # SEM002：吞错必计数——口径不符虽以退出码 1 交代，仍要留一条可统计的记录
        _ERROR_COUNT["main"] += 1
        print(f"❌ COMPARE_FAIL: {e}（已计 {_ERROR_COUNT['main']} 次）", file=sys.stderr)
        return 1
    out_json = out_dir / f"stage1_union_compare_{args.tag}.json"
    out_md = out_dir / f"stage1_union_compare_{args.tag}.md"
    atomic_write_text(out_json, json.dumps(res, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    atomic_write_text(out_md, render_md(res), encoding="utf-8")
    print(render_md(res))
    print(f"\nJSON: {out_json}\nMD:   {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
