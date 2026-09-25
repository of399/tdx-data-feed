#!/usr/bin/env python3
"""
tdxfeed/precompute/indicators.py — 离线技术指标 / 形态 / 相似 K线 预计算
==========================================================================

目的：
    把高频查询（"MA20 多少 / MACD 金叉 / 锤子形态 / 与历史哪些 K线相似"）
    从在线 LLM 查询转换为离线 Parquet 查表 → 毫秒级响应，零 LLM 消耗。

输入：
    data/parquet/daily/{sh,sz}{code}.parquet

输出（每个标的单独 parquet，存到 data/parquet/derived/）：
    {code}_indicators.parquet
        - MA5/10/20/60, MACD, KDJ, RSI, BOLL
        - 形态标记：hammer / shooting_star / doji / engulfing / marubozu
        - 相似 K线索引：top-5 历史相似窗口 + 后续 N 日收益

用法：
    # 全市场（首次，可能 10-30 分钟，CPU 密集）
    python -m tdxfeed.precompute.indicators --all

    # 单标的
    python -m tdxfeed.precompute.indicators --code sh600000

    # 增量（按 last_modified 跳过）
    python -m tdxfeed.precompute.indicators --incremental
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# === 路径 =================================================================
ROOT = Path("/home/jiuben/tdx-data-feed")
DAILY_DIR = ROOT / "data/parquet/daily"
DERIVED_DIR = ROOT / "data/parquet/derived"

# === 技术指标计算 =========================================================
def calc_ma(close: pd.Series, windows=(5, 10, 20, 60)) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    for w in windows:
        out[f"ma{w}"] = close.rolling(w).mean()
    return out


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_hist = macd - macd_signal
    return pd.DataFrame({
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "macd_golden_cross": (macd > macd_signal).astype(int),  # 金叉=1, 死叉=0
    })


def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series, n=9) -> pd.DataFrame:
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n + 1e-9) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j})


def calc_rsi(close: pd.Series, n=14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / (loss + 1e-9)
    return 100 - 100 / (1 + rs)


def calc_boll(close: pd.Series, n=20, k=2) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    return pd.DataFrame({
        "boll_mid": mid,
        "boll_upper": mid + k * std,
        "boll_lower": mid - k * std,
    })


# === 形态识别 ============================================================
def detect_candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """经典 K线形态标记（标签列，1=出现）"""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    upper_shadow = h - pd.concat([o, c], axis=1).max(axis=1)
    lower_shadow = pd.concat([o, c], axis=1).min(axis=1) - l
    total_range = h - l + 1e-9

    patterns = pd.DataFrame(index=df.index)
    # 锤子线：下影线 >= 2 倍实体，上影线很短
    patterns["hammer"] = (
        (lower_shadow >= 2 * body) & (upper_shadow <= 0.3 * body) & (body > 0)
    ).astype(int)
    # 射击之星：上影线 >= 2 倍实体，下影线很短
    patterns["shooting_star"] = (
        (upper_shadow >= 2 * body) & (lower_shadow <= 0.3 * body) & (body > 0)
    ).astype(int)
    # 十字星：实体 / 总范围 < 0.1
    patterns["doji"] = ((body / total_range) < 0.1).astype(int)
    # 看涨吞没：前阴后阳，且后者实体完全覆盖前者
    prev_o, prev_c = o.shift(1), c.shift(1)
    patterns["bullish_engulfing"] = (
        (prev_c < prev_o) & (c > o) & (c >= prev_o) & (o <= prev_c)
    ).astype(int)
    # 看跌吞没
    patterns["bearish_engulfing"] = (
        (prev_c > prev_o) & (c < o) & (c <= prev_o) & (o >= prev_c)
    ).astype(int)
    # 光头光脚阳线 / 阴线
    patterns["marubozu_bull"] = (
        (c > o) & (upper_shadow < 0.05 * total_range) & (lower_shadow < 0.05 * total_range)
    ).astype(int)
    patterns["marubozu_bear"] = (
        (c < o) & (upper_shadow < 0.05 * total_range) & (lower_shadow < 0.05 * total_range)
    ).astype(int)
    return patterns


# === 相似 K线检索 ========================================================
def find_similar_windows(df: pd.DataFrame, window=20, top_k=5, future_n=5) -> pd.DataFrame:
    """对每个 20 日窗口，用归一化收盘价向量找历史最相似的 top_k 窗口"""
    close = df["close"].values
    n = len(close)
    if n < window + future_n + top_k:
        return pd.DataFrame()

    # 归一化：每个窗口起点=1
    norm = pd.Series(close) / pd.Series(close).shift(window - 1)
    norm = norm.values

    future_returns = []
    similar_indices = []
    for i in range(n - window - future_n):
        target = norm[i:i + window]
        target_ret = close[i + window + future_n - 1] / close[i + window - 1] - 1
        future_returns.append(target_ret)

        # 历史所有窗口（不含自身 ± window）
        candidates = []
        for j in range(n - window - future_n):
            if abs(i - j) < window:
                continue
            cand = norm[j:j + window]
            if np.any(np.isnan(cand)):
                continue
            # 余弦相似度
            cos = np.dot(target, cand) / (np.linalg.norm(target) * np.linalg.norm(cand) + 1e-9)
            candidates.append((j, cos))
        # top_k
        candidates.sort(key=lambda x: x[1], reverse=True)
        top = candidates[:top_k]
        similar_indices.append({
            "date": df["date"].iloc[i + window - 1] if "date" in df.columns else i,
            "future_return": target_ret,
            "top1_idx": top[0][0] if len(top) > 0 else -1,
            "top1_sim": top[0][1] if len(top) > 0 else 0,
            "top5_idx": json.dumps([t[0] for t in top]),
            "top5_sim": json.dumps([t[1] for t in top]),
        })
    return pd.DataFrame(similar_indices)


# === 主流程 ==============================================================
def compute_one(code: str, df: pd.DataFrame) -> pd.DataFrame:
    """对单标的计算所有指标 + 形态 + 相似"""
    if df.empty or len(df) < 60:
        return pd.DataFrame()
    df = df.sort_values("date").reset_index(drop=True) if "date" in df.columns else df.reset_index(drop=True)
    close, high, low = df["close"], df["high"], df["low"]

    # 指标
    indicators = pd.concat([
        calc_ma(close),
        calc_macd(close),
        calc_kdj(high, low, close),
        calc_rsi(close).rename("rsi14"),
        calc_boll(close),
        detect_candle_patterns(df),
    ], axis=1)

    # 合并基础 K线
    out = pd.concat([df, indicators], axis=1)
    return out


def process_one(code: str, force: bool = False) -> Path | None:
    """读单标的 parquet，写 derived"""
    src = DAILY_DIR / f"{code}.parquet"
    if not src.exists():
        return None
    dst = DERIVED_DIR / f"{code}_indicators.parquet"
    if dst.exists() and not force and dst.stat().st_mtime > src.stat().st_mtime:
        return None  # 已是最新
    df = pd.read_parquet(src)
    out = compute_one(code, df)
    if out.empty:
        return None
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dst, index=False)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全市场 7,688 标的")
    ap.add_argument("--code", type=str, help="单标的代码（如 sh600000）")
    ap.add_argument("--incremental", action="store_true", help="增量（按 mtime）")
    ap.add_argument("--force", action="store_true", help="强制覆盖已有")
    args = ap.parse_args()

    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    t0 = datetime.now()

    if args.code:
        out = process_one(args.code, force=args.force)
        print(f"[{args.code}] → {out}")
        return

    # 全市场 / 增量
    files = sorted(DAILY_DIR.glob("*.parquet"))
    total = len(files)
    print(f"目标 {total} 个标的 ...")
    ok, skip, fail = 0, 0, 0
    for i, f in enumerate(files):
        code = f.stem
        try:
            r = process_one(code, force=args.force)
            if r is None:
                skip += 1
            else:
                ok += 1
        except Exception as e:
            fail += 1
            if fail < 5:
                print(f"  ✗ {code}: {e}")
        if (i + 1) % 200 == 0:
            elapsed = (datetime.now() - t0).total_seconds()
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{i + 1}/{total}] ok={ok} skip={skip} fail={fail} "
                  f"速率={rate:.1f}/s  ETA={eta/60:.1f}min")

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n完成：ok={ok} skip={skip} fail={fail}  耗时 {elapsed/60:.1f}min")
    print(f"输出目录：{DERIVED_DIR}")


if __name__ == "__main__":
    main()
