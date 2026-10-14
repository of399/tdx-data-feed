#!/usr/bin/env python3
"""构建 2000+ 均衡分类评测集：未来 120 交易日是否翻倍（累计涨幅 >= 100%）。

设计目标（对应「稳健验证」要求）:
  1. 样本量 >= 2000，正负类别均衡（各 ~50%）。
  2. 覆盖所有相关类别：按「年份(2020-2026)」+「板块(沪/深/科创/创业/北交)」分层抽样。
  3. 标签语义与模型任务一致：直接判定「未来120交易日是否翻倍」，而非用操作建议作代理。
  4. 可重复性：固定随机种子，落盘样本集(jsonl) + 元数据(meta.json)，评测脚本直接读集重跑。

正样本(label=1)来源：data/double_up/double_up_2020_2026_detail.csv（确已翻倍事件）。
  - 取事件 peak_date 前约 110 个交易日的行情窗口作为「截至日」，保证窗口后 120 交易日含翻倍点。
  - 过滤：peak 无法在 parquet 定位 / 历史不足 / 未来 120 日最大涨幅 <100% 的事件。
负样本(label=0)来源：全市场 data/parquet/daily/*.parquet 随机窗口。
  - 要求未来 120 交易日最大涨幅 ∈ [-0.6, 0.8)（明确未翻倍，去除极端暴跌噪声）。
  - 排除：窗口后交易日不足 120 的样本。

用法:
  venv/bin/python scripts/build_cls2000.py            # 生成默认 1100+1100=2200 样本
  venv/bin/python scripts/build_cls2000.py --n-pos 1200 --n-neg 1200
输出:
  data/cls2000_samples.jsonl  (每行一个样本)
  data/cls2000_meta.json      (构造参数 + 分布统计，支持可重复性审计)
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path("/home/jiuben/tdx-data-feed")
PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
DOUBLE_UP = ROOT / "data" / "double_up" / "double_up_2020_2026_detail.csv"
OUT_JSONL = ROOT / "data" / "cls2000_samples.jsonl"
OUT_META = ROOT / "data" / "cls2000_meta.json"

SEED = 20260929
HORIZON = 120
WINDOW = 20

# 负样本未来最大涨幅允许区间（明确未翻倍，去噪）
NEG_MIN_RET, NEG_MAX_RET = -0.6, 0.8


# 统一日期解析：单一数据源见 tdxfeed/date_utils.parse_date_series
from _common import parse_date_series as parse_datecol  # noqa: E402


def market_of(code: str) -> str:
    """板块归类：科创/创业/北交/沪/深。接受带或不带前缀的 6 位代码。"""
    c = re.sub(r"^(sh|sz|bj)", "", code)
    if c.startswith("920") or (c and c[0] in "84" and len(c) == 6):
        return "bj"          # 北交所
    if c.startswith("688"):
        return "kcb"         # 科创板
    if c.startswith("300"):
        return "cyb"         # 创业板
    if c.startswith("6"):
        return "sh"          # 沪主板
    if c.startswith("0") or c.startswith("3"):
        return "sz"          # 深主板
    return "other"


def get_parquet(code: str):
    # 支持裸 6 位代码或带 sh/sz/bj 前缀；data/parquet/daily 实际为裸名(+bj 前缀)
    bare = re.sub(r"^(sh|sz|bj)", "", code)
    for c in (bare, f"sh{bare}", f"sz{bare}", f"bj{bare}"):
        p = PARQUET_DIR / f"{c}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume"])
            df["date"] = parse_datecol(df["date"])
            return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return None


def window_rows(df: pd.DataFrame, end_idx: int):
    """返回 (last_date, input_json_rows)。"""
    start = max(0, end_idx - WINDOW + 1)
    tail = df.iloc[start : end_idx + 1].reset_index(drop=True)
    rows, prev = [], None
    for _, row in tail.iterrows():
        d = str(row["date"])[:10]
        close = float(row["close"])
        pct = 0.0 if (prev is None or prev == 0) else round((close - prev) / prev * 100, 2)
        rows.append({
            "日期": d, "开盘": round(float(row["open"]), 2), "收盘": round(close, 2),
            "最高": round(float(row["high"]), 2), "最低": round(float(row["low"]), 2),
            "成交量": round(float(row["volume"]) * 100, 0), "涨跌幅": pct,
        })
        prev = close
    return str(df.iloc[end_idx]["date"])[:10], rows


def future_max_ret(df: pd.DataFrame, end_idx: int) -> float:
    fut = df.iloc[end_idx + 1 : end_idx + 1 + HORIZON]["close"].astype(float)
    if len(fut) < HORIZON:
        return -1.0
    base = float(df.iloc[end_idx]["close"])
    return fut.max() / base - 1.0


# ---------- 正样本 ----------
def build_pos(double_up: pd.DataFrame, n_pos: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    du = double_up.copy()
    du["peak_date"] = pd.to_datetime(du["peak_date"])
    by_year = {int(y): g for y, g in du.groupby(du["peak_date"].dt.year)}
    years = sorted(by_year)
    per_year = max(1, n_pos // len(years))
    collected: list[dict] = []
    for y in years:
        g = by_year[y].sample(frac=1, random_state=seed + y).reset_index(drop=True)
        cnt = 0
        for _, ev in g.iterrows():
            code = str(ev["code"])
            df = get_parquet(code)
            if df is None or len(df) < WINDOW + 115:
                continue
            pdates = pd.to_datetime(df["date"])
            matches = pdates == ev["peak_date"]
            if not matches.any():
                continue
            peak_idx = int(matches.idxmax())
            if peak_idx < WINDOW + 10:
                continue
            last_idx = peak_idx - 110
            if last_idx < WINDOW - 1:
                continue
            last_date, rows = window_rows(df, last_idx)
            maxret = future_max_ret(df, last_idx)
            if maxret < 1.0:
                continue
            collected.append({
                "code": code, "market": market_of(code), "last_date": last_date,
                "year": y, "label": 1, "maxret_pct": round(maxret * 100, 1),
                "source": "double_up_event", "is_st": bool(ev.get("is_st", False)),
                "is_new": bool(ev.get("is_new", False)), "limit": bool(ev.get("limit", False)),
                "input": rows,
            })
            cnt += 1
            if cnt >= per_year:
                break
    # 若总量不足 n_pos（某年样本少），用已收集的全部（保证质量优先于数量）
    rng.shuffle(collected)
    return collected[:n_pos]


# ---------- 负样本 ----------
def build_neg_candidates(n_cand: int, seed: int) -> list[dict]:
    rng = random.Random(seed + 1)
    files = [p for p in PARQUET_DIR.glob("*.parquet") if re.match(r"^(sh|sz|bj)?\d{6}$", p.stem)]
    rng.shuffle(files)
    cands: list[dict] = []
    for p in files:
        code = re.sub(r"^(sh|sz|bj)", "", p.stem)
        if market_of(code) == "other" or code.startswith("399"):
            continue  # 排除 ETF/转债/指数等非 A 股个股噪声
        df = get_parquet(code)
        if df is None or len(df) < 30 + HORIZON:
            continue
        lo, hi = 30, len(df) - HORIZON - 1
        if lo >= hi:
            continue
        end_idx = rng.randint(lo, hi)
        last_date, rows = window_rows(df, end_idx)
        maxret = future_max_ret(df, end_idx)
        if not (NEG_MIN_RET <= maxret < NEG_MAX_RET):
            continue
        y = pd.to_datetime(df.iloc[end_idx]["date"]).year
        cands.append({
            "code": code, "market": market_of(code), "last_date": last_date,
            "year": int(y), "label": 0, "maxret_pct": round(maxret * 100, 1),
            "source": "market_random", "is_st": False, "is_new": False, "limit": False,
            "input": rows,
        })
        if len(cands) >= n_cand:
            break
    return cands


def stratified_pick(cands: list[dict], n: int, seed: int) -> list[dict]:
    """按 (year, market) 分层等比抽取，不足层用其他层补。"""
    rng = random.Random(seed + 2)
    by_key: dict[tuple, list[dict]] = {}
    for c in cands:
        by_key.setdefault((c["year"], c["market"]), []).append(c)
    picked: list[dict] = []
    # 先每层取 min(层大小, 配额); 配额按层大小加权
    total = len(cands)
    quota = {k: max(1, round(n * len(v) / total)) for k, v in by_key.items()}
    for k, v in by_key.items():
        rng.shuffle(v)
        picked.extend(v[: quota[k]])
    # 若超出则截断，不足则从未选补全
    if len(picked) > n:
        rng.shuffle(picked)
        picked = picked[:n]
    elif len(picked) < n:
        used = set(id(x) for x in picked)
        extra = [c for c in cands if id(c) not in used]
        rng.shuffle(extra)
        picked.extend(extra[: n - len(picked)])
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-pos", type=int, default=1100)
    ap.add_argument("--n-neg", type=int, default=1100)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    print(f"[build] 加载 double_up: {DOUBLE_UP}", flush=True)
    du = pd.read_csv(DOUBLE_UP)
    pos = build_pos(du, args.n_pos, args.seed)
    print(f"[build] 正样本收集: {len(pos)}", flush=True)

    neg_cand = build_neg_candidates(max(args.n_neg * 3, 1500), args.seed)
    print(f"[build] 负样本候选: {len(neg_cand)}", flush=True)
    neg = stratified_pick(neg_cand, args.n_neg, args.seed)
    print(f"[build] 负样本选定: {len(neg)}", flush=True)

    samples = pos + neg
    random.Random(args.seed).shuffle(samples)

    # 落盘（input 序列化为 JSON 字符串以兼容 jsonl 单行）
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps({**s, "input": json.dumps(s["input"], ensure_ascii=False)},
                              ensure_ascii=False) + "\n")

    # 分布统计
    n_total = len(samples)
    n_pos = sum(1 for s in samples if s["label"] == 1)
    by_year = Counter(s["year"] for s in samples)
    by_market = Counter(s["market"] for s in samples)
    by_year_label = Counter((s["year"], s["label"]) for s in samples)
    by_market_label = Counter((s["market"], s["label"]) for s in samples)

    meta = {
        "seed": args.seed,
        "horizon": HORIZON,
        "window": WINDOW,
        "neg_ret_range": [NEG_MIN_RET, NEG_MAX_RET],
        "n_total": n_total,
        "n_pos": n_pos,
        "n_neg": n_total - n_pos,
        "pos_source": "double_up_2020_2026_detail.csv (确已翻倍事件, peak前~110交易日窗口)",
        "neg_source": "data/parquet/daily/*.parquet 随机窗口 (未来120日最大涨幅[-0.6,0.8))",
        "dist_year": dict(sorted(by_year.items())),
        "dist_market": dict(by_market),
        "dist_year_label": {f"{y}/{'pos' if l else 'neg'}": c for (y, l), c in sorted(by_year_label.items())},
        "dist_market_label": {f"{m}/{'pos' if l else 'neg'}": c for (m, l), c in sorted(by_market_label.items())},
        "label_def": "未来120交易日累计最大涨幅 >= 100% 为 1(翻倍)，否则 0",
        "samples_file": str(OUT_JSONL),
    }
    OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 样本集: {OUT_JSONL} ===")
    print(f"  总量={n_total}  正={n_pos}({n_pos/n_total*100:.1f}%)  负={n_total-n_pos}({(n_total-n_pos)/n_total*100:.1f}%)")
    print(f"  年份分布: {dict(sorted(by_year.items()))}")
    print(f"  板块分布: {dict(by_market)}")
    print(f"  年份×标签: {meta['dist_year_label']}")
    print(f"  板块×标签: {meta['dist_market_label']}")
    print(f"  meta: {OUT_META}")


if __name__ == "__main__":
    main()
