"""涨停价计算与涨跌幅规则（A 股现行规则）。

⚠ 与 `tdxfeed.features_5d.get_limit` 的口径差异（务必分清，不可混用）：

| 用途 | 模块 | 比例 | 说明 |
|---|---|---|---|
| 回测/特征判定"是否涨停" | features_5d.get_limit | 0.095 / 0.195 / 0.295 | 留 0.5% 浮点缓冲 |
| **盘中实时打板挂单价** | 本模块 | 0.10 / 0.20 / 0.30 | 交易所真实比例 + 四舍五入到分（主板 ST 在 2026-07-06 前是 0.05） |

打板挂单必须用真实比例算出的**精确到分**的涨停价，否则会出现 0.01 元的
"差一分钱没封上/挂不上"的实盘事故。

涨跌幅规则（含 2026-07-06 规则变更）：
- 主板（sh60x / sz000 / sz001 / sz002）：±10%
- 创业板（sz300 / sz301）、科创板（sh688）：±20%
- 北交所（bj920 / 8 / 4）：±30%
- 新股上市前 5 个交易日不设涨跌幅（由上层"上市 > 30 日"过滤兜住）

ST / *ST 的比例是**随日期变化**的（这是 2026 年最重要的口径变更）：
- 2026-07-06 之前：仅主板 ST/*ST 为 ±5%；创业板/科创板 ST 为 20%，北交所 ST 为 30%。
- 2026-07-06 起（上证发〔2026〕41 号《上海证券交易所交易规则（2026 年修订）》第 3.3.13 条，
  深交所同步修订）：主板风险警示股票涨跌幅由 5% 调整为 10%，与主板普通股票并轨，
  不再单列一档；科创板/创业板 ST 维持 20%，北交所 ST 维持 30%。

因此 `limit_ratio` 的 ST 分支必须按交易日判断：做历史区间时传 `on_date`，
不传则按现行规则（主板 ST = 10%）。通达信自带样本公式里那条
`ISZBST股:=NAMEINCLUDE('ST') AND FINANCE(3)=1` 是 2026-07-06 之前的旧口径，别照抄。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "LIMIT_RATIO",
    "LIMIT_RATIO_ST_MAIN",
    "LIMIT_RATIO_ST_MAIN_OLD",
    "MAIN_ST_RULE_CHANGE_DATE",
    "MARKET_BSE",
    "MARKET_GEM",
    "MARKET_MAIN",
    "board_label",
    "exchange_prefix",
    "is_sealed",
    "is_st",
    "limit_down_price",
    "limit_ratio",
    "limit_up_price",
    "market_of",
    "normalize_code",
    "pct_change",
]

MARKET_MAIN = "main"
MARKET_GEM = "gem"
MARKET_BSE = "bse"

LIMIT_RATIO: dict[str, Decimal] = {
    MARKET_MAIN: Decimal("0.10"),
    MARKET_GEM: Decimal("0.20"),
    MARKET_BSE: Decimal("0.30"),
}
LIMIT_RATIO_ST_MAIN_OLD = Decimal("0.05")  # 2026-07-06 之前
LIMIT_RATIO_ST_MAIN = Decimal("0.10")      # 2026-07-06 起与主板普通股并轨

# 主板风险警示股票涨跌幅由 5% 调整为 10% 的生效日（含当日）。
MAIN_ST_RULE_CHANGE_DATE = date(2026, 7, 6)

_CENT = Decimal("0.01")
_ONE = Decimal("1")
_PREFIXES = ("sh", "sz", "bj")


def normalize_code(code: str) -> str:
    """统一为 6 位纯数字代码（去 sh/sz/bj 前缀、零填充）。"""
    c = str(code).lower().strip()
    for p in _PREFIXES:
        if c.startswith(p):
            c = c[2:]
            break
    return c.zfill(6)


def market_of(code: str) -> str:
    """判断所属板块：main / gem / bse。"""
    c = normalize_code(code)
    if c.startswith(("920", "8", "4")):
        return MARKET_BSE
    if c.startswith(("300", "301", "688", "689")):
        return MARKET_GEM
    return MARKET_MAIN


def exchange_prefix(code: str) -> str:
    """交易所前缀：sh / sz / bj。"""
    c = normalize_code(code)
    if market_of(c) == MARKET_BSE:
        return "bj"
    return "sh" if c[0] in "69" else "sz"


def is_st(name: str) -> bool:
    """名称是否含 ST / *ST（含退市整理 "退" 另由上层过滤）。"""
    return "ST" in str(name).replace(" ", "").upper()


def as_date(value) -> date | None:
    """把 'YYYYMMDD' / 'YYYY-MM-DD' / datetime / date 统一成 date；无法识别返回 None。"""
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


_as_date = as_date


def limit_ratio(code: str, name: str = "", on_date=None) -> Decimal:
    """该股的日涨跌幅限制比例。

    on_date 只影响主板 ST/*ST 一档：早于 2026-07-06 取 5%，之后取 10%（与主板普通票并轨）。
    不传 on_date 按现行规则（10%）；做 2020-2026 的历史区间一定要把交易日传进来。
    """
    market = market_of(code)
    if market == MARKET_MAIN and is_st(name):
        d = _as_date(on_date)
        if d is not None and d < MAIN_ST_RULE_CHANGE_DATE:
            return LIMIT_RATIO_ST_MAIN_OLD
        return LIMIT_RATIO_ST_MAIN
    return LIMIT_RATIO[market]


def _quantize_cents(raw: Decimal) -> float:
    """四舍五入到分（交易所口径，非银行家舍入）。"""
    return float(raw.quantize(_CENT, rounding=ROUND_HALF_UP))


def limit_up_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    """涨停价 = 前收盘 × (1 + 比例)，四舍五入到分。"""
    r = ratio if isinstance(ratio, Decimal) else Decimal(str(ratio))
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE + r))


def limit_down_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    """跌停价 = 前收盘 × (1 - 比例)，四舍五入到分。"""
    r = ratio if isinstance(ratio, Decimal) else Decimal(str(ratio))
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE - r))


def is_sealed(price: float, limit_price: float) -> bool:
    """是否已封住涨停（容差半分钱，规避浮点误差）。"""
    if limit_price <= 0 or price <= 0:
        return False
    return price >= limit_price - 0.005


def pct_change(price: float, prev_close: float) -> float:
    """涨跌幅（%）。"""
    if prev_close <= 0:
        return 0.0
    return (price / prev_close - 1.0) * 100.0


def board_label(code: str, name: str = "", on_date=None) -> str:
    """人类可读的涨跌幅标签，用于输出列（如 "主板10%"、"主板ST10%"）。

    主板 ST 在 2026-07-06 前是 5%，之后并轨 10%，标签跟着 on_date 走。
    """
    market = market_of(code)
    if market == MARKET_MAIN:
        if is_st(name):
            d = _as_date(on_date)
            old = d is not None and d < MAIN_ST_RULE_CHANGE_DATE
            return "主板ST5%" if old else "主板ST10%"
        return "主板10%"
    if market == MARKET_GEM:
        return "双创20%"
    return "北交所30%"
