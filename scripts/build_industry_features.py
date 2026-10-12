"""scripts/build_industry_features.py — 基于 akshare 申万行业的特征生成

目标:为每个训练样本添加 6 维行业代理特征(同行业当日聚合)
  - ind_ret_5d_avg      同行业当日 5d 平均收益
  - ind_amt_avg_5d      同行业当日 5d 平均成交额
  - ind_pct_t1_avg      同行业当日平均昨日方向
  - ind_volatility_avg  同行业当日平均波动率
  - ind_stock_count     同行业当日股票数
  - ret_minus_ind_5d    ret_5d - ind_ret_5d_avg (相对强弱)

数据源:
  - data/industry/sw_stock_industry.csv (akshare 申万行业归属)
  - data/double_up/d_signal_5d_features_v9.csv (训练数据)

输出:
  - data/industry/industry_features.csv (signal_date × industry_code 聚合)
  - data/double_up/d_signal_5d_features_v9_industry.csv (35+6=41 维训练数据)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
INDUSTRY_MAPPING = ROOT / "data" / "industry" / "sw_stock_industry.csv"
INDUSTRY_FEATS_OUT = ROOT / "data" / "industry" / "industry_features.csv"
TARGET_TRAIN = ROOT / "data" / "double_up" / "d_signal_5d_features_v9_industry.csv"

INDUSTRY_FEATURE_COLS = [
    "ind_ret_5d_avg", "ind_amt_avg_5d", "ind_pct_t1_avg",
    "ind_volatility_avg", "ind_stock_count", "ret_minus_ind_5d",
]


def clean_inf(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            n_inf = (~np.isfinite(df[c])).sum()
            if n_inf > 0:
                df[c] = df[c].replace([np.inf, -np.inf], np.nan).fillna(0)
    return df


def main():
    print("=== akshare 申万行业特征生成 ===\n")

    if not INDUSTRY_MAPPING.exists():
        sys.exit(f"❌ 缺失行业映射: {INDUSTRY_MAPPING}\n先跑 akshare_industry_loader.py")
    if not SOURCE.exists():
        sys.exit(f"❌ 缺失训练数据: {SOURCE}")

    df = pd.read_csv(SOURCE)
    mapping = pd.read_csv(INDUSTRY_MAPPING, usecols=["code", "industry_code"])
    print(f"训练数据: {len(df)} 样本 × {len(df.columns)-3} 特征")
    print(f"行业映射: {len(mapping)} 条 ({mapping['code'].nunique()} 只)")

    # 合并行业归属
    df = df.merge(mapping, on="code", how="left")
    n_mapped = df["industry_code"].notna().sum()
    print(f"\n合并后行业覆盖: {n_mapped}/{len(df)} = {n_mapped/len(df)*100:.1f}%")

    # 未匹配者用 industry_code = "UNMAPPED"
    df["industry_code"] = df["industry_code"].fillna("UNMAPPED")

    # 按 (signal_date, industry_code) 聚合
    print("\n[Phase 1] 计算行业聚合特征(按 signal_date × industry_code)")
    grouped = df.groupby(["signal_date", "industry_code"])
    industry_feats = pd.DataFrame({
        "ind_ret_5d_avg": grouped["ret_5d"].mean(),
        "ind_amt_avg_5d": grouped["avg_amount_5d"].mean(),
        "ind_pct_t1_avg": grouped["pct_t1"].mean(),
        "ind_volatility_avg": grouped["volatility_5d"].mean(),
        "ind_stock_count": grouped.size(),
    }).reset_index()
    print(f"  (signal_date, industry_code) 组数: {len(industry_feats)}")
    print(f"  唯一行业 in 训练: {industry_feats['industry_code'].nunique()}")
    print(f"  唯一日期 in 训练: {industry_feats['signal_date'].nunique()}")

    # 清理 inf
    industry_feats = clean_inf(industry_feats, ["ind_ret_5d_avg", "ind_amt_avg_5d", "ind_pct_t1_avg", "ind_volatility_avg"])

    # 保存行业特征表
    industry_feats.to_csv(INDUSTRY_FEATS_OUT, index=False)
    print(f"  行业特征: {INDUSTRY_FEATS_OUT}")

    # 合并回训练数据
    print("\n[Phase 2] 合并行业特征到训练数据")
    df = df.merge(industry_feats, on=["signal_date", "industry_code"], how="left")
    df["ret_minus_ind_5d"] = df["ret_5d"] - df["ind_ret_5d_avg"]

    # 清理 inf
    df = clean_inf(df, INDUSTRY_FEATURE_COLS)

    # 覆盖率检查
    for c in INDUSTRY_FEATURE_COLS:
        n_valid = df[c].notna().sum()
        print(f"  {c}: {n_valid}/{len(df)} = {n_valid/len(df)*100:.1f}%")

    # 保存训练数据
    df.to_csv(TARGET_TRAIN, index=False)
    print(f"\n  训练数据: {TARGET_TRAIN}")
    print(f"  新维度: {len(df.columns)-3} = 原 26 + 6 行业")

    # 训练数据样本(1 行)
    print("\n[样本预览]")
    sample = df[df["industry_code"] != "UNMAPPED"].head(1).iloc[0]
    print(f"  code: {sample['code']}")
    print(f"  industry_code: {sample['industry_code']}")
    print(f"  ret_5d: {sample['ret_5d']:.4f}")
    print(f"  ind_ret_5d_avg: {sample['ind_ret_5d_avg']:.4f}")
    print(f"  ret_minus_ind_5d: {sample['ret_minus_ind_5d']:.4f}")
    print(f"  ind_stock_count: {sample['ind_stock_count']}")

    print(f"\n=== 完成 ===")


if __name__ == "__main__":
    main()
