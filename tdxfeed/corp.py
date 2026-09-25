"""股本变动/分红统计（xdxr）：akshare best-effort。

【v2 修复】akshare 1.18.94 的 stock_history_dividend 为【无参】接口，返回全市场
分红统计表（列: 代码/名称/上市日期/累计股息/年均股息/分红次数/融资总额/融资次数）。
原版错误地传 symbol=/indicator= 会抛 TypeError（got an unexpected keyword
argument），一旦 akshare 网络恢复也永远拿不到数据。修正：无参调用后按『代码』列过滤。
注意：此表为分红统计（非逐笔除权除息明细）；stock_fhps_em 逐笔接口在 1.18.94
存在内部 bug（TypeError: NoneType not subscriptable）不可用，复权因子暂由
腾讯/Sina qfq 价差方案提供，xdxr 逐笔明细列为待核实。
akshare 断连时返回空列表并记录日志，不阻塞主流程（计划书 §8）。
"""
import time

import akshare as ak


def fetch_xdxr(symbol):
    """返回该标的的股本变动/分红统计记录（list[dict]），失败返回空列表。"""
    code = symbol[-6:]
    for attempt in range(3):
        try:
            df = ak.stock_history_dividend()  # 无参：全市场分红统计表
            if df is not None and not df.empty:
                sub = df[df['代码'].astype(str) == code]
                return sub.to_dict('records')
            return []
        except Exception as e:
            print(f"xdxr {symbol} 第{attempt+1}次失败: {str(e)[:60]}", flush=True)
            time.sleep(2 * (attempt + 1))
    return []
