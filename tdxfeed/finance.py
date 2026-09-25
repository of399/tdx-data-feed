"""财务摘要：akshare best-effort。

pytdx 的 get_finance_info 依赖行情数据层（当前实测不可用），
故降级为 akshare 接口；akshare 也不可达时返回空列表并记录日志，
不阻塞主流程。
"""
import time

import akshare as ak


def fetch_finance(symbol):
    """返回财务摘要记录列表（list[dict]），失败返回空列表。"""
    code = symbol[-6:]
    for attempt in range(3):
        try:
            df = ak.stock_financial_abstract(symbol=code)
            if df is not None and not df.empty:
                return df.to_dict("records")
            return []
        except Exception as e:
            print(f"finance {symbol} 第{attempt+1}次失败: {str(e)[:60]}", flush=True)
            time.sleep(2 * (attempt + 1))
    return []
