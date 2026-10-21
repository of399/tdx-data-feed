"""板块特征查表（阶段 3：把 Sec 送进模型特征管线）。

**时点红线（最重要）**：板块强度是**信号日收盘后**才知道的值。
- 训练：信号日 T，特征只能用 **T-1** 的板块强度（决策时点可得，合法）
- 推理：predict.py 在 T-1 收盘后跑、预测 T+1 → 同样用 **T-1** 的值
→ 两侧同源，故统一用 ``lag=1``；用 lag=0 会造成推理侧无法取数（未来信息泄漏）。

数据源：``data/factors/sector_features.{parquet,csv}``（由
``scripts/daban_sector_features.py`` 从日线重建，503,712 行 × 1,644 日）。

输出特征（建议 3 列）：
    sec_lag1            昨日板块强度分（0~100）
    sector_pct_lag1     昨日板块中位涨幅（%）
    sector_limitup_lag1 昨日板块涨停家数
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

__all__ = ["EXTRA_FEATURE_COLS", "SectorFeatureLookup", "build_lookup"]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SECTOR_PARQUET = DATA_DIR / "factors" / "sector_features.parquet"
SECTOR_CSV = DATA_DIR / "factors" / "sector_features.csv"

EXTRA_FEATURE_COLS = ("sec_lag1", "sector_pct_lag1", "sector_limitup_lag1")


class SectorFeatureLookup:
    """按 (industry_code, date) 查板块特征，支持滞后 N 日。"""

    def __init__(self, df: pd.DataFrame) -> None:
        d = df.copy()
        d["date"] = d["date"].astype(str)
        d["industry_code"] = d["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
        keep = [c for c in ("industry_code", "date", "sec", "sector_pct", "limit_up_n", "member_n") if c in d.columns]
        d = d[keep]
        self._table = d.set_index(["industry_code", "date"])
        self._dates = sorted(d["date"].unique())
        self._date_idx = {dt: i for i, dt in enumerate(self._dates)}

    def prev_date(self, date: str) -> str | None:
        """给定日期的前一个交易日。"""
        i = self._date_idx.get(str(date))
        if i is None or i == 0:
            return None
        return self._dates[i - 1]

    def get(self, industry_code: str, date: str, lag: int = 1) -> dict | None:
        """取 (industry_code, date) 滞后 lag 日的板块特征。"""
        if not industry_code:
            return None
        ind = str(industry_code)
        if lag > 0:
            dt = self.prev_date(date)
            if dt is None:
                return None
        else:
            dt = str(date)
        try:
            row = self._table.loc[(ind, dt)]
        except KeyError:
            return None
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return {
            "sec_lag1": float(row.get("sec", 0.0)),
            "sector_pct_lag1": float(row.get("sector_pct", 0.0)),
            "sector_limitup_lag1": float(row.get("limit_up_n", 0.0)),
            "sector_member_n_lag1": float(row.get("member_n", 0.0)),
        }


def build_lookup(path: Path | str | None = None) -> SectorFeatureLookup | None:
    """加载板块特征查表；文件不存在返回 None（调用方需优雅降级）。"""
    p = Path(path) if path else (SECTOR_PARQUET if SECTOR_PARQUET.exists() else SECTOR_CSV)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return SectorFeatureLookup(df)
