"""市场环境判定（计划书 §6.1）+ 各环境仓位参数（§6.2 / §A.3）。

判定式严格对齐《A股打板交易策略执行计划书》§6.1 的伪码：

    退潮期    : 跌停家数 > 20 且 连板高度 ≤ 3
    冰点转折  : 连板高度 2~3 且 涨停家数 < 40
    启动期    : 连板高度 4~5 且 涨停家数 40~80
    高潮期    : 连板高度 ≥ 7 且 涨停家数 > 100 且 跌停家数 < 5
    上升期    : 连板高度 ≥ 3 且 涨停家数 60~100
    熊市      : 涨停 + 跌停家数 < 20
    震荡市    : 其余

盘前跑一次，产物写 data/signals/market_phase_YYYYMMDD.json，
被扫描器用于决定仓位档位与买点类型。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "PHASE_PARAMS",
    "PhaseStats",
    "detect_market_phase",
    "phase_params",
    "read_phase",
    "write_phase",
]


@dataclass(slots=True)
class PhaseStats:
    """市场情绪统计量。"""

    limit_up_n: int = 0
    limit_down_n: int = 0
    max_consecutive: int = 0
    sector_break_n: int = 0
    trade_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def detect_market_phase(stats: PhaseStats | dict) -> str:
    """返回市场环境标签（7 选 1）。"""
    if isinstance(stats, dict):
        stats = PhaseStats(**{k: v for k, v in stats.items() if k in PhaseStats.__slots__})
    n_up = stats.limit_up_n
    n_dn = stats.limit_down_n
    h = stats.max_consecutive
    if n_dn > 20 and h <= 3:
        return "退潮期"
    if 2 <= h <= 3 and n_up < 40:
        return "冰点转折"
    if 4 <= h <= 5 and 40 <= n_up <= 80:
        return "启动期"
    if h >= 7 and n_up > 100 and n_dn < 5:
        return "高潮期"
    if h >= 3 and 60 <= n_up <= 100:
        return "上升期"
    if n_up + n_dn < 20:
        return "熊市"
    return "震荡市"


# §6.2 仓位表 + §A.3 参数模板（节选可执行字段）
PHASE_PARAMS: dict[str, dict] = {
    "退潮期": {"total_pos": "0-10%", "single_cap": "5%", "stop_loss": -0.03, "buy": "仅逆势活口，必须回封"},
    "冰点转折": {"total_pos": "20-40%", "single_cap": "10%", "stop_loss": -0.04, "buy": "新题材首板"},
    "启动期": {"total_pos": "30-50%", "single_cap": "15%", "stop_loss": -0.05, "buy": "板块日内龙头"},
    "高潮期": {"total_pos": "60-80%", "single_cap": "20-25%", "stop_loss": -0.05, "buy": "前排龙头回封"},
    "上升期": {"total_pos": "60-70%", "single_cap": "30%", "stop_loss": -0.05, "buy": "连板高度龙/回封"},
    "熊市": {"total_pos": "0%", "single_cap": "—", "stop_loss": None, "buy": "空仓"},
    "震荡市": {"total_pos": "≤30%", "single_cap": "10%", "stop_loss": -0.04, "buy": "低位轮动首板"},
}


def phase_params(phase: str) -> dict:
    """取该环境的仓位/止损/买点参数。"""
    return dict(PHASE_PARAMS.get(phase, PHASE_PARAMS["震荡市"]))


def write_phase(stats: PhaseStats, out_dir: str | Path, phase: str | None = None) -> Path:
    """写 market_phase_YYYYMMDD.json，返回路径。"""
    p = phase or detect_market_phase(stats)
    payload = {
        "phase": p,
        "params": phase_params(p),
        "stats": stats.to_dict(),
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"market_phase_{stats.trade_date or 'latest'}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_phase(out_dir: str | Path) -> dict | None:
    """读取最近一次 market_phase json；没有则返回 None。"""
    out = Path(out_dir)
    files = sorted(out.glob("market_phase_*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))
