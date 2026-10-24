"""全市场增量同步：pytdx 连接层取代码清单 + Sina 源拉日K（白天主力，后台运行）。
【v2.1】main() 末尾接入财务出数（fin_parquet.incremental：核心300 finance/xdxr 断点续传 + parquet 转换）。
【v2.2】新增指数下载通道 download_indices：通达信 get_index_bars 主 + Sina getKLineData 兜底，
       指数以市场前缀 symbol（sh000001/sz399001…）写入 parquet/daily/，与个股同目录且互不冲突。

用法: nohup venv/bin/python -m tdxfeed.full_sync >> logs/full-sync.log 2>&1 &
清单来源优先级:
  1. akshare 沪深A股全量（白天可用）优先；pytdx 分页重试兜底
  2. 失败时回退 core_stocks + INDICES + 内置样例（少量）
【v2】数据源由 Tencent 换成 Sina（白天可用，含 amount 支持）。
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.state import State
from tdxfeed.symbols import INDICES, get_core_stocks, group, sym_of_index
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
            except Exception as e:
                log.warning("分页读取异常，count 置 0: %s", e)
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
                    except Exception as e:
                        log.warning("get_security_list 重试失败: %s", e)
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
    """按代码前缀映射 Sina 合法 symbol：仅沪深股票返回 sh/sz + 代码。"""
    g = group(code)
    if g in ("sh", "sz"):
        return g + code
    return None


def download_indices(w: Writers, state: State, src: SinaSource):
    """指数下载通道（纳入日线同步）：mootdx get_index_bars 主，Sina getKLineData 兜底。

    【阶段 0 加固 · 2026-10-10】主通道由已归档的 pytdx 切换为活跃维护的 mootdx；
    两者底层同为通达信协议，volume 单位均=手，与 Sina 兜底（股→÷100→手）对齐，
    落盘由 writers.merge_write 统一合并（复用 2026-10-09 kind='mergesort' 去重修复）。
    - 指数 symbol 用市场前缀命名（sh000001 / sz399001 等），写入 parquet/daily/，
      与个股（sz000001=平安银行）互不冲突。
    - 本环境实测 TDX 公共服务器不吐指数（返回 0 根），会自动落到 Sina。
    """
    from tdxfeed.sources.mootdx_source import MootdxSource
    # 构造 + 一次性探测：mootdx 不可用时（本环境 TDX 被封锁）全程走 Sina，避免逐指数超时挂死
    pydx = None
    try:
        pydx = MootdxSource()
        if not pydx.probe():
            print("[index] mootdx 探测不可用，全程走 Sina 兜底", flush=True)
            pydx = None
    except Exception as e:
        print(f"[index] mootdx 初始化/探测失败: {e!s}，全程走 Sina 兜底", flush=True)
    ok = fail = 0
    mootdx_hit = sina_hit = fallback = 0
    for code, (mkt, name) in INDICES.items():
        sym = sym_of_index(code)
        bars, channel = [], "none"
        # 1) 通达信通道（主，mootdx）；失败/空/超时 抛 RuntimeError 后回退 Sina
        if pydx is not None:
            try:
                ib = pydx.fetch_index_bars(mkt, code)
                if ib:
                    bars, channel = ib, "mootdx"
            except Exception as e:
                print(f"[index] mootdx {name}({sym}) 失败: {e!s}", flush=True)
        # 2) Sina 兜底（fetch_bars 已 ÷100 转手，与 mootdx 主通道单位一致）
        if not bars:
            try:
                bars = src.fetch_bars(sym, datalen=1023)
                channel = "sina"
                fallback += 1
            except Exception as e:
                print(f"[index] sina {name}({sym}) 失败: {e!s}", flush=True)
        if bars:
            n = w.merge_write(sym, bars, category="daily")
            last = str(Writers._date_int(bars[-1][0]))
            state.update(sym, last, n)
            print(f"[index] {name}({sym}) ← {channel} {n} 行 末日 {last}", flush=True)
            ok += 1
            if channel == "mootdx":
                mootdx_hit += 1
            else:
                sina_hit += 1
        else:
            print(f"[index] {name}({sym}) 无数据，跳过", flush=True)
            fail += 1
    print(f"[index] 完成: 成功{ok} 失败{fail} ｜ mootdx主{mootdx_hit} Sina兜底{sina_hit} 回退{fallback}", flush=True)
    return {"ok": ok, "fail": fail, "mootdx": mootdx_hit, "sina": sina_hit, "fallback": fallback}


def write_sync_summary(individual: dict, index: dict, out_root: str):
    """写出同步质量汇总 JSON，供 verify_daily_freshness 计算“源失败率”指标。

    源失败率 = (个股失败 + 指数失败) / (个股成功 + 个股失败 + 指数成功 + 指数失败)
    目标：0（即同步零失败；本环境 TDX 公共服务器不可达时由 Sina 兜底，个股通道不计失败）。
    """
    import json

    ind_ok, ind_fail = individual.get("ok", 0), individual.get("fail", 0)
    idx_ok, idx_fail = index.get("ok", 0), index.get("fail", 0)
    total_ok = ind_ok + idx_ok
    total_fail = ind_fail + idx_fail
    total = total_ok + total_fail
    rate = (total_fail / total) if total > 0 else 0.0
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "individual": individual,
        "index": index,
        "source_failure_rate": round(rate, 6),
        "source_failure_rate_pct": round(rate * 100, 4),
        "zero_failure": total_fail == 0,
    }
    out_dir = os.path.join(out_root, "..", "logs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "sync_summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(f"[summary] 源失败率={summary['source_failure_rate_pct']}% 已写入 {path}", flush=True)
    return summary


# 下游 predict-daily / plan-e-journal 只靠钟表错开（Wants= 而非 Requires=），
# 退出码是它们唯一能看到的同步质量闸门，因此必须非零反映真实失败。
SYNC_BAD_RATIO_MAX = 0.2


def sync_exit_code(individual: dict, index: dict) -> int:
    """同步健康判定：0=可放行下游，1=systemd 应记为失败。

    skip（拿到空 bars）计入 bad 是这条函数的存在理由：源全线返回空数据时
    ok 与 fail 同时为 0，write_sync_summary 的 source_failure_rate 会算出 0%，
    把彻底失败报成完美。阈值 20% 的依据是正常夜晚 fail+skip 约 0.4%。
    """
    ind_ok = individual.get("ok", 0)
    ind_fail = individual.get("fail", 0)
    ind_skip = individual.get("skip", 0)
    idx_ok = index.get("ok", 0)
    idx_fail = index.get("fail", 0)

    bad = ind_fail + ind_skip + idx_fail
    total = ind_ok + idx_ok + bad
    if total == 0 or ind_ok + idx_ok == 0:
        return 1
    if bad / total > SYNC_BAD_RATIO_MAX:
        return 1
    return 0


def main():
    codes = get_all_codes()
    if not codes:
        codes = get_core_stocks() + list(INDICES.keys())
    # 仅同步沪深股票（北交所/ETF/债券已于 2026-10-21 下线）
    syms = []
    for c in codes:
        c = c.zfill(6)
        if group(c) not in ("sh", "sz"):
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

    # 【阶段 0 加固 · 2026-10-10】指数下载主通道切换为 mootdx，返回通道统计
    try:
        print("[index] 指数日线同步开始（mootdx 主 + Sina 兜底）", flush=True)
        idx_stats = download_indices(w, state, src)
    except Exception as e:
        print(f"[index] 指数同步异常: {e!s}", flush=True)
        idx_stats = {"ok": 0, "fail": 0, "mootdx": 0, "sina": 0, "fallback": 0}

    # 【v2.1 新增】财务出数接入主链路：核心300 finance/xdxr 断点续传 + parquet 转换
    try:
        from tdxfeed.fin_parquet import incremental
        print("[fin] 财务出数开始（核心300，断点续传）", flush=True)
        incremental()
    except Exception as e:
        print(f"[fin] 财务出数失败: {e!s}", flush=True)

    # 【阶段 0 加固 · 2026-10-10】写出同步质量汇总（含源失败率），供新鲜度校验消费
    ind_stats = {"ok": ok, "fail": fail, "skip": skip}
    write_sync_summary(ind_stats, idx_stats, OUT_ROOT)

    return sync_exit_code(ind_stats, idx_stats)


if __name__ == "__main__":
    sys.exit(main())
