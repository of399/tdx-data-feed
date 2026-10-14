"""scripts/build_market_features.py — 市场代理特征生成(Task D)

目标:突破 18 维技术面瓶颈,引入 4 维市场代理特征:
  1. mkt_ret_5d_avg: 当日全市场平均 5d 收益(大盘方向)
  2. mkt_amt_avg_5d: 当日全市场平均 5d 成交额(资金活跃度)
  3. mkt_pct_t1_avg: 当日全市场平均 pct_t1(昨日方向)
  4. stock_count_active: 当日有效股票数(记忆活跃度)

实现:
  - 读训练数据 CSV(d_signal_5d_features_v9.csv)
  - 按 signal_date 分组,计算市场平均
  - 保存 market_features.csv + 合并入训练数据
  - 输出新训练数据 d_signal_5d_features_v9_market.csv

行业分类后续:本轮用市场代理(大盘),后续轮接入真实行业分类
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
TARGET_TRAIN = ROOT / "data" / "double_up" / "d_signal_5d_features_v9_market.csv"
MARKET_FEATS = ROOT / "data" / "double_up" / "market_features.csv"

MARKET_FEATURE_COLS = ["mkt_ret_5d_avg", "mkt_amt_avg_5d", "mkt_pct_t1_avg", "stock_count_active"]
BOARD_FEATURE_COLS = ["board_sh_main", "board_sh_kechuang", "board_sz_main", "board_sz_chinext", "board_bj"]


def assign_board(code: str) -> str:
    """根据股票代码分配板块(5 大类)。"""
    if not isinstance(code, str):
        return "other"
    if code.startswith("bj"):
        return "bj"
    if code.startswith("sz300") or code.startswith("sz301"):
        return "sz_chinext"
    if code.startswith("sz000") or code.startswith("sz001") or code.startswith("sz002"):
        return "sz_main"
    if code.startswith("sh688") or code.startswith("sh689"):
        return "sh_kechuang"
    if code.startswith("sh600") or code.startswith("sh601") or code.startswith("sh603") or code.startswith("sh605"):
        return "sh_main"
    return "other"


def compute_market_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 signal_date 计算市场平均特征。"""
    grouped = df.groupby("signal_date")
    market = pd.DataFrame({
        "signal_date": grouped.size().index,
        "mkt_ret_5d_avg": grouped["ret_5d"].mean().values,
        "mkt_amt_avg_5d": grouped["avg_amount_5d"].mean().values,
        "mkt_pct_t1_avg": grouped["pct_t1"].mean().values,
        "stock_count_active": grouped.size().values,
    })
    return market


def one_hot_board(df: pd.DataFrame) -> pd.DataFrame:
    """加板块 one-hot 编码(5 大类)。"""
    boards = df["code"].apply(assign_board)
    board_set = ["sh_main", "sh_kechuang", "sz_main", "sz_chinext", "bj"]
    for b in board_set:
        col_name = f"board_{b}"
        df[col_name] = (boards == b).astype(int)
    return df


def main():
    print("=== 市场代理特征生成(Task D · 行业代理) ===\n")
    if not SOURCE.exists():
        sys.exit(f"❌ 训练数据不存在: {SOURCE}")

    df = pd.read_csv(SOURCE)
    print(f"原始训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")
    print(f"股票分布: {df['code'].nunique()} 只")

    # 板块分布
    print("\n[Phase 1] 板块分类(code 前缀)")
    boards = df["code"].apply(assign_board)
    print(boards.value_counts())

    # 市场特征
    print("\n[Phase 2] 计算 4 维市场代理特征(按 signal_date)")
    market = compute_market_features(df)
    print(f"  日期数: {len(market)}")
    print("  特征: mkt_ret_5d_avg / mkt_amt_avg_5d / mkt_pct_t1_avg / stock_count_active")
    print(f"  mkt_ret_5d_avg  范围: [{market['mkt_ret_5d_avg'].min():.4f}, {market['mkt_ret_5d_avg'].max():.4f}]")
    print(f"  mkt_amt_avg_avg  范围: [{market['mkt_amt_avg_5d'].min():.2f}, {market['mkt_amt_avg_5d'].max():.2f}]")

    # 板块 one-hot
    print("\n[Phase 3] 板块 one-hot(5 大类)")
    df = one_hot_board(df)
    new_cols = BOARD_FEATURE_COLS
    print(f"  添加: {new_cols}")
    print("  板块分布(one-hot sum 检查):")
    for c in new_cols:
        print(f"    {c}: {df[c].sum()} 笔 ({df[c].mean()*100:.1f}%)")

    # 合并市场特征
    df = df.merge(market, on="signal_date", how="left")

    # 保存
    print("\n[Phase 4] 保存")
    market.to_csv(MARKET_FEATS, index=False)
    print(f"  市场特征: {MARKET_FEATS}")

    df.to_csv(TARGET_TRAIN, index=False)
    print(f"  训练数据: {TARGET_TRAIN}")
    print(f"  新维度: {len(df.columns)-3} (原 26 + 市场 4 + 板块 5 = 35 维,实际含 code/signal_date/is_positive)")

    print("\n=== 完成 ===")
    print("下次训练:python scripts/train_pnl_proxy.py \\")
    print("  --objective huber --objective-alpha 0.5 \\")
    print("  --model-out data/double_up/d_signal_5d_model_vpnl_huber_market.txt")
    print("且修改 FEATURE_VPNL 为 28 维(20 旧 + 4 市场 + 5 板块)")


if __name__ == "__main__":
    main()
