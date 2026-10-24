"""scripts/plan_e_sim_live.py — 回测模拟实盘口径

目的：在历史窗口内，每隔一段时间跑 predict → 取 Top N → 模拟 TP/SL/HOLD。
与 plan_e_backtest v2 (已知翻倍信号) 的指标对比，量化"回测高估"上界。

回测 vs 模拟实盘的差异：
  - 回测 (plan_e_backtest v2): 信号 = 历史已确认 peak_date - 5d（事后选点）
  - 模拟实盘 (本脚本): 信号 = 历史某日 D 的 predict 输出（事前预测）
  - 二者差距 = 估算法优化幅度（过拟合上界）

输出：
  data/plan_e/plan_e_sim_live_{ts}.csv    每笔交易明细
  data/plan_e/plan_e_sim_live_{ts}.md     聚合指标 + 与回测 v2 对比

用法：
    venv/bin/python scripts/plan_e_sim_live.py \\
        --samples quarterly --top 30 --threshold 0.7

轻量烟雾测试（限 50 只股 + 4 个日期）：
    venv/bin/python scripts/plan_e_sim_live.py --limit-codes 50 --start 2024-01-01 --end 2024-12-31
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# 仓库根 + 单一数据源导入桥
sys.path.insert(0, str(Path(__file__).parent))
from _common import compute_window_features, compute_window_features_extended

ROOT = Path(__file__).parent.parent
# 显式把项目根加入 sys.path，确保 `import tdxfeed`（如 freshness）在直接运行时可用。
sys.path.insert(0, str(ROOT))

MODEL_FILE = ROOT / "data" / "double_up" / "d_signal_5d_model.txt"
UNIVERSE_FILE = ROOT / "data" / "all_a_stocks.json"

# v9 兼容: predict.py FEATURE_COLS 是 18 维;v9 模型是 26 维(需在 predict 路径兼容)。
# sim_live 用相同的 DataFrame + LightGBM predict,模型自动按特征顺序匹配。
PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
OUTPUT_DIR = ROOT / "data" / "plan_e"

# TP/SL/HOLD 与 plan_e_backtest v2 / plan_e_journal 完全一致
TP_PCT = 0.30
SL_PCT = -0.10
HOLD_DAYS = 10
TRADE_FEE_BPS = 0.03
TRADE_SLIPPAGE_BPS = 0.10

# 18 维基础特征(v8 模型)
FEATURE_COLS_BASE_18 = [
    "limit", "prev_close", "ret_5d", "ret_3d", "max_5d_pct", "limit_up_5d",
    "limit_up_t1", "pct_t1", "avg_amount_5d", "vol_ratio_t1", "volatility_5d",
    "close_pos_5d", "max_drawdown_5d", "turnover_proxy", "is_st", "is_new",
    "is_bj", "is_gem",
]

# 8 维工程化新特征(v9 模型用)
FEATURE_COLS_NEW_8 = [
    "acceleration", "range_efficiency", "vol_norm_ret", "amount_surge_3d",
    "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low", "amt_per_price",
]

# 默认 18 维(脚本启动时根据 model 实际期望动态覆盖)
FEATURE_COLS = list(FEATURE_COLS_BASE_18)


def load_universe(limit: int | None = None) -> list[str]:
    """从 data/all_a_stocks.json 加载宇宙（默认全量）。JSON 实际为 {code: name} 字典。"""
    if not UNIVERSE_FILE.exists():
        return []
    with open(UNIVERSE_FILE) as f:
        data = json.load(f)
    # JSON 实际为 {code: name} 字典，统一转为 code 列表
    if isinstance(data, dict):
        codes = list(data.keys())
    else:
        codes = list(data)
    if limit and limit > 0:
        rng = np.random.default_rng(42)
        codes = list(rng.choice(codes, min(limit, len(codes)), replace=False))
    return codes


def find_parquet(code: str) -> Path | None:
    """按代码前缀统一候选。"""
    code_clean = code.lstrip("bj")
    for prefix in ("", "sh", "sz", "bj"):
        f = PARQUET_DIR / f"{prefix}{code_clean}.parquet"
        if f.exists():
            return f
    return None


def _load_one_for_window(code: str) -> pd.DataFrame:
    """读 parquet 并归一化 date。"""
    fp = find_parquet(code)
    if fp is None:
        return pd.DataFrame()
    df = pd.read_parquet(fp, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    if df["date"].dtype in ("int64", "int32", "int16"):
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    else:
        df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def features_at(code: str, cutoff: pd.Timestamp, use_extended: bool = False) -> dict | None:
    """用 cutoff 及之前的数据计算 18/26 维特征（不偷看未来）。

    use_extended=True 时调 compute_window_features_extended (26 维,供 v9 模型)。
    """
    df = _load_one_for_window(code)
    if df.empty:
        return None
    df = df[df["date"] <= cutoff].reset_index(drop=True)
    if len(df) < 30:
        return None
    if use_extended:
        return compute_window_features_extended(df, code, sig_idx=-1)
    return compute_window_features(df, code, sig_idx=-1)


def simulate_trade(df_full: pd.DataFrame, signal_date: pd.Timestamp, code: str) -> dict | None:
    """在 signal_date 后模拟：T+1 open 入场，10d 内 TP/SL/TIME 优先判定。"""
    future = df_full[df_full["date"] > signal_date].reset_index(drop=True)
    if len(future) < 2:
        return None
    entry_price = float(future.iloc[0]["open"])  # T+1 open
    if entry_price <= 0 or np.isnan(entry_price):
        return None

    exit_price = None
    exit_reason = None
    n = min(HOLD_DAYS + 1, len(future))
    tp_lvl = entry_price * (1 + TP_PCT)
    sl_lvl = entry_price * (1 + SL_PCT)
    for i in range(1, n):
        row = future.iloc[i]
        hit_sl = float(row["low"]) <= sl_lvl
        hit_tp = float(row["high"]) >= tp_lvl
        if not (hit_sl or hit_tp):
            continue
        # 与 plan_e_backtest 同一口径：日内高低点无法证明先后，双触发取亏损一侧；
        # 跳空穿越阈值按当日开盘成交，不要抹回阈值价
        open_i = float(row["open"]) if row["open"] > 0 else None
        if hit_sl:
            exit_price = min(sl_lvl, open_i) if open_i is not None else sl_lvl
            exit_reason = "SL"
        else:
            exit_price = max(tp_lvl, open_i) if open_i is not None else tp_lvl
            exit_reason = "TP"
        break
    if exit_price is None:
        if len(future) > HOLD_DAYS:
            exit_price = float(future.iloc[HOLD_DAYS]["close"])
            exit_reason = "TIME"
        else:
            exit_price = float(future.iloc[-1]["close"])
            exit_reason = "END"

    pnl_pct = exit_price / entry_price - 1
    # 含成本（与回测 v2 一致：佣金 0.03% + 滑点 0.10% = 0.13% 双边）
    pnl_pct -= (TRADE_FEE_BPS + TRADE_SLIPPAGE_BPS) * 2 / 100
    return {
        "code": code,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "entry_date": future.iloc[0]["date"].strftime("%Y-%m-%d"),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "reason": exit_reason,
    }


def sample_dates(start: str, end: str, freq: str) -> list[str]:
    """采样日期：quarterly / monthly / yearly。"""
    dates = pd.date_range(start, end, freq=freq)
    return [d.strftime("%Y-%m-%d") for d in dates]


def _preload(codes: list[str], max_workers: int) -> dict[str, pd.DataFrame]:
    """预加载全部代码的 parquet 到内存（每只股只读一次，后续 24 季度复用）。"""
    cache: dict[str, pd.DataFrame] = {}
    if max_workers > 1 and len(codes) > 50:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_load_one_for_window, code): code for code in codes}
            done = 0
            for fut in as_completed(futs):
                done += 1
                code = futs[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        cache[code] = df
                except Exception:
                    continue
                if done % 500 == 0:
                    print(f"    预加载进度: {done}/{len(codes)} 有效: {len(cache)}", flush=True)
    else:
        for i, code in enumerate(codes, 1):
            try:
                df = _load_one_for_window(code)
                if not df.empty:
                    cache[code] = df
            except Exception:
                continue
            if i % 500 == 0:
                print(f"    预加载进度: {i}/{len(codes)} 有效: {len(cache)}", flush=True)
    return cache


def _features_from_cache(code_dfs: dict[str, pd.DataFrame], cutoff: pd.Timestamp, use_extended: bool = False) -> list[dict]:
    """用缓存的 df + cutoff 日期计算所有 code 的 18/26 维特征。"""
    from predict import _enrich_industry_features, _enrich_market_features
    feats = []
    cutoff_str = pd.Timestamp(cutoff).strftime("%Y-%m-%d")
    for code, df_full in code_dfs.items():
        df = df_full[df_full["date"] <= cutoff].reset_index(drop=True)
        if len(df) < 30:
            continue
        if use_extended:
            f = compute_window_features_extended(df, code, sig_idx=-1)
        else:
            f = compute_window_features(df, code, sig_idx=-1)
        if f is not None:
            f["code"] = code
            # Task D 行业代理(代码前缀):5 板块 + 4 市场
            f = _enrich_market_features(f, cutoff_str)
            # Task D 行业(真实 akshare 申万):6 行业特征
            f = _enrich_industry_features(f, cutoff_str)
            feats.append(f)
    return feats


def run(
    sample_dates_list: list[str],
    top_n: int,
    threshold: float,
    code_dfs: dict[str, pd.DataFrame],
    model,
    feature_cols: list[str],
    use_extended: bool = False,
) -> pd.DataFrame:
    """主循环：每个采样日期 → 特征(从缓存) → 预测 → 模拟交易。"""
    all_trades: list[dict] = []
    n_dates = len(sample_dates_list)
    n_codes = len(code_dfs)
    for di, date_str in enumerate(sample_dates_list, 1):
        cutoff = pd.Timestamp(date_str)
        feats = _features_from_cache(code_dfs, cutoff, use_extended=use_extended)
        if not feats:
            print(f"  [{di}/{n_dates}] {date_str}: loaded={n_codes} valid=0 top=0 trades=0", flush=True)
            continue
        feat_df = pd.DataFrame(feats)
        # 仅保留模型期望的特征列(顺序与 model.feature_name() 一致)
        X = feat_df[feature_cols].fillna(0)
        probs = model.predict(X)
        feat_df["prob"] = probs

        top = feat_df[feat_df["prob"] >= threshold].sort_values("prob", ascending=False).head(top_n)
        if top.empty:
            top = feat_df.sort_values("prob", ascending=False).head(top_n)

        for _, row in top.iterrows():
            code = row["code"]
            trade = simulate_trade(code_dfs[code], cutoff, code)
            if trade:
                trade["prob"] = float(row["prob"])
                all_trades.append(trade)
        print(
            f"  [{di}/{n_dates}] {date_str}: loaded={n_codes} valid={len(feats)} "
            f"top={len(top)} trades={len(all_trades)}",
            flush=True,
        )

    return pd.DataFrame(all_trades)


def summarize(trades: pd.DataFrame) -> str:
    if trades.empty:
        return "❌ 无成交"
    n = len(trades)
    wins = (trades["pnl_pct"] > 0).sum()
    win_rate = wins / n * 100
    avg_pnl = trades["pnl_pct"].mean() * 100
    total = trades["pnl_pct"].sum() * 100
    by_year = trades.groupby(trades["signal_date"].str[:4]).agg(
        n=("pnl_pct", "size"),
        win=("pnl_pct", lambda x: (x > 0).mean()),
        avg=("pnl_pct", lambda x: x.mean()),
    )
    by_year_str = "\n".join(
        f"| {y} | {int(r['n'])} | {r['win']*100:.1f}% | {r['avg']*100:+.2f}% |"
        for y, r in by_year.iterrows()
    )
    return (
        f"| 指标 | 值 |\n|---|---|"
        f"\n| 总样本 | {n} |"
        f"\n| 胜率 | {win_rate:.1f}% |"
        f"\n| 平均收益 | {avg_pnl:+.2f}% |"
        f"\n| 累计收益 | {total:+.2f}% |"
        f"\n\n### 分年度\n\n| 年 | 笔数 | 胜率 | 平均收益 |\n|---|---:|---:|---:|\n{by_year_str}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default="quarterly", choices=["quarterly", "monthly", "yearly"])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--limit-codes", type=int, default=0, help="限 N 只股（烟雾测试用）")
    parser.add_argument("--start", default="2021-03-31")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--workers", type=int, default=8, help="预加载并行进程数")
    parser.add_argument("--no-freshness-check", action="store_true", help="跳过数据新鲜度校验")
    parser.add_argument("--model-file", type=str, default=str(MODEL_FILE), help=f"LightGBM 模型文件路径(默认 {MODEL_FILE.name})")
    args = parser.parse_args()

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    import lightgbm as lgb

    if not MODEL_FILE.exists():
        sys.exit(f"❌ 模型不存在: {MODEL_FILE}")
    model_file = Path(args.model_file)
    if not model_file.exists():
        sys.exit(f"❌ 模型不存在: {model_file}")
    model = lgb.Booster(model_file=str(model_file))

    # 从 model 读特征名: v8=18 维,v9=26 维(v9 含 8 维工程化)
    feature_cols = model.feature_name()
    use_extended = len(feature_cols) > 18
    print(f"  模型: {model_file.name} | 特征数: {len(feature_cols)} | 模式: {'extended(26维)' if use_extended else 'base(18维)'}")

    codes = load_universe(limit=args.limit_codes if args.limit_codes > 0 else None)
    if not codes:
        sys.exit("❌ 宇宙清单为空")

    freq_map = {"quarterly": "QE", "monthly": "ME", "yearly": "YE"}
    sample = sample_dates(args.start, args.end, freq_map[args.samples])
    print(f"  采样: {len(sample)} 个日期 ({sample[0]} ~ {sample[-1]})")

    # Phase 1: 预加载 parquet（每只股读一次，季度循环复用）
    print(f"  预加载 {len(codes)} 只股 parquet (workers={args.workers})...")
    code_dfs = _preload(codes, args.workers)
    print(f"  预加载完成: {len(code_dfs)}/{len(codes)} 只股有数据")

    trades = run(sample, args.top, args.threshold, code_dfs, model, feature_cols, use_extended=use_extended)
    if trades.empty:
        print("❌ 无成交")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_out = OUTPUT_DIR / f"plan_e_sim_live_{ts}.csv"
    md_out = OUTPUT_DIR / f"plan_e_sim_live_{ts}.md"
    trades.to_csv(csv_out, index=False)
    md = (
        f"# 方案 E 模拟实盘 (Plan E Sim-Live) — {ts}\n\n"
        f"## 核心指标\n\n"
        f"{summarize(trades)}\n\n"
        f"---\n\n"
        f"## 与回测 v2 对比（手算估算）\n\n"
        f"| 维度 | 回测 v2（已知翻倍） | 模拟实盘（事前预测） | 差距 |\n"
        f"|---|---|---|---|\n"
        f"| 信号来源 | 历史 peak_date - 5d | predict Top {args.top} ≥ {args.threshold} | — |\n"
        f"| 样本数 | 136 | {len(trades)} | — |\n"
        f"| 胜率 | 55.6% | {(trades['pnl_pct']>0).mean()*100:.1f}% | {55.6 - (trades['pnl_pct']>0).mean()*100:+.1f}% |\n"
        f"| 平均收益 | 12.22% | {trades['pnl_pct'].mean()*100:+.2f}% | {12.22 - trades['pnl_pct'].mean()*100:+.2f}% |\n\n"
        f"> 解读：差距越大说明回测高估越严重（即策略在事前预测中表现明显差于事后选点）。\n"
        f"> 若差距小（<5pp），策略相对稳健；若差距 >10pp，建议加大实盘风控或减小仓位。\n\n"
        f"---\n\n生成时间: {datetime.now().isoformat(timespec='seconds')}  生成者: scripts/plan_e_sim_live.py  模型: d_signal_5d_model.txt  数据: {sample[0]} ~ {sample[-1]}\n"
    )
    md_out.write_text(md, encoding="utf-8")
    print(f"\n  ✅ CSV: {csv_out}")
    print(f"  ✅ MD : {md_out}")


if __name__ == "__main__":
    main()
