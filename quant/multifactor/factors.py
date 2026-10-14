"""五维因子库（momentum / valuation / sentiment / fundflow / fundamental）。

设计约束：
- 复用 factor_engine 的日 K 加载与 limit 口径（此处内联最小实现，避免脚本级耦合）。
- 估值 real（PE/PB/ROE）由 valuation_data.ValuationProvider 在 engine 层注入；
  proxy 模式下由 amount/close 推导占位，报告中披露。
- 所有因子在调仓日做截面 z-score（winsorize 3σ）后参与合成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
SENT_DIR = ROOT / "data" / "sentiment"


# --------------------------------------------------------------------------
# 日 K 加载（内联最小实现，与 factor_engine 口径一致）
# --------------------------------------------------------------------------
def norm_code(code: str) -> str:
    return re.sub(r"^(sh|sz|bj)", "", str(code))


def is_stock_code(code: str) -> bool:
    """判定是否为沪深 A 股个股（剔除指数 / ETF / 债券 / 北交所微盘股）。

    指数（sh000xxx、sz399xxx、sh950xxx）、ETF（sh51/sh56、sz15/sz16）、
    债券（sh01/sh10/sh11/sh12、sz10/sz11/sz12）均不参与截面选股。
    """
    s = norm_code(code)
    if s.startswith(("920", "8")):
        return False  # 北交所微盘股（流动性差、30% 涨跌停）
    if code.startswith("sh"):
        return s.startswith(("60", "68", "90"))
    if code.startswith("sz"):
        return s.startswith(("00", "30", "20"))
    return s.startswith(("60", "68", "90", "00", "30", "20"))


def resolve_parquet(code: str) -> Path:
    bare = norm_code(code)
    for cand in (bare, f"bj{bare}"):
        p = PARQUET_DIR / f"{cand}.parquet"
        if p.exists():
            return p
    return PARQUET_DIR / f"{bare}.parquet"


def get_limit(code: str) -> float:
    s = norm_code(code)
    if s.startswith("920") or s.startswith("8"):
        return 0.295
    if s.startswith("688") or s.startswith("300") or s.startswith("301") or s.startswith("200"):
        return 0.195
    return 0.095


def parse_daily(fp: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(fp, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    except Exception:
        return None
    if df.empty or "close" not in df.columns:
        return None
    if df["date"].dtype in ("int64", "int32"):
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# 因子元信息
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FactorMeta:
    name: str
    category: str          # momentum | valuation | sentiment | fundflow | fundamental
    direction: int         # +1 越大越强, -1 越小越强
    coverage: float = 1.0  # 截面覆盖率闸门；低于 0.6 在合成中剔除并披露

    @property
    def signed(self) -> str:
        return self.name


# 合成用类别→因子映射（valuation 的 pe_ttm/pb_mrq 与 roe_yoy 在 real 模式才有效）
FACTOR_METAS: list[FactorMeta] = [
    FactorMeta("mom_60d", "momentum", +1),
    FactorMeta("mom_120d", "momentum", +1),
    FactorMeta("mom_120d_ex5", "momentum", +1),
    FactorMeta("pe_ttm", "valuation", -1),          # real 模式；proxy 用 val_proxy
    FactorMeta("pb_mrq", "valuation", -1),
    FactorMeta("val_proxy", "valuation", -1),        # proxy 模式占位
    FactorMeta("zt_density_20d", "sentiment", +1),
    FactorMeta("lhb_netbuy_ratio_20d", "sentiment", +1),
    FactorMeta("fundflow_divergence_20d", "fundflow", +1),
    FactorMeta("fund_proxy_accel", "fundamental", +1),
    FactorMeta("roe_yoy", "fundamental", +1),        # real 模式
]

CATEGORY_OF = {m.name: m.category for m in FACTOR_METAS}


@dataclass
class MFConfig:
    start: str = "2020-01-01"
    end: str = "2026-10-14"
    rebalance_freq: str = "W-FRI"         # 周五信号，次周一开盘成交
    valuation_mode: str = "auto"          # proxy | real | auto
    top_n: int = 30
    max_weight: float = 0.08
    max_industry_exposure: float = 0.30
    max_weekly_turnover: float = 0.40
    commission: float = 0.0003
    min_commission: float = 5.0           # 最低佣金（元）
    stamp_tax: float = 0.001              # 卖出印花税
    slippage_main: float = 0.001
    slippage_bj: float = 0.003
    impact_gamma: float = 0.1             # 冲击成本 sqrt 参与率系数（真实量级）
    seed: int = 42
    limit: int = 0                        # 仅前 N 只（冒烟用）
    ipo_min_days: int = 60


# --------------------------------------------------------------------------
# 单股原始因子（可全历史向量化一次）
# --------------------------------------------------------------------------
def compute_stock_raw(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """返回以 date 为索引的因子原始值 DataFrame。

    列：mom_60d/mom_120d/mom_120d_ex5, zt_density_20d, fundflow_divergence_20d,
        fund_proxy_accel, amount_20d_avg, close, limit
    """
    out = pd.DataFrame(index=df.index)
    out["date"] = df["date"]
    close = df["close"]
    pct = close.pct_change()
    vol = df["volume"]

    out["mom_60d"] = close / close.shift(60) - 1
    out["mom_120d"] = close / close.shift(120) - 1
    out["mom_120d_ex5"] = close.shift(5) / close.shift(125) - 1   # 剔除近 5 日反转

    lim = get_limit(code)
    is_up = (pct >= lim - 0.001).fillna(False)
    out["zt_density_20d"] = is_up.rolling(20, min_periods=5).mean()

    vol_chg_20d = vol / vol.shift(20) - 1
    out["fundflow_divergence_20d"] = pct.rolling(20, min_periods=5).sum() - vol_chg_20d
    out["fund_proxy_accel"] = (close / close.shift(60) - 1) - (close / close.shift(120) - 1)

    out["amount_20d_avg"] = df["amount"].rolling(20, min_periods=5).mean()
    out["close"] = close
    out["limit"] = lim
    out = out.set_index("date")
    return out


# --------------------------------------------------------------------------
# 情绪面板（龙虎榜净买占比，trailing 20 日）
# --------------------------------------------------------------------------
def load_lhb_trailing() -> dict[str, pd.Series]:
    """返回 code -> Series(date -> trailing20日 龙虎榜净买额/市场总成交额 均值)。

    覆盖期仅 2025-2026；窗口外序列为空（合成时按缺失率剔除/中性化）。
    """
    res: dict[str, pd.Series] = {}
    for fp in sorted(SENT_DIR.glob("lhb_*2025_2026.parquet")):
        try:
            d = pd.read_parquet(fp)
        except Exception:
            continue
        if "代码" not in d.columns or "上榜日" not in d.columns:
            continue
        d = d.copy()
        d["上榜日"] = pd.to_datetime(d["上榜日"], errors="coerce")
        d = d.dropna(subset=["上榜日"])
        buy = pd.to_numeric(d.get("龙虎榜净买额"), errors="coerce")
        mkt = pd.to_numeric(d.get("市场总成交额"), errors="coerce")
        ratio = buy / mkt.replace(0, np.nan)
        d["ratio"] = ratio
        d["code"] = d["代码"].astype(str).str.zfill(6)
        for code, g in d.groupby("code"):
            g = g.sort_values("上榜日")
            s = g.set_index("上榜日")["ratio"]
            # trailing 20 日均值（按自然日滚动）
            s = s.groupby(s.index).mean()
            roll = s.rolling("20D", min_periods=1).mean()
            res[code] = roll
    return res


# --------------------------------------------------------------------------
# 截面工具
# --------------------------------------------------------------------------
def zscore_panel(panel: pd.DataFrame, winsor: float = 3.0,
                 dir_override: dict[str, int] | None = None) -> pd.DataFrame:
    """对每列做截面 z-score（winsorize），按 direction 定向。

    dir_override：因子名 -> ±1，用于根据样本内 IC 符号校正方向
    （IC 显著为负时翻转，避免「越高越差」的因子被错误加权）。
    """
    z = panel.copy().astype(float)
    for col in z.columns:
        meta = next((m for m in FACTOR_METAS if m.name == col), None)
        if meta is None:
            continue
        direction = meta.direction
        if dir_override and col in dir_override and dir_override[col] != 0:
            direction = meta.direction * dir_override[col]
        v = z[col]
        mu = v.mean()
        sd = v.std()
        if not np.isfinite(sd) or sd == 0:
            z[col] = 0.0
            continue
        zz = (v - mu) / sd
        zz = zz.clip(-winsor, winsor)
        z[col] = zz * direction
    return z


def build_factor_panel(raw: pd.DataFrame, date: pd.Timestamp,
                       lhb: dict[str, pd.Series], val_provider) -> pd.DataFrame:
    """在单个调仓日，构建全市场截面因子矩阵（含估值注入）。

    raw: code -> compute_stock_raw 结果（已按 date 索引）。
    返回 DataFrame：index=code, columns=因子名。
    """
    rows = {}
    for code, fdf in raw.items():
        if date not in fdf.index:
            continue
        r = fdf.loc[date]
        if pd.isna(r.get("close", np.nan)):
            continue
        rec = {
            "mom_60d": r["mom_60d"], "mom_120d": r["mom_120d"],
            "mom_120d_ex5": r["mom_120d_ex5"],
            "zt_density_20d": r["zt_density_20d"],
            "fundflow_divergence_20d": r["fundflow_divergence_20d"],
            "fund_proxy_accel": r["fund_proxy_accel"],
            "amount_20d_avg": r["amount_20d_avg"], "close": r["close"],
        }
        # 情绪：龙虎榜 trailing
        if code in lhb:
            try:
                rec["lhb_netbuy_ratio_20d"] = lhb[code].asof(date)
            except Exception:
                rec["lhb_netbuy_ratio_20d"] = np.nan
        else:
            rec["lhb_netbuy_ratio_20d"] = np.nan
        rows[code] = rec

    panel = pd.DataFrame(rows).T
    if panel.empty:
        cols = ["mom_60d", "mom_120d", "mom_120d_ex5", "zt_density_20d",
                "fundflow_divergence_20d", "fund_proxy_accel", "amount_20d_avg", "close",
                "lhb_netbuy_ratio_20d", "pe_ttm", "pb_mrq", "roe_yoy", "val_proxy"]
        return pd.DataFrame(columns=cols)
    # 估值注入
    if val_provider.mode != "proxy" and val_provider._real_pepb is not None:
        pepb = val_provider._real_pepb
        pepb_d = pepb[pepb["date"] <= date].groupby("code").tail(1)
        fund_d = None
        if val_provider._real_fund is not None:
            fd = val_provider._real_fund
            fund_d = fd[fd["date"] <= date].groupby("code").tail(1)
        for code in panel.index:
            sub = pepb_d[pepb_d["code"] == code]
            if not sub.empty:
                panel.loc[code, "pe_ttm"] = sub["pe_ttm"].iloc[0]
                panel.loc[code, "pb_mrq"] = sub["pb_mrq"].iloc[0]
            if fund_d is not None:
                fs = fund_d[fund_d["code"] == code]
                if not fs.empty:
                    panel.loc[code, "roe_yoy"] = fs["roe_yoy"].iloc[0]
    # proxy 估值占位
    if "pe_ttm" not in panel:
        panel["pe_ttm"] = np.nan
    if "pb_mrq" not in panel:
        panel["pb_mrq"] = np.nan
    if "roe_yoy" not in panel:
        panel["roe_yoy"] = np.nan
    if "amount_20d_avg" in panel.columns:
        amt = pd.to_numeric(panel["amount_20d_avg"], errors="coerce")
        panel["val_proxy"] = -np.log(amt.where(amt > 0))
    else:
        panel["val_proxy"] = np.nan
    return panel
