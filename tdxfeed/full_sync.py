"""全市场增量同步：pytdx 连接层取代码清单 + Sina 源拉日K（白天主力，后台运行）。
【v2.1】main() 末尾接入财务出数（fin_parquet.incremental：核心300 finance/xdxr 断点续传 + parquet 转换）。

用法: nohup venv/bin/python -m tdxfeed.full_sync >> logs/full-sync.log 2>&1 &
清单来源优先级:
  1. pytdx get_security_count/get_security_list（连接层，实测可用，取全市场证券）
  2. 失败时回退 core_stocks + INDICES + 内置样例（少量）
【v2】数据源由 Tencent 换成 Sina（白天可用，含 amount 支持）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.state import State
from tdxfeed.symbols import INDICES, get_core_stocks, group
from tdxfeed.writers import Writers

OUT_ROOT = os.environ.get("TDX_OUT_ROOT", "/home/jiuben/tdx-data-feed/data")
SLEEP = float(os.environ.get("TDX_SLEEP", "0.15"))


def get_all_codes():
    """全市场证券清单：akshare 沪深A股全量（5562，白天可用）优先；pytdx 分页重试兜底。"""
    # 1) akshare 全量 A 股清单
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        codes = [str(c).zfill(6) for c in df["code"].tolist()]
        print(f"akshare 清单: {len(codes)} 个 A 股", flush=True)
        return codes
    except Exception as e:
        print(f"akshare 清单失败: {e!s}，走 pytdx 兜底", flush=True)
    # 2) pytdx 分页（带重试 + 断点）
    try:
        from pytdx.hq import TdxHq_API
        api = TdxHq_API()
        if not api.connect("218.6.170.47", 7709, time_out=5):
            print("pytdx 连接失败，回退样例清单", flush=True)
            return None
        codes = []
        for market in (0, 1):
            try:
                count = api.get_security_count(market)
            except Exception:
                count = 0
            start = 0
            miss = 0
            while start < count and miss < 3:
                lst = None
                for _attempt in range(3):
                    try:
                        lst = api.get_security_list(market, start)
                        if lst:
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                if not lst:
                    miss += 1
                    start += 1000
                    continue
                miss = 0
                for item in lst:
                    codes.append(str(item["code"]))
                start += 1000
        api.disconnect()
        print(f"pytdx 清单: {len(codes)} 个证券", flush=True)
        return codes
    except Exception as e:
        print(f"pytdx 清单失败: {e!s}，回退样例", flush=True)
        return None

def sym_of(code):
    """按代码前缀映射 Sina 合法 symbol：sh/sz + 代码。"""
    g = group(code)
    if g in ("sh", "sz"):
        return g + code
    if g == "etf":
        return ("sh" if code.startswith(("51", "58", "56", "52", "53")) else "sz") + code
    if g == "bond":
        return ("sh" if code.startswith("11") else "sz") + code
    return None

def main():
    codes = get_all_codes()
    if not codes:
        codes = get_core_stocks() + list(INDICES.keys())
    # 过滤：按前缀分组，跳过 other（北交所等先排除）
    syms = []
    for c in codes:
        c = c.zfill(6)
        g = group(c)
        if g == "other":
            continue
        sym = sym_of(c)
        if sym:
            syms.append(sym)
    print(f"待同步标的: {len(syms)}", flush=True)

    src = SinaSource()
    w = Writers(OUT_ROOT)
    state = State(os.path.join(OUT_ROOT, ".state", "manifest.json"))
    ok = fail = skip = 0
    for sym in syms:
        try:
            bars = src.fetch_bars(sym)
            if bars:
                n = w.merge_write(sym, bars)
                last = str(bars[-1]).replace("-", "")
                state.update(sym, last, n)
                ok += 1
            else:
                skip += 1
        except Exception as e:
            fail += 1
            print(f"FAIL {sym}: {e!s}", flush=True)
        time.sleep(SLEEP)
    print(f"完成: 成功{ok} 跳过{skip} 失败{fail}", flush=True)

    # 【v2.1 新增】财务出数接入主链路：核心300 finance/xdxr 断点续传 + parquet 转换
    try:
        from tdxfeed.fin_parquet import incremental
        print("[fin] 财务出数开始（核心300，断点续传）", flush=True)
        incremental()
    except Exception as e:
        print(f"[fin] 财务出数失败: {e!s}", flush=True)


if __name__ == "__main__":
    main()
