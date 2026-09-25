"""证券代码清单：指数/个股/ETF/转债分组与核心标的生成。"""
import akshare as ak

# 内置指数表：code -> (market, name)；沪市 market=1，深市 market=0
INDICES = {
    "000001": (1, "上证指数"),
    "399001": (0, "深证成指"),
    "399006": (0, "创业板指"),
    "000300": (1, "沪深300"),
    "000905": (1, "中证500"),
}


def get_core_stocks():
    """沪深300成分股代码（akshare），失败时返回内置样例。"""
    try:
        df = ak.index_stock_cons_csindex(symbol="000300")
        codes = df["成分券代码"].tolist()
    except Exception:
        codes = ["600000", "000001", "600519"]
    return codes


def group(code):
    """按代码前缀分组：sh 沪A / sz 深A / etf / bond / other。"""
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("51", "58", "560", "561", "562", "563",
                       "520", "526", "530", "15", "16", "18")):
        return "etf"
    if code.startswith(("11", "12")):
        return "bond"
    return "other"


def market_of(code):
    return 1 if group(code) == "sh" else 0


def get_security_list():
    """全市场A股代码清单（akshare），失败时返回内置样例。"""
    try:
        df = ak.stock_info_a_code_name()
        codes = df["code"].tolist()
    except Exception:
        codes = ["600000", "000001", "600519"]
    return codes
