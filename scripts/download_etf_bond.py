#!/usr/bin/env python3
"""下载 ETF 日 K 到 data/parquet/daily/{sh|sz}{code}.parquet。

【v2 治理产物】（2026-09-27）
补齐 ETF 日 K，弥补原始 `group()` 只识别 stock 的空白。

【债券下线】（2026-09-28）项目政策：不再保留债券数据，可转债下载已弃用。

数据源优先级：Sina（含 ETF/转债）→ akshare(Eastmoney) → Tencent
列与沪深北交所日 K 完全一致：date, open, high, low, close, volume, amount
v2 写入：date 强制 date32（复用 Writers.merge_write 的归一化）

用法:
    # 全量 ETF + 可转债（约 1500+ 只）
    venv/bin/python scripts/download_etf_bond.py

    # 仅 ETF（默认）
    venv/bin/python scripts/download_etf_bond.py --only etf

    # 限速/限数
    venv/bin/python scripts/download_etf_bond.py --limit 100 --sleep 0.3
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd

from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.writers import Writers

ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
W = Writers(str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("etf_bond")


def list_etf_codes() -> list[str]:
    """akshare 列出 ETF 代码（去重 6 位零填充）。"""
    try:
        df = ak.fund_etf_spot_em()
        codes = df["代码"].astype(str).str.zfill(6).tolist()
        return sorted(set(codes))
    except Exception as e:
        log.warning("akshare fund_etf_spot_em 失败: %s", e)
        return []


def list_bond_codes() -> list[str]:
    """可转债下载已弃用（项目政策：不再保留债券数据）。始终返回空。"""
    log.warning("可转债下载已弃用：项目政策不再保留债券数据，list_bond_codes 返回 []")
    return []


def sym_from_code(code: str) -> str | None:
    """6 位代码 → Sina 合法 symbol（含 sh/sz 前缀）。"""
    from tdxfeed.symbols import group, sym_of_sina
    g = group(code)
    if g != "etf":
        return None
    return sym_of_sina(code)


def fetch_one(symbol: str, code: str) -> str:
    """单只拉取 → merge_write。返回 'ok' / 'empty' / 'fail:...'。"""
    src = SinaSource()
    try:
        bars = src.fetch_bars(symbol, scale=240, datalen=1023)
    except Exception as e:
        return f"fail:{type(e).__name__}:{str(e)[:50]}"
    if not bars:
        return "empty"
    fp = DAILY_DIR / f"{symbol}.parquet"
    try:
        W.merge_write(symbol, bars)
        return "ok"
    except Exception as e:
        return f"fail:write:{type(e).__name__}:{str(e)[:50]}"


def main() -> None:
    raise SystemExit("已移除：ETF 数据已于 2026-10-21 下线，本脚本不再使用")
    ap = argparse.ArgumentParser(description="下载 ETF / 可转债 日 K")
    ap.add_argument("--only", choices=["etf", "all"], default="etf")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 只")
    ap.add_argument("--sleep", type=float, default=0.3, help="每只间隔秒")
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []  # (kind, code)
    if args.only in ("etf", "all"):
        for c in list_etf_codes():
            targets.append(("etf", c))
    if args.only == "bond":
        log.warning("可转债下载已弃用（项目政策：不再保留债券数据），跳过")

    log.warning("候选 %d 只 (etf+bond)", len(targets))

    if args.limit:
        targets = targets[: args.limit]

    counters: dict[str, int] = {}
    t0 = time.time()
    for i, (kind, code) in enumerate(targets, 1):
        sym = sym_from_code(code)
        if sym is None:
            counters["skip_not_etf_bond"] = counters.get("skip_not_etf_bond", 0) + 1
            continue
        fp = DAILY_DIR / f"{sym}.parquet"
        # 增量：已有且末日期 >= 昨日 → 跳过（避免重复）
        if fp.exists() and fp.stat().st_size > 0:
            try:
                last = pd.read_parquet(fp, columns=["date"])["date"].max()
                import datetime as _dt
                if pd.notna(last) and last >= pd.Timestamp(_dt.date.today() - _dt.timedelta(days=2)):
                    counters["cached"] = counters.get("cached", 0) + 1
                    if i % 100 == 0:
                        log.info("  [%d/%d] %s cached", i, len(targets), sym)
                    time.sleep(args.sleep)
                    continue
            except Exception as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
                print(f"[WARN] swallowed: {e!r}")
        result = fetch_one(sym, code)
        counters[result] = counters.get(result, 0) + 1
        if i % 100 == 0 or i == len(targets):
            el = time.time() - t0
            log.info("  [%d/%d] status=%s el=%.0fs", i, len(targets), counters, el)
        time.sleep(args.sleep)

    log.warning("完成 counters=%s", counters)


if __name__ == "__main__":
    main()
