#!/usr/bin/env python3
"""导出剔除次新后的干净子集（任务③）。

从 double_30d_pre90_features.csv 中剔除"次新上市"与"数据缺口"事件，
输出干净子集 CSV（保留全部 per-event 特征），供下游聚类/回测使用。

用法：
  venv/bin/python scripts/export_clean_subset.py
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
FEATURES = ROOT / 'data' / 'double_up' / 'double_30d_pre90_features.csv'
OUT = ROOT / 'data' / 'double_up'


def main() -> None:
    feat = pd.read_csv(FEATURES)
    total = len(feat)
    n_new = int(feat['new_listing'].fillna(False).sum())
    n_gap = int(feat['data_gap'].fillna(False).sum())
    n_new_and_gap = int((feat['new_listing'].fillna(False) & feat['data_gap'].fillna(False)).sum())

    # 干净 = 非次新 且 非数据缺口
    clean = feat[~feat['new_listing'].fillna(False) & ~feat['data_gap'].fillna(False)].copy()

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fp = OUT / f'double_30d_clean_subset_{ts}.csv'
    clean.to_csv(fp, index=False, encoding='utf-8-sig')

    # 同时保留一个"仅剔除次新"的版本（不含数据缺口剔除），便于对照
    clean_newonly = feat[~feat['new_listing'].fillna(False)].copy()
    fp2 = OUT / f'double_30d_no_newlisting_{ts}.csv'
    clean_newonly.to_csv(fp2, index=False, encoding='utf-8-sig')

    print(f"总事件: {total}")
    print(f"  次新(new_listing): {n_new}")
    print(f"  数据缺口(data_gap): {n_gap}（与次新重叠 {n_new_and_gap}）")
    print(f"  干净子集(剔除次新+缺口): {len(clean)}  → {fp.name}")
    print(f"  仅剔除次新: {len(clean_newonly)}  → {fp2.name}")

    # 按 pre_pattern 分布的简短报告
    pat = clean['pre_pattern'].value_counts()
    print("\n干净子集形态分布:")
    for k, v in pat.items():
        print(f"  {k}: {v} ({v/len(clean)*100:.1f}%)")


if __name__ == '__main__':
    main()
