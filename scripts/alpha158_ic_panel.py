#!/usr/bin/env python
"""步骤③：全市场日频面板 + 远期 hfq 收益标签——158 维与现役 26 维的 IC 对照。

    venv/bin/python scripts/alpha158_ic_panel.py --limit 5          # 冒烟（5 只）
    systemd-run --user --quiet --scope -p MemoryMax=10G -p MemorySwapMax=3G \
        venv/bin/python scripts/alpha158_ic_panel.py               # 正式跑

判据是 `tdxfeed/alpha158/ic.py`（逐日横截面 Rank IC + ICIR + Newey-West + BH-FDR），
四个统计量全部复用 `quant/multifactor/cv_utils.py`，这里不另写一份。

样本与口径（每条都是选出来的，不是默认的）
----------------------------------------
**面板**：横截面 = 抽样的沪深 A 股，逐日一行；标签 = 同一只票 t→t+horizon 的
**hfq 收盘收益**（`tdxfeed.adjusted.frame(kind="hfq")` 的读侧权威，不是 raw，也不是
adj_factor）。特征与标签同口径，否则价格类因子的 IC 里混着复权口径差。

**两套特征喂同一批 (code, date) 行**：158 维由 `config/alpha158_expressions.json`
经 `tdxfeed.alpha158.eval` 装配；26 维直接调现役生产函数
`tdxfeed.features_5d.compute_window_features_extended`。各用不同样本算出来的 IC
不能并排比，所以两套在同一次循环里出。

四个会静默出错、必须钉住的点：
① `compute_window_features_extended` 的 `is_new` 读的是 **sig_idx 在传入帧里的位置**
   （`sig_idx < 250`）。必须传整段历史、用绝对索引；先把窗口切短再传，"上市未满一年"
   这个标记会被整体改成一个常数。守卫 tests/test_alpha158_ic_panel.py。
② 价格列走 hfq，`volume`/`amount` 保持 raw——`adjusted.PRICE_COLS` 只含四个价格，
   这也正是 qlib 侧 `$volume` 的口径（不是复权量）。
③ vwap 只能由 `amount/volume` 推（工件 caveat ②）；`volume=0` 的日子给 **NaN** 不给 0，
   否则 `Corr`/`Mean` 会把停牌日当成一个真实的 vwap=0 观测。
④ 时窗默认在 **2026-09-16 之前结束**（`tdxfeed.amount_fix.PROXY_CALIBER_START`，成交额
   代理化那天）：之后 `amount` 是 `close×volume` 的代理值，vwap 恒等于 close，26 维里
   5 个 amount 派生维一起退化。把代理段放进主样本，等于让"因子有没有信号"和"成交额
   是不是真的"两件事混进同一个数；代理段用 `--include-proxy` 单独跑一遍做敏感性对照。

抽样与精度
----------
FEAT26 是逐点函数（实测 2.4 ms/点），全市场 × 全时窗要跑两小时以上，所以按代码随机
抽样（固定 seed，可复现）。截面精度由每天的公司数决定，时序精度由天数决定：1,200 只/天
⇒ 单日 IC 抽样噪声 ≈ 1/√1199 ≈ 0.029；horizon=5 的标签重叠使有效独立天数 ≈ 天数/5，
660 个交易日 ⇒ 均值噪声 ≈ 0.0025。分辨 |IC| 差 0.01 够用，差 0.002 不够——报告里
低于这个尺度的排序差不要读成结论。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.multifactor import cv_utils  # noqa: E402  # 统计量单源，不在这里重写
from tdxfeed import adjusted, paths  # noqa: E402
from tdxfeed.alpha158.eval import evaluate_frame  # noqa: E402
from tdxfeed.alpha158.ic import (  # noqa: E402
    DEDUP_THRESHOLD,
    IcConfig,
    daily_ic,
    independent_subset,
    summarize_ic,
)
from tdxfeed.amount_fix import PROXY_CALIBER_START  # noqa: E402  # 单源，不在此重述日期
from tdxfeed.atomic import atomic_write_frame, atomic_write_text  # noqa: E402  # SEM001
from tdxfeed.features_5d import compute_window_features_extended  # noqa: E402
from tdxfeed.names import universe_codes  # noqa: E402  # 股票池唯一入口
from tdxfeed.stage1_wide_table import FEAT26_ORDER  # noqa: E402

PROXY_DATE = datetime.strptime(str(PROXY_CALIBER_START), "%Y%m%d").date()
# 主样本的默认终点：代理化前一天。写成日期而不是字面量，PROXY_CALIBER_START 一动它跟着动。
PRE_PROXY_END = (PROXY_DATE - timedelta(days=1)).isoformat()

# 吞错计数（SEM002）：单只票读不动不终止整轮，但必须计入——否则「样本 1200 只」这句话
# 里有多少只其实没进面板，没人知道。
_ERROR_COUNT: Counter[str] = Counter()

# 26 维里由 amount 派生的维度。这份名单不是随手列的：
# tests/test_alpha158_ic_panel.py 逐条核对 features_5d 源码里这些维的定义确实读 amount，
# 改名或改口径都会让守卫红。代理段的污染面 = 这 5 维 + Alpha158 的 $vwap 维（从工件点名）。
FEAT26_AMOUNT_DIMS = ["avg_amount_5d", "vol_ratio_t1", "turnover_proxy",
                      "amount_surge_3d", "amt_per_price"]


def vwap_dims_from_artifact(payload: dict) -> list[str]:
    """从 ① 工件点名哪些维度用了 `$vwap`——代理段的伪信号面就是这几维。"""
    return [f["name"] for f in payload["features"] if "$vwap" in f["expr"]]


# 两套清单里"语义上最接近"的维度对。列出来不是为了合并它们，而是为了暴露**符号约定**：
# qlib 的 ROC* 写成 `Ref($close,N)/$close`（过去价 ÷ 现价），和现役 ret_5d
# （现价 ÷ 过去价）是倒数关系。按直觉把 ROC5 读成"N 日涨幅"的人，IC 符号会整个反过来。
PARITY_PAIRS = [("ret_5d", "ROC5"), ("volatility_5d", "STD5"),
                ("max_5d_pct", "MAX5"), ("close_pos_5d", "MIN5"),
                ("avg_amount_5d", "VWAP0")]


def parity_rows(panel: pd.DataFrame, pairs: Sequence[tuple[str, str]], *,
                date_col: str = "date", min_codes: int = 10) -> list[dict]:
    """每对维度的**日内横截面**相关，按天平均——不是把整张面板堆一起算。

    这正是 `tdxfeed/alpha158/ic.py` 里那条判据的同一个坑：面板里 `avg_amount_5d` 是
    几千万的量级、`VWAP0` 是 1 附近的比值，pooled 相关会被"哪只票、哪天"的水位差主导，
    给出一个与语义无关的数（实测 pooled 给 -0.72，日内给 ~0）。逐日算完再平均，量纲与
    水位都消掉了，剩下的才是两维在同一批票上的同向程度。

    整列缺失或当天方差为 0 时给 NaN 并**保留这一行**：漏掉一对比给不出数更糟——读者会
    以为这两维没关系，其实是没验。
    """
    rows = []
    for left, right in pairs:
        if left not in panel.columns or right not in panel.columns:
            rows.append({"left": left, "right": right, "n_days": 0, "n_rows": 0,
                         "mean_corr": float("nan"), "note": "缺列"})
            continue
        per_day = []
        for _, sub in panel.groupby(date_col, sort=True):
            s = sub[[left, right]].dropna()
            if len(s) < min_codes or s.nunique().max() < 2:
                continue
            r = s[left].corr(s[right], method="spearman")
            if pd.notna(r):
                per_day.append(float(r))
        if not per_day:
            rows.append({"left": left, "right": right, "n_days": 0,
                         "n_rows": len(panel), "mean_corr": float("nan"),
                         "note": "样本/方差不足"})
            continue
        mean_corr = float(np.mean(per_day))
        rows.append({"left": left, "right": right, "n_days": len(per_day),
                     "n_rows": len(panel), "mean_corr": mean_corr,
                     "note": "反向" if mean_corr < -0.2
                     else ("同向" if mean_corr > 0.2 else "无关")})
    return rows


def select_codes(sample: int, seed: int) -> list[str]:
    """确定性抽样：股票池（唯一入口）∩ 日线在场 ∩ hfq 因子文件在场。

    先筛后抽：抽到的票若没有 hfq 覆盖就得整只丢掉，那样"样本 1200 只"就不是 1200 只。
    """
    codes = [c for c in universe_codes()
             if paths.daily_path(c).exists() and paths.hfq_factor_path(c).exists()]
    if sample >= len(codes):
        return sorted(codes)
    return sorted(random.Random(seed).sample(sorted(codes), sample))


def forward_return(close: pd.Series, horizon: int) -> pd.Series:
    """t→t+horizon 的收盘收益。**标签算式只有这一份**。

    装配侧（`build_code_panel`）与复用侧（`attach_horizon_labels`）都必须走它：
    多 horizon 分析的全部前提就是「换 horizon 只换标签列、特征一格不动」，
    两份 shift 会在尾段悄悄分叉。尾部不足 horizon 天自然是 NaN——不是 0。
    """
    return close.astype(float).shift(-int(horizon)) / close.astype(float) - 1.0


def attach_horizon_labels(panel: pd.DataFrame, closes: dict[str, pd.Series], *,
                          horizons: Sequence[int],
                          primary: int | None = None) -> tuple[pd.DataFrame, dict]:
    """给已有面板加 `label_h{N}` 列：只读价格帧，不重算 158+26 维特征。

    三条记账（都是「错了不报错、只静默给可看数字」的地方）：

    ① `mismatch`——面板已有的 `label` 与 `label_h{primary}` 不一致的格数。这是两条路径
       唯一的咬合点：日期没对齐、拿成 raw 价、shift 轴错都会在这里红。容差按 float32
       给（面板落盘是 float32），所以它抓的是**口径**不是精度。
    ② `unmatched_rows`——面板里有这一天、价格帧里没有 ⇒ NaN 并计数，不静默错位对齐。
    ③ `codes_without_frame`——整只票没有帧 ⇒ 标签整列 NaN + 计数；**不填 0、不丢行**。
       丢行会悄悄改变横截面，填 0 是把「不知道」写成「知道没有」。
    """
    hs = [int(h) for h in horizons]
    if not hs:
        raise ValueError("horizons 为空：没有标签列的面板不能出 IC")
    primary = min(hs) if primary is None else int(primary)
    out = panel.copy()
    for h in hs:
        out[f"label_h{h}"] = np.nan
    stats = {"mismatch": 0, "unmatched_rows": 0, "codes_without_frame": 0,
             "primary": primary, "horizons": hs}
    has_label = "label" in out.columns
    tol = 1e-5                                  # 面板 label 落盘是 float32
    for code, sub in out.groupby("code", sort=True):
        s = closes.get(str(code))
        if s is None or len(s) == 0:
            stats["codes_without_frame"] += 1
            continue
        pos = s.index.get_indexer(sub["date"].astype(str))
        base = pos >= 0
        stats["unmatched_rows"] += int((~base).sum())
        for h in hs:
            fwd = forward_return(s, h).to_numpy()
            vals = np.where(base, fwd[np.clip(pos, 0, len(fwd) - 1)], np.nan)
            out.loc[sub.index, f"label_h{h}"] = vals
        if has_label and primary in hs:
            a = out.loc[sub.index, "label"].astype(float).to_numpy(dtype=float)
            b = out.loc[sub.index, f"label_h{primary}"].to_numpy(dtype=float)
            both_nan = np.isnan(a) & np.isnan(b)
            differ = (np.isnan(a) != np.isnan(b)) | (
                ~np.isnan(a) & ~np.isnan(b) & ~np.isclose(a, b, rtol=tol, atol=1e-9))
            stats["mismatch"] += int((differ & ~both_nan).sum())
    return out, stats


def build_code_panel(raw: pd.DataFrame, hfq: pd.DataFrame, code: str, *,
                     start: str, end: str, horizon: int, payload: dict,
                     feat_cols: Sequence[str] | None = None) -> pd.DataFrame:
    """单只票的面板切片：逐日 (date, code, label, 26 维, 158 维)。

    传整段历史帧、按日期挑行、用绝对 sig_idx——见模块 docstring ①。尾部不足 horizon
    天的行标签为 NaN 并丢掉：没有标签的行不构成证据，留着等于用 0 冒充"无信号"。
    """
    cols = list(FEAT26_ORDER if feat_cols is None else feat_cols)
    raw = raw.sort_values("date").reset_index(drop=True)
    hfq = hfq.sort_values("date").reset_index(drop=True)
    if len(raw) != len(hfq) or not (raw["date"].to_numpy() == hfq["date"].to_numpy()).all():
        raise ValueError(f"{code}: raw 与 hfq 帧的日期不一致，逐维分派无从对齐")

    px = hfq[["date", "open", "high", "low", "close", "volume"]].copy()
    # `$vwap` 必须与 `$close` 同口径，两件容易漏的事：① 日线 volume 的单位是**手**，
    # amount/volume 得到每股价的 100 倍；② hfq 帧的价 = raw × 当日因子。所以先按股、
    # 再按当日因子 rebase 到 hfq。少了 ② 时 VWAP0 = (raw vwap/raw close)/因子——截面排序
    # 九成由复权因子决定（KI-DATA-005：实测日内 corr 中位 0.949，656 天全部 >0.9），
    # 一条本来过 BH 的维会被静默压成不显著；少了 ① 只是整体等比，不影响秩但影响水平。
    per_share = raw["amount"].astype(float) / (raw["volume"].replace(0, np.nan) * 100.0)
    factor = hfq["close"].astype(float) / raw["close"].astype(float).replace(0, np.nan)
    px["vwap"] = per_share * factor
    a158 = evaluate_frame(px, payload=payload)

    fwd = forward_return(hfq["close"], horizon)

    keep: list[int] = []
    feats: list[dict] = []
    for i in range(len(raw)):
        d = str(raw["date"].iloc[i])
        if not start <= d <= end:
            continue
        row = compute_window_features_extended(raw, code, sig_idx=i, frame_hfq=hfq)
        if row is None:
            continue
        keep.append(i)
        feats.append(row)
    if not keep:
        return pd.DataFrame()

    out = pd.DataFrame({
        "date": [str(raw["date"].iloc[i]) for i in keep],
        "code": code,
        "label": [float(fwd.iloc[i]) for i in keep],
    })
    # 按 cols 取列：dict 的键序不是契约，宽表的列序才是
    f26 = pd.DataFrame(feats)[cols].reset_index(drop=True)
    a158_sub = a158.iloc[keep].reset_index(drop=True)
    dup = set(out.columns) & (set(cols) | set(a158_sub.columns))
    if dup:
        raise ValueError(f"{code}: 标签/键列与特征重名 {sorted(dup)}")
    out = pd.concat([out, f26, a158_sub], axis=1)
    return out.dropna(subset=["label"]).reset_index(drop=True)


def ic_redundancy(daily: pd.DataFrame, a158: Sequence[str],
                  f26: Sequence[str]) -> dict:
    """两套信号的**信息重叠**：逐日 IC 序列之间的相关。

    为什么单列这一层：158 维里 QTLD60/MA60/MIN60/VMA60 是同一件事的不同写法，它们的
    IC 会在同一天一起响。"多少维显著"因此不等于"多少条独立信息"，跨集合比较必须
    同时报重叠度，否则读者会把 132/158 与 18/26 当成信息量的比。

    用 Pearson（不是 Spearman）量 IC 序列：这里问的是"两天的信号强度是否线性同行"，
    序列本身已经是相关的估计值，再排一次秩会把弱同号关系读成强关系。
    """
    cols = [c for c in daily.columns]
    corr = daily[cols].corr()
    a = [c for c in a158 if c in corr.columns]
    f = [c for c in f26 if c in corr.columns]
    # 整列没有 IC 的维（常数维、未覆盖维）不参与相关：pandas 会给 NaN，而 NaN 一旦进了
    # mean/median，"重叠度 0.35" 就变成 "重叠度 nan"——把可算的量弄丢了。
    dead = [c for c in cols if int(daily[c].notna().sum()) < 3]

    def mean_abs_within(names: list[str]) -> tuple[float, int]:
        if len(names) < 2:
            return float("nan"), 0
        m = np.abs(corr.loc[names, names].to_numpy())
        iu = np.triu_indices(len(names), k=1)
        vals = m[iu]
        vals = vals[np.isfinite(vals)]
        return (float(vals.mean()) if vals.size else float("nan")), int(vals.size)

    dup_a, pairs_a = mean_abs_within(a)
    dup_f, pairs_f = mean_abs_within(f)

    cross = []
    for set_name, src, other in (("Alpha158", a, f), ("FEAT26", f, a)):
        if not other:
            continue
        for c in src:
            vals = corr.loc[c, other].abs().dropna()
            if vals.empty:
                continue
            cross.append({"feature": c, "set": set_name,
                          "nearest": str(vals.idxmax()),
                          "max_abs_corr": float(vals.max())})
    maxima = [r["max_abs_corr"] for r in cross]
    return {"dup_within_alpha158": dup_a, "dup_within_feat26": dup_f,
            "pairs_used": {"alpha158": pairs_a, "feat26": pairs_f},
            "dims_without_ic": dead,
            "cross": cross,
            "cross_median": float(np.median(maxima)) if maxima else float("nan"),
            "cross_ge_08": int(sum(1 for v in maxima if v >= 0.8)),
            "cross_n": len(maxima)}


# 主口径 = 判据核的默认阈值（单源，不在这里重述 0.8）：报告里点名到维的清单只出一版，
# 三档并报的是「独立维数」，不是三张清单——否则读者会挑最有利的那档。
DEDUP_PRIMARY = DEDUP_THRESHOLD
DEDUP_THRESHOLDS = (0.7, DEDUP_PRIMARY, 0.9)


def _fmt_ic(x: float) -> str:
    """效应量印成 4 位小数；NaN 印「—」而不是 `nan`——后者在表里读起来像一个值。"""
    return "—" if not np.isfinite(x) else f"{x:.4f}"


def _kept_stats(summary: pd.DataFrame, dims: list[str]) -> dict:
    """留下的这批维的效应量：中位 |IC|、过 BH 的条数、最大 |t|。

    只给「还剩几维」是不够的——去重的意义在于**独立的那几条还剩多少力**，
    条数与力必须并排看，否则会把「去掉冗余后中位 |IC| 反而上升」读成变好了。
    """
    rows = summary.loc[[d for d in dims if d in summary.index]]
    if rows.empty:
        return {"kept": 0, "kept_dims": [], "median_abs_ic": float("nan"),
                "reject": 0, "max_abs_t": float("nan")}
    ab = rows["mean_ic"].abs().to_numpy(dtype=float)
    t = rows["nw_t"].abs().to_numpy(dtype=float)
    return {"kept": len(rows), "kept_dims": list(rows.index),
            "median_abs_ic": float(np.median(ab)),
            "reject": int(rows["reject"].sum()),
            "max_abs_t": float(np.nanmax(t)) if np.isfinite(t).any() else float("nan")}


def _dedup_view(daily: pd.DataFrame, summary: pd.DataFrame, dims: list[str], *,
                threshold: float, seed: list[str]) -> dict:
    """一个方向的去重，并把「被谁丢掉」拆成 seed（另一套已有）与集内（同套重复）。

    被 seed 挡掉的那批**也带效应量**（`blocked_median_abs_ic`）：只有「净新增 vs 被挡」
    并排，才能判现役覆盖的到底是弱维还是强维。少这个数，读者会默认挡掉的都是杂讯——
    而本面板实测挡掉的 28 条里含 KLEN/STD5/STD10 这几条全表前列的维。
    """
    res = independent_subset(daily, dims, threshold=threshold, seed=seed)
    out = _kept_stats(summary, res["kept"])
    seed_set = set(res["seed"])
    blocked = [d for d, v in res["dropped"].items() if v["by"] in seed_set]
    twin = [d for d, v in res["dropped"].items() if v["by"] not in seed_set]
    rows = summary.loc[[d for d in blocked if d in summary.index]]
    ab = rows["mean_ic"].abs().to_numpy(dtype=float)

    def _pair(names: list[str]) -> list[dict]:
        return [{"dim": d, "by": res["dropped"][d]["by"],
                 "abs_corr": res["dropped"][d]["abs_corr"]} for d in names]

    out.update(dropped_by_seed=len(blocked),
               # 「被谁挡」点名到维，两份分开：seed（另一套已经有）与集内（同一套的替身
               # 换位）。条数答「挡了几条」，名字答「现役到底覆盖了什么」——少了名字，
               # 「挡掉 28 条」会被读成 28 条一一对应，而清单换人也会从「同族换位」
               # 被误读成「信息丢了」。
               blocked_by_seed=_pair(blocked),
               blocked_within=_pair(twin),
               dropped_within=len(twin),
               blocked_median_abs_ic=float(np.median(ab)) if ab.size else float("nan"),
               no_ic=len(res["no_ic"]),
               uncomparable=res["uncomparable"])
    return out


def dedup_views(daily: pd.DataFrame, summary: pd.DataFrame,
                a158: Sequence[str], f26: Sequence[str],
                thresholds: Sequence[float] = DEDUP_THRESHOLDS) -> dict:
    """去重后的独立信息：集合内条数 + 两个方向的**净新增**，三档阈值并报。

    「158 维带来多少独立信息」的答案不是「集合内剩 K 条」，而是
    `net_new`：先把现役 26 维里独立的那几条当 seed，再数 158 维还能留下几条。
    `reverse` 是同一判据的反方向（现役相对 158 维净新增几条）——只问单边就等于
    预先假设了借来的那套更划算。阈值三档并报，是为了让「独立维数」不被读成
    某一个 0.8 的 artifact。
    """
    a158_l, f26_l = list(a158), list(f26)
    out: dict[float, dict] = {}
    for thr in thresholds:
        within_a = _dedup_view(daily, summary, a158_l, threshold=thr, seed=[])
        within_f = _dedup_view(daily, summary, f26_l, threshold=thr, seed=[])
        out[float(thr)] = {
            "within": {"Alpha158": within_a, "FEAT26": within_f},
            "net_new": _dedup_view(daily, summary, a158_l,
                                   threshold=thr, seed=within_f["kept_dims"]),
            "reverse": _dedup_view(daily, summary, f26_l,
                                   threshold=thr, seed=within_a["kept_dims"]),
        }
    return out


def horizon_block(panel: pd.DataFrame, feature_cols: Sequence[str],
                  a158: Sequence[str], f26: Sequence[str], *,
                  horizon: int, min_codes: int, alpha: float) -> dict:
    """一个 horizon 的完整判据块：IC → 汇总 → 去重，参数只有标签列与 HAC 滞后在变。

    `maxlags=horizon` 是必须的：重叠标签的自相关长度就是 horizon，h=20 用 5 会低估
    标准误，h=1 用 5 是给不存在的自相关留余量。跨年/跨 horizon 的比较只有在这一条
    成立时才同类。
    """
    label_col = f"label_h{horizon}"
    if label_col not in panel.columns:
        raise KeyError(f"面板没有 {label_col}：先用 --extra-horizons 加标签列再出 IC")
    cfg = IcConfig(label_col=label_col, min_codes=min_codes,
                   maxlags=horizon, alpha=alpha)
    daily = daily_ic(panel, feature_cols, cfg)
    summary = summarize_ic(daily, cfg)
    ded = dedup_views(daily, summary, a158, f26)
    prim = ded[DEDUP_PRIMARY]

    def _set_stat(cols: Sequence[str], col: str) -> float | int:
        rows = summary.loc[[c for c in cols if c in summary.index]]
        if col == "reject":
            return int(rows["reject"].sum())
        return float(rows["mean_ic"].abs().median()) if len(rows) else float("nan")

    nn = prim["net_new"]
    return {
        "horizon": int(horizon), "label_col": label_col,
        "days_with_ic": len(daily),
        # 噪声门槛是本块自己的数：`maxlags=h` 变了，HAC 标准误就跟着变，
        # 拿主口径那条 SE 去判别的 horizon 等于换一个不存在的尺子。
        "noise_scale": float(summary["nw_se"].median()),
        "median_abs_ic": {"Alpha158": _set_stat(a158, "median"),
                          "FEAT26": _set_stat(f26, "median")},
        "reject": {"Alpha158": _set_stat(a158, "reject"),
                   "FEAT26": _set_stat(f26, "reject")},
        "within": {"Alpha158": prim["within"]["Alpha158"]["kept"],
                   "FEAT26": prim["within"]["FEAT26"]["kept"]},
        "net_new": {"kept": nn["kept"], "reject": nn["reject"],
                    "median_abs_ic": nn["median_abs_ic"],
                    "blocked_median_abs_ic": nn["blocked_median_abs_ic"],
                    "dropped_by_seed": nn["dropped_by_seed"],
                    "blocked_by_seed": list(nn["blocked_by_seed"]),
                    "blocked_within": list(nn["blocked_within"]),
                    "dropped_within": nn["dropped_within"],
                    "kept_dims": list(nn["kept_dims"])},
        "reverse": {"kept": prim["reverse"]["kept"],
                    "median_abs_ic": prim["reverse"]["median_abs_ic"],
                    "kept_dims": list(prim["reverse"]["kept_dims"])},
    }


def yearly_decay(daily: pd.DataFrame, summary: pd.DataFrame,
                 groups: dict[str, Sequence[str]], *,
                 maxlags: int) -> dict:
    """逐年衰减：每年给「该组维的 |mean IC| 中位」与「与全样本同号的占比」。

    为什么两个都要：只报均值会把「一半维在换向」掩盖成一个略低的数；同号率掉下来
    就说明这一组维的方向不稳，那它进候选集的风险和 |IC| 高低是两个问题。
    年份按**字符串**做键——这份东西要落进 run_meta 的 JSON，整数键过一遍 JSON 就变
    字符串，两种形状迟早让下游读错。

    `noise_scale` 是**该年该组自己的** HAC 标准误中位（判据与全样本同一份
    `cv_utils.ic_newey_west`，滞后由调用方给，函数不猜）。没有它，「2026 的中位 |IC|
    掉到 0.0121」会被读成「信号没了」——而那一年只有 171 天，门槛本身比全样本大一截。
    """
    records = [{"date": d, "ic": float(v), "factor": f}
               for f in daily.columns
               for d, v in zip(daily.index, daily[f].to_numpy(), strict=True)
               if np.isfinite(v)]
    by_year = cv_utils.ic_by_year(records)
    if by_year.empty:
        return {"years": [], "groups": {}}
    full = summary["mean_ic"]
    out: dict[str, dict] = {}
    years = sorted(int(y) for y in by_year.index)
    for name, dims in groups.items():
        cols = [c for c in dims if c in by_year.columns]
        per: dict[str, dict] = {"median_abs_ic": {}, "sign_agreement": {},
                                "n_dims": {}, "noise_scale": {}}
        for y in years:
            s = by_year.loc[y, cols].dropna()
            if s.empty:
                continue
            base = full.reindex(s.index)
            same = np.sign(s.to_numpy()) == np.sign(base.to_numpy())
            key = str(y)
            per["median_abs_ic"][key] = float(np.median(np.abs(s.to_numpy())))
            per["sign_agreement"][key] = float(np.mean(same))
            per["n_dims"][key] = int(s.size)
            sub = daily.loc[[d for d in daily.index if str(d).startswith(key)], list(s.index)]
            ses = [cv_utils.ic_newey_west(sub[c], maxlags=maxlags)["se_hac"]
                   for c in s.index]
            # 那年不足 10 天时 HAC 给 NaN：门槛就是 NaN，不要 `nanmedian` 全 NaN 切片
            # （会打 RuntimeWarning，而且更坏的是它看起来像一个算出来的门槛）
            per["noise_scale"][key] = (float(np.nanmedian(ses))
                                       if any(np.isfinite(ses)) else float("nan"))
        out[name] = per
    return {"years": [str(y) for y in years], "groups": out}


def overlapping_days(pre: pd.DataFrame, post: pd.DataFrame) -> list[str]:
    """两段日频 IC 共用的日期（升序）。跨断点位移的前提就是这份清单为空。"""
    return sorted({str(d) for d in pre.index} & {str(d) for d in post.index})


def proxy_shift(pre: pd.DataFrame, post: pd.DataFrame, dims: Sequence[str], *,
                maxlags: int, alpha: float = 0.05) -> dict:
    """成交额代理化断点前后的 IC 位移：两段用**同一判据**汇总，差另有自己的 SE。

    KI-DATA-001 从 2026-09-16 起把 `amount` 写成 `close×volume` 的代理值。受污染的维
    在这段里测出来的 IC 还能不能用，只能拿断点前同一条维对照——所以这里比的是**同一条维**
    在两段的 `mean_IC`，不是两个样本池的平均数。

    三条不能靠直觉的口径：

    ① 判据复用 `summarize_ic`（逐日 Rank IC + HAC + BH），两段各自跑一遍：换一份实现
       就等于换一把尺子，位移里会混进判据差异。
    ② `se_diff = hypot(se_pre, se_post)`：两段的日不重叠，差的方差是两边之和。代理段
       只有几十天，它的 SE 天然比主段大一截——拿主段门槛去判代理段会把噪声读成位移。
    ③ 任一段给不出 IC（整段常数、横截面太薄、该维不可比）⇒ 位移是 **NaN** 并计入
       `uncomparable`，绝不补 0。补 0 会把「没验到」冒充「口径改了但 IC 没动」，
       那正是这一层要回答的问题上最贵的一次误读。
    """
    cfg = IcConfig(min_codes=1, maxlags=maxlags, alpha=alpha)
    overlap = overlapping_days(pre, post)
    if overlap:
        # `se_diff = hypot(se_pre, se_post)` 的前提是两段的日互不重叠。共用交易日时
        # 两条估计量相关，按独立合成会**低估**差的 SE，把噪声读成位移。
        raise ValueError(f"两段日频 IC 有 {len(overlap)} 个重叠日（如 {overlap[:3]}）："
                         f"位移的 SE 合成前提（两段互不重叠）不成立，把窗口切干净再来")
    s_pre = summarize_ic(pre[list(dict.fromkeys(dims))], cfg)
    s_post = summarize_ic(post[list(dict.fromkeys(dims))], cfg)
    rows: list[dict[str, Any]] = []
    uncomparable = 0
    for dim in dict.fromkeys(dims):
        p, q = s_pre.loc[dim], s_post.loc[dim]
        a, b = float(p["mean_ic"]), float(q["mean_ic"])
        se_a, se_b = float(p["nw_se"]), float(q["nw_se"])
        missing = not (np.isfinite(a) and np.isfinite(b)
                       and np.isfinite(se_a) and np.isfinite(se_b))
        if missing:
            diff = se_diff = z = float("nan")
            uncomparable += 1
        else:
            diff = b - a
            se_diff = float(np.hypot(se_a, se_b))
            z = float(diff / se_diff) if se_diff > 0 else float("nan")
        rows.append({"dim": str(dim),
                     "pre_days": int(p["n_days"]), "pre_mean_ic": a, "pre_nw_t": float(p["nw_t"]),
                     "post_days": int(q["n_days"]), "post_mean_ic": b, "post_nw_t": float(q["nw_t"]),
                     "diff": diff, "se_diff": se_diff, "z": z, "missing": bool(missing)})
    return {"maxlags": int(maxlags), "alpha": float(alpha),
            "pre_days": len(pre), "post_days": len(post),
            "rows": rows, "uncomparable": int(uncomparable)}


def json_safe(obj):
    """递归把 NaN/Inf 换成 null。

    `json.dumps` 默认会写出裸 `NaN`——Python 读得回来，jq 和任何严格 JSON 解析器都会
    当场失败。台账与巡检脚本都用 jq 取字段，所以落盘前先洗干净。
    """
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def atomic_write_parquet(df: pd.DataFrame, path: Path) -> Path:
    """tmp → os.replace 写 parquet（SEM001）。

    不走 `tdxfeed.parquet_io.write_table`：那是五张在役表的入口契约（date 归一 date32、
    与备份语义绑定）。这里的 `date` 必须是 IC 层读到的那个 ISO 字符串，研究产物也不
    参与任何表级备份。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{path}.tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def panel_window(dates, *, start: str, end: str) -> tuple[str, str, bool]:
    """复用路径的时窗由**面板自己**决定：返回 (start, end, 是否改了)。

    产物名（tag）是从 start/end 派生的。装配路径的 start/end 是因、面板是果；复用路径
    反过来——面板是因，start/end 只是「我要给这批行出报告」的标签。还按命令行默认值打
    tag，就会把另一轮的 IC/汇总/报告原地盖掉（2026-11-04 实踩过一次：代理段面板 + 默认
    时窗 ⇒ 主面板那 656 天的四份派生产物被 24 天的内容覆盖，且没有任何报错）。
    """
    s = pd.Series(dates, dtype=str)
    lo, hi = str(s.min()), str(s.max())
    if not len(s) or lo == "nan":
        raise ValueError("面板没有可用日期，无法决定时窗")
    return (start, end, False) if (lo, hi) == (start, end) else (lo, hi, True)


def parse_horizons(spec: str, primary: int) -> list[int]:
    """把 `--extra-horizons` 的逗号串解析成**含 primary** 的升序去重列表。

    primary 必须在里面：报告的多 horizon 表要跟主口径那一块对得上，缺了它就没有
    「同一张特征表、同一个标签」的锚点。非正数直接拒——h=0 的标签是自己减自己，恒为 0。
    """
    try:
        p = int(primary)
    except (TypeError, ValueError) as e:
        raise ValueError(f"primary horizon 不是整数：{primary!r}") from e
    if p <= 0:
        raise ValueError(f"primary horizon 必须是正整数，实得 {p}")
    out = {p}
    for part in str(spec or "").split(","):
        token = part.strip()
        if not token:
            continue
        n = int(token)
        if n <= 0:
            raise ValueError(f"horizon 必须是正整数，实得 {n}")
        out.add(n)
    return sorted(out)


def run(args: argparse.Namespace) -> int:
    payload = json.loads((ROOT / "config" / "alpha158_expressions.json")
                         .read_text(encoding="utf-8"))
    a158_names = [f["name"] for f in payload["features"]]
    overlap = set(a158_names) & set(FEAT26_ORDER)
    if overlap:
        print(f"❌ 两套清单重名 {sorted(overlap)}：宽表会互相覆盖，必须先加前缀", file=sys.stderr)
        return 1

    if args.from_panel:
        # 装配一次几十分钟，报告的措辞与判据参数要能反复改：从落盘面板直接出 IC。
        panel = pd.read_parquet(args.from_panel)
        print(f"从面板复用：{Path(args.from_panel).name} → {panel.shape}", flush=True)
        lo, hi, changed = panel_window(panel["date"], start=args.start, end=args.end)
        if changed:
            print(f"⚠️ 时窗按面板取 [{lo}, {hi}]（命令行是 [{args.start}, {args.end}]）："
                  f"复用路径的产物名跟着数据走，否则这一轮的 IC/报告会原地覆盖另一轮的",
                  flush=True)
            args.start, args.end = lo, hi
        extra: dict = {"reused_panel": str(args.from_panel)}
        multi: list[int] = []
        if str(args.extra_horizons or "").strip():
            if args.limit:
                print("❌ --limit 与 --extra-horizons 不能同用：抽样只对装配路径有意义",
                      file=sys.stderr)
                return 1
            multi = parse_horizons(args.extra_horizons, args.horizon)
            t0 = time.perf_counter()
            closes: dict[str, pd.Series] = {}
            for code in sorted(set(panel["code"].astype(str))):
                try:
                    hfq = adjusted.frame(code, kind="hfq")
                except Exception as e:          # SEM002：计数并继续，不静默少一只
                    _ERROR_COUNT["attach_horizon_labels"] += 1
                    if _ERROR_COUNT["attach_horizon_labels"] <= 5:
                        print(f"  ! {code}: {type(e).__name__}: {str(e)[:60]}",
                              file=sys.stderr)
                    continue
                closes[code] = (hfq.assign(date=hfq["date"].astype(str))
                                .set_index("date")["close"].astype(float).sort_index())
            panel, stats = attach_horizon_labels(panel, closes, horizons=multi,
                                                 primary=args.horizon)
            if stats["mismatch"]:
                # 两条路径的标签算式必须逐格吻合；不吻合就是日期/口径/轴其中一个错了，
                # 下游每一个数都靠这个咬合点，宁可停下来。
                print(f"❌ 复用侧 label_h{args.horizon} 与面板 label 不一致 "
                      f"{stats['mismatch']} 格（unmatched_rows="
                      f"{stats['unmatched_rows']}, codes_without_frame="
                      f"{stats['codes_without_frame']}）：不要拿这张面板出多 horizon 结论",
                      file=sys.stderr)
                return 1
            extra["label_stats"] = stats
            extra["multi_horizons"] = multi
            extra["label_attach_sec"] = round(time.perf_counter() - t0, 1)
            print(f"标签列已加：{multi} · {extra['label_attach_sec']}s · "
                  f"缺基准行 {stats['unmatched_rows']} · 无帧票 {stats['codes_without_frame']}",
                  flush=True)
        return aggregate_and_write(panel, args, payload, extra=extra,
                                   write_panel=False, multi_horizons=multi)

    if str(args.extra_horizons or "").strip():
        print("❌ --extra-horizons 只配合 --from-panel 用：换标签列不该重算 158+26 维特征。"
              "先装配一次，再带 --from-panel 加标签。", file=sys.stderr)
        return 1
    codes = select_codes(args.sample, args.seed)
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("❌ 抽不到标的：股票池 / 日线 / hfq 三者交集为空", file=sys.stderr)
        return 1
    print(f"样本 {len(codes)} 只 · 时窗 [{args.start}, {args.end}] · "
          f"horizon={args.horizon} · seed={args.seed}", flush=True)

    chunks: list[pd.DataFrame] = []
    skipped: Counter[str] = Counter()
    t0 = time.perf_counter()
    for n, code in enumerate(codes, 1):
        try:
            raw = adjusted.frame(code, kind="raw")
            hfq = adjusted.frame(code, kind="hfq")
            if hfq.attrs.get("factor_missing") or hfq.attrs.get("factor_mismatch"):
                skipped["hfq 覆盖不足或口径不符"] += 1
                continue
            panel = build_code_panel(raw, hfq, code, start=args.start, end=args.end,
                                     horizon=args.horizon, payload=payload)
            if panel.empty:
                skipped["窗口内无有效行"] += 1
                continue
            chunks.append(panel)
        except Exception as e:                      # SEM002：计入并继续，不静默少一只
            _ERROR_COUNT["build_code_panel"] += 1
            skipped[f"{type(e).__name__}"] += 1
            if sum(skipped.values()) <= 5:
                print(f"  ! {code}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        if n % 50 == 0:
            print(f"  [{n}/{len(codes)}] 已装配 {len(chunks)} 只 · "
                  f"{time.perf_counter() - t0:.0f}s", flush=True)

    if not chunks:
        print("❌ 一只都没装配成功", file=sys.stderr)
        return 1
    panel = pd.concat(chunks, ignore_index=True)
    chunks.clear()
    print(f"面板 {panel.shape}（{panel['date'].nunique()} 天 × "
          f"{panel['code'].nunique()} 只）· 跳过 {dict(skipped)}", flush=True)
    return aggregate_and_write(
        panel, args, payload,
        extra={"skipped": dict(skipped), "error_count": dict(_ERROR_COUNT),
               "sample_requested": args.sample,
               "elapsed_sec": round(time.perf_counter() - t0, 1)})


def aggregate_and_write(panel: pd.DataFrame, args: argparse.Namespace,
                        payload: dict, *, extra: dict,
                        write_panel: bool = True,
                        multi_horizons: Sequence[int] = ()) -> int:
    """从面板出 IC、汇总、落盘、写报告。

    单独成一个函数是为了 `--from-panel`：装配一次 40 分钟，而报告的读法、符号约定、
    污染维标注这些是要反复改的——改措辞不该重跑特征。
    """
    a158_names = [f["name"] for f in payload["features"]]
    feature_cols = a158_names + list(FEAT26_ORDER)
    missing = [c for c in feature_cols if c not in panel.columns]
    if missing:
        print(f"❌ 面板缺 {len(missing)} 列（如 {missing[:5]}）：与当前清单不同源，"
              f"别拿它出 IC，重跑装配", file=sys.stderr)
        return 1

    cfg = IcConfig(label_col="label", min_codes=args.min_codes,
                   maxlags=args.horizon, alpha=args.alpha)
    daily = daily_ic(panel, feature_cols, cfg)
    summary = summarize_ic(daily, cfg)

    out_dir = Path(args.out_dir)
    tag = f"{args.start}_{args.end}_h{args.horizon}_n{args.sample}_s{args.seed}"
    meta = {
        "tag": tag, "start": args.start, "end": args.end, "horizon": args.horizon,
        "codes_built": int(panel["code"].nunique()),
        "days": int(panel["date"].nunique()), "rows": len(panel),
        "codes_per_day_median": float(panel.groupby("date")["code"].count().median()),
        "seed": args.seed, "min_codes": args.min_codes, "alpha": args.alpha,
        "include_proxy": args.include_proxy,
        "proxy_caliber_start": PROXY_DATE.isoformat(),
        "feat26_amount_dims": FEAT26_AMOUNT_DIMS,
        "alpha158_vwap_dims": vwap_dims_from_artifact(payload),
        "days_in_panel": int(panel["date"].nunique()),
        "days_with_ic": len(daily),
        "days_dropped_thin": int(panel["date"].nunique() - len(daily)),
        "parity": parity_rows(panel, PARITY_PAIRS),
        "redundancy": ic_redundancy(daily, a158_names, list(FEAT26_ORDER)),
        "dedup": dedup_views(daily, summary, a158_names, list(FEAT26_ORDER)),
        **extra,
    }
    if multi_horizons:
        hs = sorted({int(h) for h in multi_horizons})
        blocks: dict[str, dict] = {}
        for h in hs:
            blocks[str(h)] = horizon_block(
                panel, feature_cols, a158_names, list(FEAT26_ORDER),
                horizon=h, min_codes=args.min_codes, alpha=args.alpha)
        meta["horizons"] = blocks
        nn_dims = blocks.get(str(args.horizon), {}).get("net_new", {}).get("kept_dims", [])
        meta["yearly"] = yearly_decay(daily, summary, {
            "Alpha158": a158_names, "FEAT26": list(FEAT26_ORDER), "净新增": nn_dims},
            maxlags=args.horizon)
    if str(getattr(args, "against_daily", "") or "").strip():
        # 位移只在两段互不重叠时成立；把对照段的日频 IC 读进来，按同一判据各算一遍。
        against = pd.read_parquet(args.against_daily)
        if "date" in against.columns:
            against = against.set_index("date")
        contaminated = FEAT26_AMOUNT_DIMS + vwap_dims_from_artifact(payload)
        controls = [c for c in FEAT26_ORDER if c not in FEAT26_AMOUNT_DIMS]
        shared = overlapping_days(against, daily)
        if shared:
            # 在这里就判掉：`proxy_shift` 自己也会 raise，但调用侧先说清是哪几天，
            # 报错才有动作价值（否则只有一个异常文本，得回头打印 index）。
            print(f"❌ 对照段与本次窗口有 {len(shared)} 个重叠日（如 {shared[:3]}）："
                  f"位移的 SE 合成前提不成立，把 --against-daily 指向不重叠的那一段",
                  file=sys.stderr)
            return 1
        meta["proxy_sensitivity"] = proxy_shift(
            against, daily, [*contaminated, *controls], maxlags=args.horizon,
            alpha=args.alpha)
    if write_panel:
        atomic_write_parquet(panel.astype({c: "float32" for c in [*feature_cols, "label"]}),
                             out_dir / f"panel_{tag}.parquet")
    atomic_write_parquet(daily.reset_index(names="date"), out_dir / f"daily_ic_{tag}.parquet")
    atomic_write_text(out_dir / f"run_meta_{tag}.json",
                      json.dumps(json_safe(meta), ensure_ascii=False,
                                 indent=2, sort_keys=True, allow_nan=False))
    atomic_write_frame(summary.reset_index(), out_dir / f"ic_summary_{tag}.csv")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True), flush=True)

    write_report(summary, a158_names, list(FEAT26_ORDER),
                 out_dir / f"report_{tag}.md", meta)
    return 0


def report_lines(summary: pd.DataFrame, a158: list[str], f26: list[str],
                 meta: dict) -> list[str]:
    """报告正文（纯函数，好测）。"""
    s = summary.copy()
    s["abs_ic"] = s["mean_ic"].abs()
    # 耗时这条只在**真测过**的时候出现：装配路径有 elapsed_sec，复用路径只有加标签
    # 列的秒数；拿缺省 0 印成「耗时 0s」会被读成「这一轮不花钱」，而复用省下的只是装配。
    sec = meta.get("elapsed_sec")
    attach = meta.get("label_attach_sec")
    cost: str | None = None
    if sec is not None:
        cost = f"- 耗时 {float(sec):.0f}s"
    elif attach is not None:
        cost = (f"- 本轮不重跑装配（复用面板 {meta.get('reused_panel', '—')}）"
                f"· 加标签列 {float(attach):.1f}s")
    lines = [
        f"# Alpha158 × FEAT26 日频 IC 对照 · {meta['tag']}",
        "",
        f"- 样本 {meta['codes_built']} 只 / {meta['days']} 天 / {meta['rows']} 行"
        f"（请求 {meta.get('sample_requested', '—')} 只，"
        f"跳过 {meta.get('skipped', '复用已落盘面板')}）"
        f"，每天中位 {meta['codes_per_day_median']:.0f} 只",
        f"- 标签 t→t+{meta['horizon']} 的 **hfq** 收盘收益；`min_codes={meta['min_codes']}`，"
        f"BH α={meta['alpha']}，HAC `maxlags={meta['horizon']}`（重叠标签的自相关长度）",
        f"- 横截面够天的 {meta.get('days_with_ic', '—')} 天出 IC，"
        f"太薄被丢弃 {meta.get('days_dropped_thin', '—')} 天"
        f"（丢弃不是补 0：那是「没验到」，不是「无信号」）",
        *([cost] if cost else []),
        "",
        f"成交额代理段自 {meta['proxy_caliber_start']} 起（KI-DATA-001）：本次"
        f"{'**在样本内**' if meta['include_proxy'] else '**已排除**'}。受污染维："
        f"Alpha158 的 {meta['alpha158_vwap_dims']}（用 $vwap），FEAT26 的 "
        f"{meta['feat26_amount_dims']}（用 amount）",
        "",
        "| 集合 | 维度数 | 有 IC 的天数（中位） | \\|IC\\| 中位 | q<α 的维数 |",
        "|---|---|---|---|---|",
    ]
    for name, cols in (("Alpha158", a158), ("FEAT26", f26)):
        sub = s.loc[[c for c in cols if c in s.index]]
        lines.append(f"| {name} | {len(sub)} | {sub['n_days'].median():.0f} | "
                     f"{sub['abs_ic'].median():.4f} | {int(sub['reject'].sum())} |")
    for name, cols in (("Alpha158", a158), ("FEAT26", f26)):
        sub = s.loc[[c for c in cols if c in s.index]].sort_values(
            "abs_ic", ascending=False).head(15)
        lines += ["", f"## {name}：|IC| 前 15", "",
                  "| 维度 | n_days | mean_IC | ICIR | NW_t | p | q | q<α |",
                  "|---|---|---|---|---|---|---|---|"]
        for f, r in sub.iterrows():
            lines.append(f"| {f} | {r['n_days']:.0f} | {r['mean_ic']:+.4f} | "
                         f"{r['icir']:+.2f} | {r['nw_t']:+.2f} | {r['nw_p']:.3g} | "
                         f"{r['q_value']:.3g} | {'✓' if r['reject'] else ''} |")
    lines += ["", "## 两套里语义最接近的维度：日内横截面相关（按天平均）", ""]
    if meta.get("parity"):
        lines += ["| 现役维 | Alpha158 维 | 参与天数 | 日均日内相关 | 方向 |",
                  "|---|---|---|---|---|"]
        for r in meta["parity"]:
            lines.append(f"| {r['left']} | {r['right']} | {r['n_days']} | "
                         f"{r['mean_corr']:+.3f} | {r['note']} |")
        lines += ["", "算法与 IC 一致：逐日在同一批票上算 Spearman，再对天平均——pooled "
                  "相关会被『哪只票、哪天』的量级水位主导，跟两维的语义没关系。", "",
                  "qlib 的 `ROC*` 写成 `Ref($close,N)/$close`（过去价 ÷ 现价），"
                  "与现役 `ret_5d`（现价 ÷ 过去价）方向相反：这一维的 IC 符号与"
                  "\"N 日涨幅\"的直觉相反。读下面的排行时必须先确认方向，否则会把"
                  "\"过去跌得多\"读成\"过去涨得多\"。"]
    red = meta.get("redundancy") or {}
    if red:
        lines += ["", "## 信息重叠：为什么「多少维显著」不等于「多少条独立信息」", "",
                  f"- Alpha158 内部：逐日 IC 序列两两 |corr| 均值 "
                  f"**{red['dup_within_alpha158']:.2f}**"
                  f"（{red['pairs_used']['alpha158']} 对）；FEAT26 内部 "
                  f"**{red['dup_within_feat26']:.2f}**"
                  f"（{red['pairs_used']['feat26']} 对）——1 表示这些维在同一天一起响",
                  f"- 跨集合：每维在另一集合里最近的 |corr|，中位 "
                  f"**{red['cross_median']:.2f}**，"
                  f"其中 {red['cross_ge_08']}/{red['cross_n']} 维 ≥ 0.8"
                  f"——这些维在另一套里已经有一个同日同行的替身",
                  f"- 没进这张表的维：{len(red.get('dims_without_ic', []))} 个"
                  f"（{red.get('dims_without_ic', [])}）整列给不出 IC，"
                  f"常数维与未覆盖维属「没验」，不算重叠也不算独立", ""]
    ded = meta.get("dedup") or {}
    if ded:
        primary = DEDUP_PRIMARY if DEDUP_PRIMARY in ded else sorted(ded)[0]
        lines += ["", "## 去重之后还剩多少独立信号", "",
                  "| 阈值 | Alpha158 独立维 | FEAT26 独立维 | 158 维净新增 |"
                  " 净新增 \\|IC\\| 中位 | 被挡 \\|IC\\| 中位 | 其中过 BH | 现役净新增 |",
                  "|---|---|---|---|---|---|---|---|"]
        for thr in sorted(ded):
            v = ded[thr]
            nn = v["net_new"]
            lines.append(f"| {thr:.2f} | {v['within']['Alpha158']['kept']} | "
                         f"{v['within']['FEAT26']['kept']} | {nn['kept']} | "
                         f"{_fmt_ic(nn['median_abs_ic'])} | "
                         f"{_fmt_ic(nn['blocked_median_abs_ic'])} | "
                         f"{nn['reject']} | {v['reverse']['kept']} |"
                         + (" ← 主口径" if thr == primary else ""))
        nn = ded[primary]["net_new"]
        lines += ["", f"净新增的维（阈值 {primary:.2f}，按 |IC| 序，最多列 12 条）：", ""]
        if nn["kept"]:
            lines += ["| 维度 | n_days | mean_IC | ICIR | NW_t | q | q<α |",
                      "|---|---|---|---|---|---|---|"]
            sub = summary.loc[[d for d in nn["kept_dims"] if d in summary.index]].copy()
            sub["abs_ic"] = sub["mean_ic"].abs()
            for f, r in sub.sort_values("abs_ic", ascending=False).head(12).iterrows():
                lines.append(f"| {f} | {r['n_days']:.0f} | {r['mean_ic']:+.4f} | "
                             f"{r['icir']:+.2f} | {r['nw_t']:+.2f} | {r['q_value']:.3g} | "
                             f"{'✓' if r['reject'] else ''} |")
        else:
            lines.append("（空——158 维里没有一条是现役 26 维之外的独立信号）")
        lines += ["", "读法：**「集合内独立维数」说的是那一套自己有多冗余，"
                  "「净新增」才是切特征集的量纲**——后者已经把现役已有的信号扣掉了。"
                  f"并集 = 现役独立 {ded[primary]['within']['FEAT26']['kept']} + "
                  f"净新增 {nn['kept']} = "
                  f"{ded[primary]['within']['FEAT26']['kept'] + nn['kept']} 条独立信号"
                  "（两套的维名不重叠，所以可以直接相加）。"
                  f"被 seed 挡掉的维 {ded[primary]['net_new']['dropped_by_seed']} 条、"
                  f"集内自重复 {ded[primary]['net_new']['dropped_within']} 条；"
                  "两维没有共同有效日而无法判定的对数记在 `uncomparable`，"
                  "它不该被算进「独立」。"]
    blocks = meta.get("horizons") or {}
    if blocks:
        lines += ["", "## 多 horizon：换前瞻窗口，结论会不会翻", "",
                  "| h | 有 IC 的天数 | Alpha158 \\|IC\\| 中位 | FEAT26 \\|IC\\| 中位 |"
                  " Alpha158 独立维 | FEAT26 独立维 | 净新增 | 其中过 BH |"
                  " 净新增 \\|IC\\| 中位 | 现役净新增 | 2×SE 门槛 | 被现役挡（点名） |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for h in sorted(blocks, key=lambda x: int(x)):
            b = blocks[h]
            blocked = b["net_new"].get("blocked_by_seed") or []
            names = "、".join(f"{d['dim']}←{d['by']}" for d in blocked[:4]) or "—"
            if len(blocked) > 4:
                names += f" 等 {len(blocked)} 条"
            lines.append(
                f"| {b['horizon']} | {b['days_with_ic']} | "
                f"{_fmt_ic(b['median_abs_ic']['Alpha158'])} | "
                f"{_fmt_ic(b['median_abs_ic']['FEAT26'])} | "
                f"{b['within']['Alpha158']} | {b['within']['FEAT26']} | "
                f"{b['net_new']['kept']} | {b['net_new']['reject']} | "
                f"{_fmt_ic(b['net_new']['median_abs_ic'])} | {b['reverse']['kept']} | "
                f"{_fmt_ic(2 * float(b['noise_scale']))} | {names} |")
        days = [int(b["days_with_ic"]) for b in blocks.values()]
        same_basis = len(set(days)) == 1
        note = ("**各块共用同一批天（" + f"{days[0]} 天）**——差异只来自标签窗口与"
                " HAC 滞后，可以直接并列比。"
                if same_basis else
                "**各块天数不同（" + f"{min(days)}–{max(days)} 天）**：h 越大尾段越容易"
                "缺标签，那是「窗口尽头」不是「信号消失」，比均值要连着覆盖差一起看。")
        lines += ["", "读法：每一块都是同一张特征表、只换标签列与 HAC 滞后"
                  f"（`maxlags=h`）——所以横向可比。{note}"
                  "最后一列是该块自己的 2×SE（门槛由本块 HAC 标准误算出，随 h 变）："
                  "两格的差落在门槛以内就读不出结论。"]
    yearly = meta.get("yearly") or {}
    if yearly.get("groups"):
        yrs = yearly["years"]
        lines += ["", "## 逐年衰减：|IC| 中位与同号率", "",
                  "| 组 | " + " | ".join(yrs) + " |",
                  "|---|" + "---|" * len(yrs)]
        for name, g in yearly["groups"].items():
            lines.append(f"| {name} 中位\\|IC\\| | " + " | ".join(
                _fmt_ic(g["median_abs_ic"].get(y, float("nan"))) if y in g["median_abs_ic"]
                else "—" for y in yrs) + " |")
            lines.append(f"| {name} 比值（÷该年 2×SE） | " + " | ".join(
                (f"{g['median_abs_ic'][y] / (2 * g['noise_scale'][y]):.2f}"
                 if y in g["median_abs_ic"] and y in g.get("noise_scale", {})
                 and np.isfinite(g["noise_scale"][y]) and g["noise_scale"][y] > 0 else "—")
                for y in yrs) + " |")
            lines.append(f"| {name} 同号率 | " + " | ".join(
                f"{g['sign_agreement'][y]:.2f}" if y in g["sign_agreement"] else "—"
                for y in yrs) + " |")
        lines += ["", "同号率 = 该年逐日 IC 均值的符号与**全样本**符号一致的维占比。"
                  "中位数掉下来但同号率仍接近 1，是「变弱」；同号率掉到 0.5 附近，是"
                  "「在换向」——后者不能靠加长窗口解决，也不该进候选集。",
                  "比值那行是**跨年比较的唯一尺子**：各年的天数不同（尾年往往更短），"
                  "门槛就不同——2026 段 171 天的门槛比 656 天全样本大一截。**比值 <1 表示"
                  "那一年该组的中位 |IC| 落在自己的噪声门槛以内**，只说明「按年切片判不出」，"
                  "不能读成「信号没了」。"]
    sens = meta.get("proxy_sensitivity") or {}
    if sens.get("rows"):
        lines += ["", "## 代理段敏感性：口径改了之后，受污染的维还剩多少", "",
                  f"断点 {meta.get('proxy_caliber_start', '—')}（KI-DATA-001：`amount` 改为 "
                  f"`close×volume` 的代理值）；主段 {sens['pre_days']} 天 vs 代理段 "
                  f"{sens['post_days']} 天，两段同判据、同 `maxlags={sens['maxlags']}`。", "",
                  "| 维度 | 主段 n_days | 主段 mean_IC | 主段 NW_t | 代理段 n_days |"
                  " 代理段 mean_IC | 代理段 NW_t | 位移 | 差的 2×SE | z |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        def _cell(v: float) -> str:
            # 判不出的格子统一印「—」：`f"{nan:+.2f}"` 印出来是 "+nan"，读者会当成一个数
            return "—" if not np.isfinite(v) else f"{v:+.2f}"

        for r in sens["rows"]:
            lines.append(
                f"| {r['dim']} | {r['pre_days']} | {_fmt_ic(r['pre_mean_ic'])} | "
                f"{_cell(r['pre_nw_t'])} | {r['post_days']} | "
                f"{_fmt_ic(r['post_mean_ic'])} | {_cell(r['post_nw_t'])} | "
                + ("—" if r["missing"] else f"{r['diff']:+.4f}") + " | "
                + ("—" if not np.isfinite(r["se_diff"]) else f"{2 * r['se_diff']:.4f}") + " | "
                + ("—" if r["missing"] else f"{r['z']:+.2f}") + " |")
        lines += ["", f"读法：**位移是 `代理段 − 主段`，判据用 z（|z|>2 才算看得见）**，"
                  f"两段的日不重叠，所以差的 SE 是两段 SE 的平方和开根。代理段只有 "
                  f"{sens['post_days']} 天，它的门槛天然比主段宽——位移落在「差的 2×SE」"
                  "以内时读作「判不出位移」，既不等于「口径没影响」，也不等于「IC 还在」"
                  "（代理段的样本本身也可能与主段的市场状态不同，这一层没隔开）。"
                  f"给不出 IC 的维印「—」并计入 `uncomparable`（本次 "
                  f"{sens['uncomparable']} 条）：那是「没验到」，不是「位移为 0」。"]
    noise = s["nw_se"].median()
    lines += ["", "读法：`n_days` 远小于天数的维度，其均值由少数几天给出，不能与满覆盖",
              f"的维度并列排序；本表 `mean_IC` 的 HAC 标准误中位是 {noise:.4f}，"
              f"两个维度的 `|IC|` 相差不到 {2 * noise:.4f}（2×SE）时不要读成结论"
              "——这个门槛由本表算出，换样本就换数。"]
    return lines


def write_report(summary: pd.DataFrame, a158: list[str], f26: list[str],
                 path: Path, meta: dict) -> None:
    atomic_write_text(path, "\n".join(report_lines(summary, a158, f26, meta)) + "\n")
    print(f"报告 → {path}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=PRE_PROXY_END,
                    help="默认停在成交额代理化之前（PROXY_CALIBER_START 前一天）")
    ap.add_argument("--include-proxy", action="store_true",
                    help="允许时窗越过成交额代理化日（敏感性对照用）")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--sample", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=20261104)
    ap.add_argument("--min-codes", type=int, default=300,
                    help="单日横截面最少标的数，不足则该天丢弃")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--limit", type=int, help="只跑前 N 只（冒烟）")
    ap.add_argument("--from-panel", help="复用已落盘的 panel_*.parquet，不重新装配特征")
    ap.add_argument("--extra-horizons", default="",
                    help="配合 --from-panel：逗号分隔的额外前瞻天数（如 1,10,20），"
                         "只加 label_hN 列、不重算特征；主 --horizon 自动包含")
    ap.add_argument("--against-daily", default="",
                    help="另一段的日频 IC parquet（通常是代理化之前的主段）；给出时"
                         "报告加「代理段敏感性」一节，位移按 KI-DATA-001 的受污染维 + "
                         "FEAT26 其余维（同产源、不读 amount）当对照")
    ap.add_argument("--out-dir", default=str(paths.DATA_ROOT / "alpha158_ic"))
    args = ap.parse_args(argv)

    if not args.include_proxy and args.end >= PROXY_DATE.isoformat():
        args.end = PRE_PROXY_END
        print(f"⚠️ --end 越过成交额代理化日且未给 --include-proxy，收到 {args.end}",
              file=sys.stderr)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
