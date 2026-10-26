"""把一只票的日线向历史方向补到 target 之前，写盘只走 Writers.merge_write。

不直接 write_parquet 的理由：merge_write 已带三条保护——mergesort + keep='last' 去重、
同日 amount 取 max（防深取段的 0 抹掉已有真值）、用合并结果重建 .day。第四条
fill_missing_amount（给 amount<=0 且有量的行补估算）2026-10-26 还只在别的工作树改动里，
深取段的 amount 因此目前就是 0；它接不接线本层不假设，判据见
tests/test_backfill_history_runner.py 的 _AMOUNT_ESTIMATE_WIRED。

续跑只有一份权威：done 集（data/.state/history_backfill.json）。这里刻意不再写第二份
State manifest——`State.save()` 是整文件重写，3 线程并发会互相覆盖，而回填真正需要回答的
只有「这只补过没有」；补得对不对由 merge_write 的幂等性保证（重跑无害）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from tdxfeed.history_paging import fetch_history
from tdxfeed.writers import Writers

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = str(ROOT / "data")
RESUME_PATH = str(ROOT / "data" / ".state" / "history_backfill.json")


def load_done(state_path: str = RESUME_PATH) -> set[str]:
    if not os.path.exists(state_path):
        return set()
    with open(state_path, encoding="utf-8") as fh:
        return set(json.load(fh).get("done", []))


def mark_done(state_path: str, symbol: str) -> None:
    """原子换文件（.tmp + os.replace）。只在主线程调用：读-改-写不是原子的，
    worker 线程里并发跑会丢条目。"""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    done = load_done(state_path) | {symbol}
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"done": sorted(done)}, fh, ensure_ascii=False)
    os.replace(tmp, state_path)


def backfill_symbol(symbol: str, target: int, *, fetch=fetch_history,
                    writers: Writers | None = None) -> dict:
    """深取 [target 之前, 今天] 全段并合并写盘；返回新增行数与首末日期。

    fetch 可注入（测试用假页函数，零网络）。0 行一律抛错：深取通道「返回空」和
    「这只票确实没历史」在这一层分不开，宁可让 runner 红，也不要静默少补。
    """
    w = writers if writers is not None else Writers(DEFAULT_OUT_ROOT)
    t = str(int(target))
    want_from = f"{t[:4]}-{t[4:6]}-{t[6:]}"
    bars = fetch(symbol, want_from)
    if not bars:
        raise RuntimeError(f"{symbol} 深取返回 0 行：这不是历史尽头，是通道故障")
    path = os.path.join(w.out_root, "parquet", "daily", f"{symbol}.parquet")
    before = len(pd.read_parquet(path, columns=["date"])) if os.path.exists(path) else 0
    total = w.merge_write(symbol, bars, category="daily")
    return {"symbol": symbol, "rows_added": max(0, total - before),
            "total_rows": total, "first_date": int(bars[0][0]),
            "last_date": int(bars[-1][0]), "skipped": False, "reason": None}
