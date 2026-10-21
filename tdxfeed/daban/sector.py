"""板块强度 Sec 因子（计划书 §A.1）。

公式（计划书原文）::

    Sec = 40×板块涨幅排名百分位 + 40×min(板块涨停数,10)/10 + 20×领涨股涨幅/10

三项均归一到 0–100，Sec 为 0–100 的板块强度分。

数据来源（零外部依赖）：
- **成分股分组**：`data/industry/sw_stock_industry.csv`（申万行业，5931 只，本地缓存）
- **涨幅/涨停判定**：新浪 L1 全市场实时快照（scanner 已在拉）
- **行业名称**：可选 `data/industry/sector_name.csv`（code,name），缺省显示行业代码

概念板块（THS）目前缺成分股接口 → 见 `scripts/daban_sector_loader.py`，
可通过本地映射文件注入，走同一套计算。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SectorStat",
    "compute_sector_strength",
    "load_industry_map",
    "load_sector_names",
    "sector_of",
]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
INDUSTRY_MAP_PATH = DATA_DIR / "industry" / "sw_stock_industry.csv"
SECTOR_NAME_PATH = DATA_DIR / "industry" / "sector_name.csv"
INDUSTRY_NAME_PATH = DATA_DIR / "industry" / "industry_name.csv"


@dataclass(slots=True)
class SectorStat:
    """一个板块（申万行业）的实时强度统计。"""

    sector_code: str
    sector_name: str
    member_n: int = 0
    limit_up_n: int = 0
    median_pct: float = 0.0
    leader_pct: float = 0.0
    rank_pct: float = 0.0  # 板块涨幅在全部板块中的百分位 0~1
    sec: float = 0.0  # 0~100

    def to_dict(self) -> dict:
        return {
            "sector_code": self.sector_code,
            "sector_name": self.sector_name,
            "member_n": self.member_n,
            "limit_up_n": self.limit_up_n,
            "median_pct": round(self.median_pct, 2),
            "leader_pct": round(self.leader_pct, 2),
            "rank_pct": round(self.rank_pct, 1),
            "sec": round(self.sec, 1),
        }


def load_industry_map(path: Path | str = INDUSTRY_MAP_PATH) -> dict[str, str]:
    """载入 code → industry_code 映射。

    `sw_stock_industry.csv` 的 code 带交易所前缀（sz000001），这里统一成 6 位。
    """
    p = Path(path)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            code = str(row.get("code", "")).strip()
            ind = str(row.get("industry_code", "")).strip()
            if not code or not ind:
                continue
            c = code.lower()
            for prefix in ("sh", "sz", "bj"):
                if c.startswith(prefix):
                    c = c[2:]
                    break
            out[c.zfill(6)] = ind
    return out


def load_sector_names(path: Path | str = SECTOR_NAME_PATH) -> dict[str, str]:
    """载入 code → 行业名（可选）。"""
    p = Path(path)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if code and name:
                c = code.lower()
                for prefix in ("sh", "sz", "bj"):
                    if c.startswith(prefix):
                        c = c[2:]
                        break
                out[c.zfill(6)] = name
    return out


def _bare_code(code: str) -> str:
    """去交易所前缀 → 6 位代码。"""
    c = str(code).strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if c.startswith(prefix):
            c = c[2:]
            break
    return c.zfill(6)


def load_industry_names(path: Path | str = INDUSTRY_NAME_PATH) -> dict[str, str]:
    """载入 industry_code → 行业名。

    申万在线接口（sw_index_third_info）当前不可用，本地 850 体系（旧版）与
    480301 体系（2021 版）编码不同、无法自动对齐，故提供人工/半自动补录文件：
    ``data/industry/industry_name.csv`` 列为 industry_code,name。
    """
    p = Path(path)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = str(row.get("industry_code", "")).strip()
            name = str(row.get("name", "")).strip()
            if code and name:
                out[code] = name
    return out


def sector_of(code: str, industry_map: dict[str, str]) -> str:
    """个股所属板块代码（申万行业码），无映射返回空串。"""
    return industry_map.get(_bare_code(code), "")


def compute_sector_strength(
    quotes: dict,
    industry_map: dict[str, str],
    is_limit_up_fn,
    sector_names: dict[str, str] | None = None,
    min_members: int = 3,
    industry_names: dict[str, str] | None = None,
) -> dict[str, SectorStat]:
    """按全市场实时快照计算各板块强度。

    参数:
        quotes:       {symbol: Quote}（新浪 L1 全市场快照）
        industry_map: code → sector_code
        is_limit_up_fn: (price, prev_close, code) -> bool，判定是否封死涨停
        sector_names: code → 行业名（可选）
        min_members:  成分股少于该数的板块不参与排名（样本太小不可信）

    返回:
        {sector_code: SectorStat}
    """
    pcts: dict[str, list[float]] = defaultdict(list)
    limit_ups: dict[str, int] = defaultdict(int)
    leaders: dict[str, float] = defaultdict(float)
    members: dict[str, list[str]] = defaultdict(list)

    for symbol, q in quotes.items():
        if q.suspended or q.prev_close <= 0:
            continue
        code = _bare_code(symbol)
        sec = industry_map.get(code, "")
        if not sec:
            continue
        pct = q.pct()
        pcts[sec].append(pct)
        members[sec].append(code)
        leaders[sec] = max(leaders[sec], pct)
        if is_limit_up_fn(q.price, q.prev_close, code):
            limit_ups[sec] += 1

    stats: dict[str, SectorStat] = {}
    for sec, plist in pcts.items():
        if len(plist) < min_members:
            continue
        plist_sorted = sorted(plist)
        mid = len(plist_sorted) // 2
        median = (
            plist_sorted[mid]
            if len(plist_sorted) % 2
            else (plist_sorted[mid - 1] + plist_sorted[mid]) / 2
        )
        # 板块名：优先 industry_code→名称；否则从成员个股的行业名反查
        name = (industry_names or {}).get(sec) or next(
            (sector_names[c] for c in members[sec] if c in sector_names),
            sec,
        )
        stats[sec] = SectorStat(
            sector_code=sec,
            sector_name=name,
            member_n=len(plist),
            limit_up_n=limit_ups.get(sec, 0),
            median_pct=median,
            leader_pct=leaders.get(sec, 0.0),
        )

    # 板块涨幅排名百分位（中位数口径）
    ranked = sorted(stats.values(), key=lambda s: s.median_pct)
    n = len(ranked)
    for i, s in enumerate(ranked):
        s.rank_pct = (i + 1) / n if n else 0.0

    for s in stats.values():
        # 领涨股涨幅按 10% 封顶（20cm 板不再额外加分），保证 Sec ∈ [0, 100]
        s.sec = (
            40 * s.rank_pct
            + 40 * min(s.limit_up_n, 10) / 10
            + 20 * min(max(s.leader_pct, 0.0), 10.0) / 10
        )
    return stats
