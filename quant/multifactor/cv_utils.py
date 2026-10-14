"""过拟合防御工具：purged + embargo 时序交叉验证、IC 分年衰减。

说明：
- purged_embargo_splits：将有序时间序列切分为训练/测试折，并在测试集两侧
  留出 embargo 间隙，避免重叠样本泄漏（Lopez de Prado 范式）。
- ic_by_year：按年聚合 IC，用于衰减检验（逐年 IC 转负/衰减>50% 报警）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CVConfig:
    n_splits: int = 5
    embargo: int = 5          # 折间 embargo 交易日数


def purged_embargo_splits(n: int, cfg: CVConfig | None = None):
    """返回 list[(train_idx, test_idx)]，基于顺序切分 + embargo 剔除。"""
    cfg = cfg or CVConfig()
    if n < cfg.n_splits * 2:
        return []
    fold = n // cfg.n_splits
    splits = []
    for i in range(cfg.n_splits):
        # 测试窗（顺序）
        te_start = i * fold
        te_end = te_start + fold if i < cfg.n_splits - 1 else n
        tr_mask = np.ones(n, dtype=bool)
        # 测试窗
        tr_mask[te_start:te_end] = False
        # embargo：测试窗前后各 embargo 个样本从训练剔除
        lo = max(0, te_start - cfg.embargo)
        hi = min(n, te_end + cfg.embargo)
        tr_mask[lo:hi] = False
        splits.append((np.where(tr_mask)[0], np.arange(te_start, te_end)))
    return splits


def ic_by_year(ic_records: list[dict]) -> pd.DataFrame:
    """ic_records: list of {'date':Timestamp, 'ic':float, 'factor':str}。
    返回按年 × 因子的 IC 均值表。"""
    df = pd.DataFrame(ic_records)
    if df.empty:
        return pd.DataFrame()
    df["year"] = pd.to_datetime(df["date"]).dt.year
    out = df.pivot_table(index="year", columns="factor", values="ic", aggfunc="mean")
    return out


def icir(ic_series: pd.Series) -> float:
    """IC 信息比率 = mean/std，样本不足返回 0。"""
    s = ic_series.dropna()
    if len(s) < 5 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std())
