"""交易时段判定（竞价 / 连续竞价 / 休市）。

打板的三段节奏（计划书 §9 SOP）：

============  ==============  ====================================================
时段          时间             程序行为
============  ==============  ====================================================
集合竞价      09:15–09:25      现价 = 虚拟开盘价；封单量 = 未撮合买量
              （09:15–09:20 可撤单，09:20–09:25 不可撤单）
竞价静默      09:25–09:30      撮合完成，等待开盘
连续竞价      09:30–11:30      正常打板窗口
午间休市      11:30–13:00      不动
连续竞价      13:00–15:00      尾盘偷袭窗口（14:30 后）
已收盘        15:00 之后        快照为收盘数据，仅供复盘
============  ==============  ====================================================
"""
from __future__ import annotations

from datetime import datetime, time as dtime

__all__ = [
    "SESSION_AUCTION",
    "SESSION_CLOSED",
    "SESSION_CONTINUOUS",
    "SESSION_LUNCH",
    "SESSION_PRE_AUCTION",
    "SESSION_PRE_OPEN",
    "current_session",
]

SESSION_PRE_AUCTION = "pre_auction"  # 09:15 前
SESSION_AUCTION = "auction"  # 09:15–09:25 集合竞价
SESSION_PRE_OPEN = "pre_open"  # 09:25–09:30 竞价静默
SESSION_CONTINUOUS = "continuous"  # 连续竞价
SESSION_LUNCH = "lunch_break"  # 午间休市
SESSION_CLOSED = "closed"  # 已收盘

_AUCTION_START = dtime(9, 15)
_AUCTION_END = dtime(9, 25)
_OPEN_AM = dtime(9, 30)
_CLOSE_AM = dtime(11, 30)
_OPEN_PM = dtime(13, 0)
_CLOSE_PM = dtime(15, 0)


def current_session(now: datetime | None = None) -> str:
    """返回当前交易时段标签。"""
    t = (now or datetime.now()).time()
    if t < _AUCTION_START:
        return SESSION_PRE_AUCTION
    if t < _AUCTION_END:
        return SESSION_AUCTION
    if t < _OPEN_AM:
        return SESSION_PRE_OPEN
    if t < _CLOSE_AM:
        return SESSION_CONTINUOUS
    if t < _OPEN_PM:
        return SESSION_LUNCH
    if t < _CLOSE_PM:
        return SESSION_CONTINUOUS
    return SESSION_CLOSED


def is_auction(session: str) -> bool:
    """是否集合竞价时段（含 09:20 前可撤单阶段）。"""
    return session == SESSION_AUCTION


def is_continuous(session: str) -> bool:
    return session == SESSION_CONTINUOUS
