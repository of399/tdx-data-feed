"""封单比统一计算 + 外部数据源接入（计划书 §2.6 / 附录 E）。

三种封单口径：

============  ==========================  ==========================================
口径          公式                        数据来源
============  ==========================  ==========================================
金额法        封单额 / 流通市值            L1 委买一额 + 流通股本（或通达信导出）
股数法        封单股数 / 当日成交量        L1 委买一量 + 实时成交量（L2/快照源）
本地代理      封单股数 / 近 5 日均量      L1 委买一量 + 本地 parquet（无外部依赖）
============  ==========================  ==========================================

本模块提供：
- ``compute_seal_ratio``：统一入口，按可用数据自动选择口径
- ``load_tdx_export``：解析通达信导出的封单快照 CSV（附录 E 第 1 阶段）
- ``SealRatioSource``：协议，接入东财 L2 / 同花顺 L2 只需实现 ``fetch``
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "SealRatio",
    "SealRatioSource",
    "compute_seal_ratio",
    "grade_seal_ratio",
    "load_tdx_export",
]

# 计划书 §2.6 档位（金额法/股数法同口径）
GRADE_BANDS = (
    (0.005, "极弱"),
    (0.02, "弱"),
    (0.08, "正常偏强"),
    (0.20, "强"),
)
GRADE_TOP = "极强"


@dataclass(slots=True)
class SealRatio:
    """封单比计算结果。"""

    code: str
    seal_shares: float = 0.0
    seal_amount: float = 0.0
    ratio_amount: float | None = None  # 金额法
    ratio_shares: float | None = None  # 股数法
    ratio_proxy: float | None = None  # 本地代理（/近5日均量）
    grade: str = ""
    basis: str = ""  # 口径说明

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "seal_shares": self.seal_shares,
            "seal_amount": self.seal_amount,
            "ratio_amount": self.ratio_amount,
            "ratio_shares": self.ratio_shares,
            "ratio_proxy": self.ratio_proxy,
            "grade": self.grade,
            "basis": self.basis,
        }


def grade_seal_ratio(ratio: float | None) -> str:
    """封单比分档（§2.6）。"""
    if ratio is None:
        return ""
    for threshold, label in GRADE_BANDS:
        if ratio < threshold:
            return label
    return GRADE_TOP


def compute_seal_ratio(
    code: str,
    seal_shares: float,
    seal_amount: float,
    float_mcap: float | None = None,
    volume_shares: float | None = None,
    avg_volume5_shares: float | None = None,
) -> SealRatio:
    """统一封单比计算（按数据可得性自动降级）。

    参数:
        code: 6 位代码
        seal_shares: 封单股数
        seal_amount: 封单金额（元）
        float_mcap: 流通市值（元）→ 启用金额法
        volume_shares: 当日成交量（股）→ 启用股数法
        avg_volume5_shares: 近 5 日均量（股）→ 启用本地代理
    """
    r = SealRatio(code=code, seal_shares=seal_shares, seal_amount=seal_amount)
    if float_mcap and float_mcap > 0:
        r.ratio_amount = seal_amount / float_mcap
        r.grade = grade_seal_ratio(r.ratio_amount)
        r.basis = "金额法(封单额/流通市值)"
    if volume_shares and volume_shares > 0:
        r.ratio_shares = seal_shares / volume_shares
        if not r.grade:
            r.grade = grade_seal_ratio(r.ratio_shares)
            r.basis = "股数法(封单股数/当日成交量)"
    if avg_volume5_shares and avg_volume5_shares > 0:
        r.ratio_proxy = seal_shares / avg_volume5_shares
        if not r.grade:
            r.grade = grade_seal_ratio(r.ratio_proxy)
            r.basis = "本地代理(封单股数/近5日均量)"
    return r


class SealRatioSource(Protocol):
    """外部封单数据源协议（东财 L2 / 同花顺 super L2 / 通达信导出）。

    实现 ``fetch(codes)`` 返回 {code: {"seal_shares": float, "seal_amount": float,
    "volume_shares": float | None}}，即可被 scanner 直接消费。
    """

    def fetch(self, codes: list[str]) -> dict[str, dict]:  # pragma: no cover - 协议
        ...


# ---------------------------------------------------------------- 通达信导出

_TDX_COL_ALIASES = {
    "code": ("code", "代码", "证券代码", "股票代码", "symbol"),
    "name": ("name", "名称", "证券名称", "股票简称"),
    "seal_shares": ("seal_shares", "委买一量", "委买量", "封单量", "封单股数", "buy1_vol"),
    "seal_amount": ("seal_amount", "委买一额", "委买额", "封单额", "封单金额", "buy1_amt"),
    "float_mcap": ("float_mcap", "流通市值", "流通市值(元)", "float_cap"),
    "volume": ("volume", "成交量", "现量", "当日成交量"),
    "time": ("time", "时间", "行情时间"),
    "price": ("price", "现价", "最新价", "收盘价"),
    "limit_price": ("limit_price", "涨停价", "涨停板价"),
}


def _pick(row: dict, key: str) -> str:
    for alias in _TDX_COL_ALIASES[key]:
        for k, v in row.items():
            if k and k.strip().lower() == alias.lower():
                return (v or "").strip()
    return ""


def _num(text: str) -> float:
    try:
        return float(str(text).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def load_tdx_export(path: Path | str) -> dict[str, dict]:
    """解析通达信导出的封单快照 CSV（附录 E 第 1 阶段产物）。

    列名兼容中英文：代码/名称/委买一量/委买一额/流通市值/成交量/时间/现价/涨停价。
    数值列会去掉千分位逗号与百分号。

    返回:
        {code: {"seal_shares","seal_amount","float_mcap","volume","time","price",
        "limit_price","name"}}
    """
    p = Path(path)
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = _pick(row, "code")
            if not code:
                continue
            c = code.lower()
            for prefix in ("sh", "sz", "bj"):
                if c.startswith(prefix):
                    c = c[2:]
                    break
            c = c.zfill(6)
            out[c] = {
                "name": _pick(row, "name"),
                "seal_shares": _num(_pick(row, "seal_shares")),
                "seal_amount": _num(_pick(row, "seal_amount")),
                "float_mcap": _num(_pick(row, "float_mcap")),
                "volume": _num(_pick(row, "volume")),
                "time": _pick(row, "time"),
                "price": _num(_pick(row, "price")),
                "limit_price": _num(_pick(row, "limit_price")),
            }
    return out
