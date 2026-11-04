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
from tdxfeed.alpha158.ic import IcConfig, daily_ic, summarize_ic  # noqa: E402
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
    feature_cols = a158_names + list(FEAT26_ORDER)
    print(f"面板 {panel.shape}（{panel['date'].nunique()} 天 × "
          f"{panel['code'].nunique()} 只）· 跳过 {dict(skipped)}", flush=True)

    cfg = IcConfig(label_col="label", min_codes=args.min_codes,
                   maxlags=args.horizon, alpha=args.alpha)
    daily = daily_ic(panel, feature_cols, cfg)
    summary = summarize_ic(daily, cfg)

    out_dir = Path(args.out_dir)
    tag = f"{args.start}_{args.end}_h{args.horizon}_n{args.sample}_s{args.seed}"
    meta = {
        "tag": tag, "start": args.start, "end": args.end, "horizon": args.horizon,
        "sample_requested": args.sample, "codes_built": int(panel["code"].nunique()),
        "days": int(panel["date"].nunique()), "rows": len(panel),
        "codes_per_day_median": float(panel.groupby("date")["code"].count().median()),
        "seed": args.seed, "min_codes": args.min_codes, "alpha": args.alpha,
        "skipped": dict(skipped), "error_count": dict(_ERROR_COUNT),
        "include_proxy": args.include_proxy,
        "proxy_caliber_start": PROXY_DATE.isoformat(),
        "feat26_amount_dims": FEAT26_AMOUNT_DIMS,
        "alpha158_vwap_dims": vwap_dims_from_artifact(payload),
        "elapsed_sec": round(time.perf_counter() - t0, 1),
    }
    atomic_write_parquet(panel.astype({c: "float32" for c in [*feature_cols, "label"]}),
                         out_dir / f"panel_{tag}.parquet")
    atomic_write_parquet(daily.reset_index(names="date"), out_dir / f"daily_ic_{tag}.parquet")
    atomic_write_text(out_dir / f"run_meta_{tag}.json",
                      json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
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
        f"（请求 {meta['sample_requested']} 只，跳过 {meta['skipped']}）"
        f"，每天中位 {meta['codes_per_day_median']:.0f} 只",
        f"- 标签 t→t+{meta['horizon']} 的 **hfq** 收盘收益；`min_codes={meta['min_codes']}`，"
        f"BH α={meta['alpha']}，HAC `maxlags={meta['horizon']}`（重叠标签的自相关长度）",
        f"- 耗时 {meta['elapsed_sec']:.0f}s",
        "",
        f"成交额代理段自 {meta['proxy_caliber_start']} 起（KI-DATA-001）：本次"
        f"{'**在样本内**' if meta['include_proxy'] else '**已排除'}。受污染维："
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
    lines += ["", "读法：`n_days` 远小于天数的维度，其均值由少数几天给出，不能与满覆盖",
              "的维度并列排序；`|IC|` 差小于 0.002（本样本的均值噪声尺度）时不要读成结论。"]
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
    ap.add_argument("--out-dir", default=str(paths.DATA_ROOT / "alpha158_ic"))
    args = ap.parse_args(argv)

    if not args.include_proxy and args.end >= PROXY_DATE.isoformat():
        args.end = PRE_PROXY_END
        print(f"⚠️ --end 越过成交额代理化日且未给 --include-proxy，收到 {args.end}",
              file=sys.stderr)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
