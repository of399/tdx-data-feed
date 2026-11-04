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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

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
    px["vwap"] = raw["amount"].astype(float) / raw["volume"].replace(0, np.nan)
    a158 = evaluate_frame(px, payload=payload)

    close_h = hfq["close"].astype(float)
    fwd = close_h.shift(-horizon) / close_h - 1.0

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
    rows = summary.loc[[d for d in blocked if d in summary.index]]
    ab = rows["mean_ic"].abs().to_numpy(dtype=float)
    out.update(dropped_by_seed=len(blocked),
               dropped_within=len(res["dropped"]) - len(blocked),
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
        return aggregate_and_write(panel, args, payload,
                                   extra={"reused_panel": str(args.from_panel)},
                                   write_panel=False)

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
                        write_panel: bool = True) -> int:
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
        f"- 耗时 {meta.get('elapsed_sec', 0.0):.0f}s"
        + (f"（复用面板 {meta['reused_panel']}）" if meta.get("reused_panel") else ""),
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
    ap.add_argument("--out-dir", default=str(paths.DATA_ROOT / "alpha158_ic"))
    args = ap.parse_args(argv)

    if not args.include_proxy and args.end >= PROXY_DATE.isoformat():
        args.end = PRE_PROXY_END
        print(f"⚠️ --end 越过成交额代理化日且未给 --include-proxy，收到 {args.end}",
              file=sys.stderr)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
