"""财务摘要：akshare best-effort。

pytdx 的 get_finance_info 依赖行情数据层（当前实测不可用），
故降级为 akshare 接口；akshare 也不可达时返回空列表并记录日志，
不阻塞主流程。
"""
import time
from collections import Counter

import akshare as ak

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()


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
            _ERROR_COUNT['fetch_finance'] += 1
            print(f"finance {symbol} 第{attempt+1}次失败: {str(e)[:60]}", flush=True)
            time.sleep(2 * (attempt + 1))
    return []
