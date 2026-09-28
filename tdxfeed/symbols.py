"""证券代码清单：指数/个股/ETF/转债/北交所分组与符号映射。

【v2 扩展】（2026-09-27）
- 新增 `bj` 类别：北交所 920/830/870/43 前缀
- ETF 前缀扩展：510/511/512-519/521-528/56x/58x/15/16/159/18/501
- 可转债扩展：11/12/113/123/127/128
- 新增 `sym_of_sina()`：6 位代码 → Sina 合法 symbol（含 bj 前缀）
- `market_of()` 扩展：返回 0=sz, 1=sh, 2=bj
"""
from __future__ import annotations

import akshare as ak

# 内置指数表：code -> (market, name)；沪市 market=1，深市 market=0
INDICES = {
    "000001": (1, "上证指数"),
    "399001": (0, "深证成指"),
    "399006": (0, "创业板指"),
    "000300": (1, "沪深300"),
    "000905": (1, "中证500"),
}

# 各类别前缀集合（v2 完整版）
SH_PREFIX = ("60", "68")                              # 沪 A 股
SZ_PREFIX = ("00", "30")                              # 深 A 股（主板/创业板/科创板）
BJ_PREFIX = ("920", "830", "870", "43")               # 北交所 A 股
ETF_PREFIX = (
    "510", "511", "512", "513", "515", "516", "517", "518", "519",
    "521", "522", "523", "525", "526", "528", "560", "561", "562", "563",
    "588", "159", "150", "151", "152", "16", "18", "501", "502",
)
BOND_PREFIX = (
    "110", "111", "112", "113",      # 沪市可转债
    "123", "127", "128", "129",      # 深市可转债
    "12",                              # 兼容 v1
)


def get_core_stocks() -> list[str]:
    """沪深300成分股代码（akshare），失败时返回内置样例。"""
    try:
        df = ak.index_stock_cons_csindex(symbol="000300")
        return df["成分券代码"].tolist()
    except Exception:
        return ["600000", "000001", "600519"]


def group(code: str) -> str:
    """按代码前缀分组：sh / sz / bj / etf / bond / other。"""
    if code.startswith(SH_PREFIX):
        return "sh"
    if code.startswith(SZ_PREFIX):
        return "sz"
    if code.startswith(BJ_PREFIX):
        return "bj"
    if code.startswith(ETF_PREFIX):
        return "etf"
    if code.startswith(BOND_PREFIX):
        return "bond"
    return "other"


def sym_of_sina(code: str) -> str | None:
    """6 位代码 → Sina/腾讯合法 symbol（含 bj 前缀）。"""
    g = group(code)
    if g in ("sh", "sz"):
        return g + code
    if g == "bj":
        return "bj" + code
    if g == "etf":
        # 上交所 ETF (5xxxxx, 56x, 58x) → sh；深交所 ETF (15/16/18/159, 50x) → sz
        if code.startswith(("51", "56", "58", "5")) and not code.startswith("5"):
            pass
        sh_etf = ("510", "511", "512", "513", "515", "516", "517", "518", "519",
                  "521", "522", "523", "525", "526", "528", "560", "561", "562",
                  "563", "588")
        return "sh" + code if code.startswith(sh_etf) else "sz" + code
    if g == "bond":
        # 沪市转债 11x/113 → sh；深市转债 12x/123/127/128/129 → sz
        return "sh" + code if code.startswith(("110", "111", "112", "113")) else "sz" + code
    return None


def market_of(code: str) -> int:
    """返回 pytdx market: 1=沪 / 0=深 / 2=北交。"""
    g = group(code)
    if g == "sh":
        return 1
    if g == "sz":
        return 0
    if g == "bj":
        return 2
    # ETF/Bond 用所属交易所
    sym = sym_of_sina(code)
    if sym is None:
        return -1
    if sym.startswith("sh"):
        return 1
    if sym.startswith("sz"):
        return 0
    if sym.startswith("bj"):
        return 2
    return -1


def get_security_list() -> list[str]:
    """全市场 A 股代码清单（akshare），失败时返回内置样例。"""
    try:
        df = ak.stock_info_a_code_name()
        return df["code"].tolist()
    except Exception:
        return ["600000", "000001", "600519"]


# 旧名兼容（download_universe.py / full_sync.py 仍用 sym_of）
def sym_of(code: str) -> str | None:
    """兼容旧名。"""
    return sym_of_sina(code)