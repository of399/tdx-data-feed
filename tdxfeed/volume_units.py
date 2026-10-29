"""腾讯 K 线 volume 单位：科创板回股，其余回手；本地档案统一手。

2026-10-30 实测（proxy / ifzq / web 三台一致）：sh688981 当日回 28429754，
行内成交额列 285795.10 万元与 vol×close 吻合（⇒ 股）；同日 sh600000 回 753949，
amount/close/100 与之相等（⇒ 手）。不折算会在同一只票里造出 100 倍单位断层。
"""

STAR_PREFIXES = ("sh688", "sh689")


def to_hands(symbol: str, volume: float) -> float:
    return volume / 100.0 if str(symbol).startswith(STAR_PREFIXES) else volume
