"""估值 / 基本面数据层：real 与 proxy 双模，auto 热切换。

- real：消费 data/valuation/pe_pb_daily.parquet 与 fundamentals.parquet（由
  scripts/download_valuation.py 经 akshare 下载；百度估值 + 东财财务）。
- proxy：纯量价代理（对数流动性深度 / 价格加速度），当天即可跑通全流程。
- auto（默认）：real 文件存在且体量达标 → 用 real，否则降级 proxy，并在
  coverage 报告中披露。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
VAL_DIR = ROOT / "data" / "valuation"
PE_PB_FP = VAL_DIR / "pe_pb_daily.parquet"
FUND_FP = VAL_DIR / "fundamentals.parquet"


@dataclass(frozen=True)
class ValuationConfig:
    mode: str = "auto"          # proxy | real | auto
    min_real_codes: int = 3000  # auto 判定 real 可用的最小覆盖股票数（避免半量下载误判）


class ValuationProvider:
    """按 (code, date) 提供估值/基本面截面值，内置 proxy 兜底。"""

    def __init__(self, cfg: ValuationConfig | None = None):
        self.cfg = cfg or ValuationConfig()
        self._real_pepb: pd.DataFrame | None = None
        self._real_fund: pd.DataFrame | None = None
        self.mode = self._resolve_mode()
        self.real_coverage_start: str | None = None
        if self.mode != "proxy":
            self._load_real()

    # ---- 模式解析 ----------------------------------------------------------
    def _resolve_mode(self) -> str:
        if self.cfg.mode == "proxy":
            return "proxy"
        if self.cfg.mode == "real":
            return "real" if PE_PB_FP.exists() and FUND_FP.exists() else "proxy"
        # auto：以覆盖股票数判定，避免半量下载误判可用
        if PE_PB_FP.exists():
            try:
                n = pd.read_parquet(PE_PB_FP, columns=["code"])["code"].nunique()
                if n >= self.cfg.min_real_codes:
                    return "real"
            except Exception:
                pass
        return "proxy"

    def _load_real(self) -> None:
        try:
            self._real_pepb = pd.read_parquet(PE_PB_FP)
            self._real_pepb["date"] = pd.to_datetime(self._real_pepb["date"])
            self._real_fund = pd.read_parquet(FUND_FP)
            self._real_fund["date"] = pd.to_datetime(self._real_fund["date"])
            self.real_coverage_start = str(self._real_pepb["date"].min().date())
        except Exception:
            self.mode = "proxy"
            self._real_pepb = None
            self._real_fund = None

    # ---- 对外查询 ----------------------------------------------------------
    def get_panel(self) -> pd.DataFrame | None:
        """返回 real 估值+财务合并的 long panel（code,date,pe_ttm,pb_mrq,roe_yoy）。"""
        if self.mode == "proxy" or self._real_pepb is None:
            return None
        pepb = self._real_pepb.copy()
        fund = self._real_fund.copy() if self._real_fund is not None else None
        df = pepb.merge(fund, on=["code", "date"], how="left") if fund is not None else pepb
        return df

    def proxy_pe(self, amount_20d_avg: float, close: float) -> float:
        """估值代理：以 20 日成交额对数的负值作为「便宜度」代理（越小越便宜）。
        仅作 real 数据到位前的占位，报告中明确披露。"""
        if not np.isfinite(amount_20d_avg) or amount_20d_avg <= 0:
            return np.nan
        return -np.log(amount_20d_avg)

    def proxy_pb(self, close: float, amount_20d_avg: float) -> float:
        """基本面/估值代理：价格加速度代理（mom_60d - mom_120d），正值更强。"""
        return np.nan  # 实际加速度在 factors.py 内由价格序列计算，此处不重复

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "real_coverage_start": self.real_coverage_start,
            "pepb_rows": 0 if self._real_pepb is None else len(self._real_pepb),
            "fund_rows": 0 if self._real_fund is None else len(self._real_fund),
        }
