"""涨跌幅规则：精确比例（算价）与判定阈值（认板）是两套合同，不可混用。

| 用途 | 接口 | 值 |
|---|---|---|
| 挂单/涨跌停价 | limit_ratio + limit_up_price | 0.10 / 0.20 / 0.30，Decimal 取整到分 |
| 「是否涨停」判定 | limit_threshold | 比例 − 0.5% 缓冲，0.095 / 0.195 / 0.295 |

主板 ST 在 2026-07-06 与普通票并轨（±5% → ±10%），因此比例必须能按交易日判；
不传 on_date 按现行规则。创业板/科创板/北交所的 ST 不单独设档。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

MARKET_MAIN = "main"
MARKET_GEM = "gem"
MARKET_BSE = "bse"

LIMIT_RATIO: dict[str, Decimal] = {
    MARKET_MAIN: Decimal("0.10"),
    MARKET_GEM: Decimal("0.20"),
    MARKET_BSE: Decimal("0.30"),
}
LIMIT_RATIO_ST_MAIN_OLD = Decimal("0.05")
LIMIT_RATIO_ST_MAIN = Decimal("0.10")
MAIN_ST_RULE_CHANGE_DATE = date(2026, 7, 6)
DEFAULT_THRESHOLD_BUFFER = 0.005
RET_TOLERANCE = 0.001

_CENT = Decimal("0.01")
_ONE = Decimal("1")

# 交易所前缀（sh/sz/bj）：代码归一的**唯一**一份，两个契约共用同一条底层逻辑。
EXCHANGE_PREFIXES = ("sh", "sz", "bj")
_EXCHANGE_PREFIX_RE = re.compile(r"^(sh|sz|bj)")


def strip_exchange_prefix(code: str) -> str:
    """【契约 B · 仅剥前缀】去掉 sh/sz/bj 前缀，其余**原样**（大小写、长度都不动）。

    调用场景：输入是**内部生成**的标识——日线文件名（`sh600000`）、带前缀的裸代码。
    这类输入必须与文件路径 / 名称映射逐字对齐（名称映射的键是 6 位裸码），
    所以不能顺手 lower / 补零：`sz000001` 与 `000001` 在这里必须保持可区分。

    与 `normalize_code` 的差别只有「不做 lower、不做 zfill(6)」，其余共用同一条底层实现。
    **两个契约都要保留**（审计 Round2 2026-10-18 注记："契约不同, 勿盲目统一"）：
    把这里的调用换成 `normalize_code` 会把 `"1"` 变成 `"000001"`、把 `"SH600000"` 变成小写，
    改变的是「同一输入在两条链路上得到同一字符串」这件事本身。
    """
    return _EXCHANGE_PREFIX_RE.sub("", str(code))


def normalize_code(code: str) -> str:
    """【契约 A · 规范化】lower + 剥前缀 + 零填充到 6 位。

    调用场景：输入是 **CLI 用户输入**（`600519` / `sh600519` / `" 600519 "`），
    需要一个能直接与日线文件名对齐的 6 位裸码。

    底层与 `strip_exchange_prefix` 共用同一条前缀剥离逻辑（不复制粘贴）。
    """
    c = str(code).lower().strip()
    return strip_exchange_prefix(c).zfill(6)


def market_of(code: str) -> str:
    c = normalize_code(code)
    if c.startswith(("920", "8", "4")):
        return MARKET_BSE
    if c.startswith(("300", "301", "688", "689")):
        return MARKET_GEM
    return MARKET_MAIN


def as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("-", "")[:8]
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return None


def limit_ratio(code: str, *, is_st: bool = False, on_date=None) -> Decimal:
    market = market_of(code)
    if market == MARKET_MAIN and is_st:
        d = as_date(on_date)
        if d is not None and d < MAIN_ST_RULE_CHANGE_DATE:
            return LIMIT_RATIO_ST_MAIN_OLD
        return LIMIT_RATIO_ST_MAIN
    return LIMIT_RATIO[market]


def limit_threshold(code: str, *, is_st: bool = False, on_date=None,
                    buffer: float = DEFAULT_THRESHOLD_BUFFER) -> float:
    return float(limit_ratio(code, is_st=is_st, on_date=on_date) - Decimal(str(buffer)))


def _quantize_cents(raw: Decimal) -> float:
    return float(raw.quantize(_CENT, rounding=ROUND_HALF_UP))


def _as_decimal(ratio: Decimal | float) -> Decimal:
    return ratio if isinstance(ratio, Decimal) else Decimal(str(ratio))


def limit_up_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE + _as_decimal(ratio)))


def limit_down_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE - _as_decimal(ratio)))


def is_sealed(price: float, limit_price: float) -> bool:
    """已封住涨停价（容差半分钱，规避浮点误差）。"""
    if limit_price <= 0 or price <= 0:
        return False
    return price >= limit_price - 0.005


def is_limit_up_ret(ret: float, limit: float) -> bool:
    return ret >= (limit - RET_TOLERANCE)


def open_is_limit_up(open_price: float, prev_close: float, code: str, *,
                     is_st: bool = False, on_date=None) -> bool:
    """开盘即涨停（阈值合同）：判定是否「买不进」，不是挂单价。"""
    if prev_close is None or prev_close <= 0:
        return False
    limit = limit_threshold(code, is_st=is_st, on_date=on_date)
    return is_limit_up_ret(open_price / prev_close - 1.0, limit)


@dataclass(frozen=True)
class LimitRule:
    """把「哪只票、是不是 ST、容差多少」绑在一起，按日供给比例。"""
    code: str
    is_st: bool = False
    tol: float = 0.005

    def ratio(self, on_date=None) -> Decimal:
        return limit_ratio(self.code, is_st=self.is_st, on_date=on_date)

    def up_price(self, prev_close: float, on_date=None) -> float:
        return limit_up_price(prev_close, self.ratio(on_date))

    def down_price(self, prev_close: float, on_date=None) -> float:
        return limit_down_price(prev_close, self.ratio(on_date))

    def is_open_limit_up(self, open_price: float, prev_close: float, on_date=None) -> bool:
        return open_is_limit_up(open_price, prev_close, self.code,
                               is_st=self.is_st, on_date=on_date)

    def is_sealed(self, price: float, prev_close: float, on_date=None) -> bool:
        return is_sealed(price, self.up_price(prev_close, on_date))

    def is_sealed_at(self, price: float, limit_price: float) -> bool:
        return is_sealed(price, limit_price)
