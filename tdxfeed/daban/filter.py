"""统一的板块过滤口径（阶段 0 落地的单一事实来源）。

**背景**：`scripts/daban_sec_ablation.py` 实测（90,778 笔打板交易，命中 98.6%）：

    过滤条件                              样本    保留%   胜率%   均收益%  盈亏比
    无                                89474   100%   62.5   1.622   1.95
    sec≥60                             27631    31%   64.5   1.810   2.12
    sec≥60 & 板块共振≥2 家              17375    19%   65.8   1.994   2.20
    sec≥50 & 板块共振≥3 家              12712    14%   67.7   2.222   2.23

分年 6/7 正向（2024 微负 -0.06），各年回撤均改善 → 稳健，零重训成本。

**本模块提供**：
- ``SectorFilter``：过滤配置 + 落盘/读取 + 对交易明细应用过滤
- ``config``：``data/daban/filter_config.json``（回测/评估/predict 共用）
- 板块特征来源：``data/factors/sector_features.{csv,parquet}``（当日口径）

**时点红线**：板块强度/涨停家数是**信号日收盘后**才知道的值。
- 盘中打板（09:30-10:00）：实时可得 → 可直接用当日值做盘中二次确认
- **盘前候选池（T-1 收盘后出候选）：不可用当日值** → 只能用滞后一版（阶段 3 的 ``sec_lag1``）
  本模块的 ``apply`` 面向**回测与评估**（用信号日当日值，与实盘盘中决策一致）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

__all__ = ["FILTER_CONFIG_PATH", "SectorFilter", "load_sector_features"]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SECTOR_CSV = DATA_DIR / "factors" / "sector_features.csv"
SECTOR_PARQUET = DATA_DIR / "factors" / "sector_features.parquet"
FILTER_CONFIG_PATH = DATA_DIR / "daban" / "filter_config.json"


@dataclass(slots=True)
class SectorFilter:
    """板块过滤配置。

    min_sec:      板块 Sec 下限（0 = 不过滤）
    min_limitup:  当日同板块涨停家数下限（0 = 不过滤）
    """

    min_sec: float = 0.0
    min_limitup: int = 0
    note: str = ""

    @property
    def enabled(self) -> bool:
        return self.min_sec > 0 or self.min_limitup > 0

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path | str = FILTER_CONFIG_PATH) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return p

    @classmethod
    def load(cls, path: Path | str = FILTER_CONFIG_PATH) -> SectorFilter:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            min_sec=float(d.get("min_sec", 0.0)),
            min_limitup=int(d.get("min_limitup", 0)),
            note=str(d.get("note", "")),
        )

    def apply(self, trades: pd.DataFrame, sector: pd.DataFrame) -> pd.DataFrame:
        """对交易明细应用过滤（按 signal_date + code 关联当日板块特征）。

        trades 需含：code（6 位字符串）、signal_date（YYYYMMDD 字符串）
        sector 需含：date、industry_code、sec、limit_up_n
        """
        if not self.enabled:
            return trades
        if trades.empty or sector.empty:
            return trades
        t = trades.copy()
        # code 归一：去 sh/sz/bj 前缀 + 6 位补零（回测是 6 位，评估可能是带前缀的文件名）
        t["code"] = (
            t["code"]
            .astype(str)
            .str.lower()
            .str.replace(r"^(sh|sz|bj)", "", regex=True)
            .str.zfill(6)
        )
        # 日期列自适应：回测用 signal_date，评估用 date → 统一重命名为 signal_date
        date_col = "signal_date" if "signal_date" in t.columns else "date"
        t = t.rename(columns={date_col: "signal_date"})
        t["signal_date"] = t["signal_date"].astype(str)
        if "industry_code" not in t.columns:
            from tdxfeed.daban.sector import load_industry_map

            t["industry_code"] = t["code"].map(load_industry_map())
        s = sector.copy()
        s["date"] = s["date"].astype(str)
        s["industry_code"] = s["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
        merged = t.merge(
            s[["date", "industry_code", "sec", "limit_up_n"]],
            left_on=["signal_date", "industry_code"],
            right_on=["date", "industry_code"],
            how="left",
        )
        keep = merged["sec"].notna()  # 无板块特征的样本（对齐失败）一律剔除
        if not keep.any():
            raise ValueError(
                "过滤后 0 条：日期或行业码未对齐。"
                f"trades 日期样例={t['signal_date'].head(2).tolist()}，"
                f"sector 日期样例={s['date'].head(2).tolist()}，"
                f"trades 行业码样例={t['industry_code'].head(2).tolist()}，"
                f"sector 行业码样例={s['industry_code'].head(2).tolist()}"
            )
        if self.min_sec > 0:
            keep &= merged["sec"] >= self.min_sec
        if self.min_limitup > 0:
            keep &= merged["limit_up_n"] >= self.min_limitup
        return merged[keep].drop(columns=["date"], errors="ignore").reset_index(drop=True)


def load_sector_features() -> pd.DataFrame:
    """载入板块特征（优先 parquet，回退 csv）。"""
    if SECTOR_PARQUET.exists():
        return pd.read_parquet(SECTOR_PARQUET)
    if SECTOR_CSV.exists():
        return pd.read_csv(SECTOR_CSV)
    return pd.DataFrame()
