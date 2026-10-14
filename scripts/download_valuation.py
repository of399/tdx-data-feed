#!/usr/bin/env python3
"""download_valuation.py —— 真实估值/基本面数据采集器

将 A 股真实估值(PE/PB)与基本面(ROE 同比)数据落盘到 data/valuation/。
【绝不修改】data/parquet/daily 与 data/adj_factor 下任何已有文件。

设计背景
--------
多因子升级需要真实估值(PE/PB)与基本面(ROE 增速)。本环境已知东财
`stock_zh_a_hist` 被阻断(见 quant/README.md)，因此采用:
  - 估值: ak.stock_zh_valuation_baidu (百度, 非东财)   [优先]
  - 备选: ak.stock_a_indicator_lg (乐咕乐股) —— 本版 akshare 若不存在则自动跳过
  - 基本面: ak.stock_financial_analysis_indicator (东财财务端点, 非 hist)
并配套「proxy 因子先行 + 真实数据到位后 auto 切换」策略。

CLI
---
  --probe          仅探测接口可用性, 不下载任何数据
  --mode real      全量下载 (可断点续传: 已落盘 code 自动跳过)
  --incremental    仅补充缺失 code (与 real 等价续传语义, 保留以兼容)
  --limit N        仅处理前 N 只 (验证用, 可与 --mode real 组合)

输出 (data/valuation/)
  - pe_pb_daily.parquet : long 格式  code, date, pe_ttm, pb_mrq
  - fundamentals.parquet: code, date, roe_yoy
  - _download_report.json: 探测/下载报告 (status/probe/coverage/fails)
"""
import argparse
import json
import os
import socket
import sys
import time
import traceback
import pandas as pd
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
# 抑制 akshare 内部 tqdm 进度条噪声 (写入日志无用)
os.environ.setdefault("TQDM_DISABLE", "1")
# 全局网络超时, 避免单只 hang 阻塞整批 (与 download_adj_factor_b.py 一致)
socket.setdefaulttimeout(25)

ROOT = Path("/home/jiuben/tdx-data-feed")
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
VAL_DIR = ROOT / "data" / "valuation"
VAL_DIR.mkdir(parents=True, exist_ok=True)
PE_PB_PATH = VAL_DIR / "pe_pb_daily.parquet"
FUND_PATH = VAL_DIR / "fundamentals.parquet"
REPORT_PATH = VAL_DIR / "_download_report.json"
LOG_PATH = ROOT / "logs" / "download_valuation.log"

# 百度估值单次调用最大历史窗口
BAIDU_PERIOD = "近五年"
# 指数退避重试: 1s / 4s / 9s, 共 3 次
RETRY_DELAYS = (1, 4, 9)
# 每只之间限速(秒), 避免触发风控
THROTTLE = 0.2
# 每多少只写一次盘 (断点续传粒度)
BATCH = 200

# 明显的指数代码 (从 daily 文件名中剔除, 避免污染覆盖率统计)
INDEX_FILTER = {
    ("sh", "000001"), ("sh", "000002"), ("sh", "000003"), ("sh", "000010"),
    ("sh", "000015"), ("sh", "000016"), ("sh", "000300"), ("sh", "000688"),
    ("sh", "000698"), ("sh", "000905"), ("sh", "000906"), ("sh", "000907"),
    ("sh", "000852"),
}


class RetryExhausted(Exception):
    pass


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def with_retry(fn, *args, **kwargs):
    """指数退避重试 (1s/4s/9s, 共 3 次), 全部失败抛出 RetryExhausted。"""
    last = None
    for i, d in enumerate(RETRY_DELAYS):
        time.sleep(d)
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RetryExhausted(f"{type(last).__name__}: {str(last)[:120]}")


def get_codes():
    """从 data/parquet/daily/*.parquet 派生 6 位股票代码 (去掉 sh/sz/bj 前缀), 过滤指数。"""
    import pandas as pd  # noqa: F401  (确保依赖存在)

    codes = set()
    for fp in DAILY_DIR.glob("*.parquet"):
        if fp.name.endswith(".bak.parquet"):
            continue
        stem = fp.name[:-8]  # 去掉 .parquet
        if len(stem) < 8:
            continue
        prefix, code = stem[:2], stem[2:]
        if prefix not in ("sh", "sz", "bj"):
            continue
        if (prefix, code) in INDEX_FILTER:
            continue
        if prefix == "sz" and code.startswith("399"):
            continue
        if not code.isdigit() or len(code) != 6:
            continue
        codes.add(code)
    return sorted(codes)


def load_done_codes():
    """已落盘估值数据的 code 集合 (用于断点续传)。"""
    done = set()
    if PE_PB_PATH.exists():
        try:
            import pandas as pd
            df = pd.read_parquet(PE_PB_PATH, columns=["code"])
            done.update(df["code"].astype(str).tolist())
        except Exception:  # noqa: BLE001
            pass
    return done


def append_parquet(path: Path, new_df):
    import pandas as pd
    if path.exists():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
        # 安全去重 (按 code,date)
        keys = [c for c in ("code", "date") if c in combined.columns]
        if keys:
            combined = combined.drop_duplicates(keys)
    else:
        combined = new_df
    combined.to_parquet(path, index=False)


# --------------------------------------------------------------------------- #
# 数据拉取
# --------------------------------------------------------------------------- #
def fetch_valuation(ak, code: str):
    """百度估值: PE(TTM) + 市净率, 合并为 long 格式。失败返回 None。"""
    try:
        pe = with_retry(ak.stock_zh_valuation_baidu, symbol=code,
                        indicator="市盈率(TTM)", period=BAIDU_PERIOD)
    except RetryExhausted:
        pe = None
    try:
        pb = with_retry(ak.stock_zh_valuation_baidu, symbol=code,
                        indicator="市净率", period=BAIDU_PERIOD)
    except RetryExhausted:
        pb = None

    if pe is None and pb is None:
        return None

    if pe is not None:
        pe = pe.rename(columns={"value": "pe_ttm"})[["date", "pe_ttm"]]
    if pb is not None:
        pb = pb.rename(columns={"value": "pb_mrq"})[["date", "pb_mrq"]]

    if pe is not None and pb is not None:
        df = pe.merge(pb, on="date", how="outer")
    elif pe is not None:
        df = pe
    else:
        df = pb

    df["code"] = code
    for c in ("pe_ttm", "pb_mrq"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["pe_ttm", "pb_mrq"], how="all")
    return df[["code", "date", "pe_ttm", "pb_mrq"]] if len(df) else None


def fetch_fundamentals(ak, code: str):
    """东财财务分析: 取 净资产收益率(%) 计算 ROE 同比(对去年同期)。失败返回 None。"""
    try:
        df = with_retry(ak.stock_financial_analysis_indicator,
                        symbol=code, start_year="2020")
    except RetryExhausted:
        return None
    if df is None or len(df) == 0:
        return None
    need = {"日期": "date", "净资产收益率(%)": "roe"}
    if not set(need).issubset(df.columns):
        return None
    df = df.rename(columns=need)
    df["date"] = df["date"].astype(str)
    df["roe"] = pd.to_numeric(df["roe"], errors="coerce")
    df = df.dropna(subset=["roe"]).sort_values("date").reset_index(drop=True)
    if len(df) == 0:
        return None
    prev = df["roe"].shift(4)  # 季度数据, 同比 = 去年同期
    df["roe_yoy"] = (df["roe"] - prev) / prev.abs() * 100.0
    df["roe_yoy"] = df["roe_yoy"].replace([float("inf"), -float("inf")], float("nan"))
    df["code"] = code
    return df[["code", "date", "roe_yoy"]]


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def do_probe():
    import importlib.util

    report = {
        "stage": "probe",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "akshare_installed": importlib.util.find_spec("akshare") is not None,
        "interfaces": {},
        "status": "proxy",
    }
    if not report["akshare_installed"]:
        log("❌ akshare 未安装, 无法拉取真实数据, 降级为 proxy 模式。")
        report["note"] = "akshare not installed; 仅产出脚本, 真实下载跳过。"
        write_report(report)
        return report

    import akshare as ak
    import pandas as pd

    test_codes = ["600519", "000001", "300750", "920002"]
    # 1) 主接口: 百度估值
    baidu_ok, baidu_sample = False, None
    for code in test_codes:
        try:
            df = with_retry(ak.stock_zh_valuation_baidu, symbol=code,
                            indicator="市盈率(TTM)", period=BAIDU_PERIOD)
            if df is not None and len(df):
                baidu_ok = True
                _h = df.head(1).copy()
                _h["date"] = _h["date"].astype(str)
                baidu_sample = {
                    "code": code,
                    "columns": list(df.columns),
                    "rows": int(len(df)),
                    "head": _h.to_dict(orient="records"),
                    "date_range": [str(df["date"].min()), str(df["date"].max())],
                }
                break
        except RetryExhausted:
            continue
    report["interfaces"]["stock_zh_valuation_baidu"] = {
        "available": baidu_ok,
        "sample": baidu_sample,
    }

    # 2) 备选: 乐咕乐股 (本版 akshare 可能不存在)
    lg_ok = hasattr(ak, "stock_a_indicator_lg")
    report["interfaces"]["stock_a_indicator_lg"] = {
        "available": lg_ok,
        "note": "本版 akshare 无此接口, 自动跳过" if not lg_ok else "可用",
    }

    # 3) 基本面: 东财财务分析 (非 hist 端点)
    fund_ok, fund_sample = False, None
    for code in test_codes:
        try:
            df = with_retry(ak.stock_financial_analysis_indicator,
                            symbol=code, start_year="2023")
            if df is not None and "净资产收益率(%)" in df.columns and len(df):
                fund_ok = True
                fund_sample = {
                    "code": code,
                    "columns_have_roe": "净资产收益率(%)" in df.columns,
                    "rows": int(len(df)),
                    "date_range": [str(df["日期"].min()), str(df["日期"].max())],
                }
                break
        except RetryExhausted:
            continue
    report["interfaces"]["stock_financial_analysis_indicator"] = {
        "available": fund_ok,
        "sample": fund_sample,
    }

    # 覆盖率粗估: 用估值主接口在测试集上的命中率外推
    codes = get_codes()
    total = len(codes)
    report["universe_total"] = total
    if baidu_ok:
        hit = sum(1 for c in test_codes[:3]
                  if _try_baidu(ak, c))
        cov = hit / max(len(test_codes[:3]), 1)
        report["estimated_coverage"] = round(cov, 3)
        report["status"] = "ok" if cov > 0 else "proxy"
    else:
        report["estimated_coverage"] = 0.0
        report["status"] = "proxy"

    # 打印结论
    log("=" * 60)
    log("PROBE 结论")
    log(f"  akshare 安装: {report['akshare_installed']}")
    log(f"  估值主接口  stock_zh_valuation_baidu : {'✅ 可用' if baidu_ok else '❌ 不可用'}")
    if baidu_sample:
        log(f"    样例字段: {baidu_sample['columns']}  行数: {baidu_sample['rows']}")
        log(f"    样例区间: {baidu_sample['date_range']}  首行: {baidu_sample['head']}")
    log(f"  估值备选    stock_a_indicator_lg       : {'✅ 可用' if lg_ok else '⚠️ 本版缺失, 跳过'}")
    log(f"  基本面接口  stock_financial_analysis_indicator : {'✅ 可用' if fund_ok else '❌ 不可用'}")
    if fund_sample:
        log(f"    样例区间: {fund_sample['date_range']}  行数: {fund_sample['rows']}")
    log(f"  股票池总数: {total}")
    log(f"  预估覆盖率: {report.get('estimated_coverage')}")
    if report["status"] == "proxy":
        log("  >>> 降级为 proxy 模式, report 标记 status=proxy, 不启动全量下载。")
    else:
        log("  >>> 估值主接口可用, 可启动真实下载 (--limit 验证后 --mode real)。")
    log("=" * 60)

    write_report(report)
    return report


def _try_baidu(ak, code: str) -> bool:
    try:
        df = with_retry(ak.stock_zh_valuation_baidu, symbol=code,
                        indicator="市盈率(TTM)", period=BAIDU_PERIOD)
        return df is not None and len(df) > 0
    except RetryExhausted:
        return False


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download_all(mode: str, limit: int = None):
    import akshare as ak
    import pandas as pd

    codes = get_codes()
    if limit:
        codes = codes[:limit]
    log(f"=== 下载启动 mode={mode} limit={limit} 待处理={len(codes)} ===")

    done = load_done_codes()
    pe_rows, fund_rows = [], []
    ok = fail = cached = 0
    fail_list = []
    t0 = time.time()

    for i, code in enumerate(codes):
        if code in done:
            cached += 1
            continue

        # 估值 (主交付物)
        pe_df = None
        try:
            pe_df = fetch_valuation(ak, code)
        except Exception as e:  # noqa: BLE001
            log(f"  [{i+1}/{len(codes)}] {code} valuation ERR {type(e).__name__}: {str(e)[:80]}")
        if pe_df is not None and len(pe_df):
            pe_rows.append(pe_df)
            ok += 1
        else:
            fail += 1
            fail_list.append((code, "valuation_empty_or_fail"))
            # 估值缺失则跳过基本面, 避免无效写入
            time.sleep(THROTTLE)
            if (i + 1) % 100 == 0:
                _progress(i, len(codes), ok, fail, cached, t0)
            continue

        # 基本面 (ROE 同比)
        try:
            fund_df = fetch_fundamentals(ak, code)
            if fund_df is not None and len(fund_df):
                fund_rows.append(fund_df)
        except Exception as e:  # noqa: BLE001
            log(f"  [{i+1}/{len(codes)}] {code} fundamentals ERR {type(e).__name__}: {str(e)[:80]}")

        if (i + 1) % 100 == 0:
            _progress(i, len(codes), ok, fail, cached, t0)

        time.sleep(THROTTLE)

    # 末批落盘
    if pe_rows:
        append_parquet(PE_PB_PATH, pd.concat(pe_rows, ignore_index=True))
    if fund_rows:
        append_parquet(FUND_PATH, pd.concat(fund_rows, ignore_index=True))

    elapsed = (time.time() - t0) / 60
    log(f"\n=== 完成 mode={mode} === ok={ok} fail={fail} cached={cached} 耗时={elapsed:.1f}min")
    if fail_list:
        log(f"失败 {len(fail_list)} 只 (前20):")
        for c, r in fail_list[:20]:
            log(f"  {c}: {r}")

    report = {
        "stage": "download",
        "mode": mode,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok" if fail == 0 else "partial",
        "universe_total": len(get_codes()),
        "processed": ok + fail + cached,
        "ok": ok,
        "fail": fail,
        "cached": cached,
        "fail_codes": [c for c, _ in fail_list],
        "outputs": {
            "pe_pb_daily": str(PE_PB_PATH),
            "fundamentals": str(FUND_PATH),
        },
        "data_boundary": {
            "valuation_period": BAIDU_PERIOD,
            "valuation_note": "百度估值单次调用上限约5年, 实际区间以各股上市/数据可得性为准",
            "fundamentals_start_year": "2020",
            "fundamentals_note": "季度报告期(03-31/06-30/09-30/12-31), roe_yoy 为对去年同期同比",
        },
    }
    write_report(report)
    return report


def _progress(i, total, ok, fail, cached, t0):
    elapsed = time.time() - t0
    speed = (i + 1 - cached) / max(elapsed, 1)
    remain = total - i - 1
    eta = remain / max(speed, 0.01) / 60
    log(f"  [{i+1}/{total}] ok={ok} fail={fail} cached={cached} "
        f"speed={speed:.2f}只/s ETA={eta:.0f}min")


def write_report(report: dict):
    try:
        with REPORT_PATH.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log(f"report 写入失败: {e}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="A股真实估值/基本面数据采集")
    ap.add_argument("--probe", action="store_true", help="仅探测接口可用性")
    ap.add_argument("--mode", choices=["real", "incremental"], help="下载模式")
    ap.add_argument("--limit", type=int, default=None, help="仅前 N 只(验证)")
    ap.add_argument("--incremental", action="store_true", help="增量(等价续传)")
    args = ap.parse_args()

    if args.probe:
        do_probe()
        return

    mode = args.mode or ("incremental" if args.incremental else "real")
    try:
        download_all(mode, limit=args.limit)
    except Exception:
        with REPORT_PATH.open("a", encoding="utf-8") as f:
            f.write(f"\n[FATAL] {traceback.format_exc()}\n")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(f"[FATAL] {traceback.format_exc()}")
        raise
