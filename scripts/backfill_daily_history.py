#!/usr/bin/env python
"""按审计产物（任务 3 的 extend 清单）回填历史日线：幂等、可 Ctrl-C 续跑。

    venv/bin/python scripts/backfill_daily_history.py --dry-run          # 只看清单
    venv/bin/python scripts/backfill_daily_history.py --limit 5          # 先 5 只
    venv/bin/python scripts/backfill_daily_history.py --workers 3        # 全量

退出码 1 的两种情形：本轮一只都没补成（total=0），或失败占比超 BAD_RATIO_MAX。
理由与 full_sync.sync_exit_code 同源——「零成功」不能报成完美，否则下游只看退出码
就会把通道故障当成「今天不用补」。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.backfill_history import (  # noqa: E402
    RESUME_PATH, backfill_symbol, load_done, mark_done)
from tdxfeed.writers import Writers  # noqa: E402

BAD_RATIO_MAX = 0.05
TARGET_DEFAULT = 20000101
# 连续这么多只失败就判通道故障并熔断（逐只的数据问题不会连成一片）。
TRIP_FAIL_STREAK = 10


def latest_audit() -> Path:
    fs = sorted(glob.glob(str(ROOT / "data" / "audit" / "history_depth_*.json")))
    if not fs:
        raise SystemExit("先跑 scripts/audit_daily_history_depth.py（任务 3）："
                         "回填范围来自它的 extend 清单")
    return Path(fs[-1])


def plan(audit: dict, done: set[str], limit: int = 0) -> tuple[list[str], list[str]]:
    """返回 (审计里的 extend 全清单, 本轮待补=extend∖已补，再截 limit)。"""
    extend = [r["symbol"] for r in audit["rows"] if r["state"] == "extend"]
    todo = [s for s in extend if s not in done]
    if limit:
        todo = todo[:limit]
    return extend, todo


def _run(sym: str, target: int, w: Writers) -> dict:
    try:
        return backfill_symbol(sym, target, writers=w)
    except Exception as e:  # 逐只隔离：一只坏不影响整轮，但计入 fail 与退出码
        return {"symbol": sym, "error": f"{type(e).__name__}:{str(e)[:140]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT)
    ap.add_argument("--resume", default=RESUME_PATH)
    args = ap.parse_args()

    audit_path = latest_audit()
    with open(audit_path, encoding="utf-8") as fh:
        audit = json.load(fh)
    if audit.get("target") != args.target:
        print(f"[WARN] 审计 target={audit.get('target')} ≠ 本次 --target={args.target}，"
              "回填范围与审计口径不一致", flush=True)
    done = load_done(args.resume)
    extend, todo = plan(audit, done, args.limit)
    print(f"审计 {audit_path.name}：extend {len(extend)} 只，"
          f"已补 {len(extend) - len([s for s in extend if s not in done])} 只，"
          f"本轮待补 {len(todo)} 只", flush=True)
    if not todo:
        print("无需回填（extend 已全部补过，或审计里没有 extend）", flush=True)
        return 0
    if args.dry_run:
        print(f"dry-run：不写盘。前 10 只 {todo[:10]}", flush=True)
        return 0

    w = Writers(str(ROOT / "data"))
    ok = fail = added = 0
    tripped = False
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_run, s, args.target, w): s for s in todo}
        streak = 0
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("error"):
                fail += 1
                streak += 1
                print(f"FAIL {r['symbol']}: {r['error']}", flush=True)
                if streak >= TRIP_FAIL_STREAK:
                    # 连续整片失败 = 通道级故障（IP 被风控时每只要磨 2 域×3 试≈分钟级），
                    # 继续排队就是拿剩下几千只砸同一个被封的地址：撤未启动的任务并退出。
                    pending = sum(1 for f in futs if f.cancel())
                    tripped = True
                    print(f"ABORT 连续 {streak} 只失败，撤掉 {pending} 个未启动任务；"
                          "先确认通道（scripts/audit_daily_history_depth.py --limit 20）再续跑",
                          flush=True)
                    break
            else:
                ok += 1
                streak = 0
                added += r["rows_added"]
                # 主线程写 done 集：mark_done 是读-改-写，worker 线程并发会丢条目
                mark_done(args.resume, r["symbol"])
                print(f"OK {r['symbol']} +{r['rows_added']} 行 "
                      f"（共 {r['total_rows']}，最早 {r['first_date']}）", flush=True)
    total = ok + fail
    print(f"完成 成功{ok} 失败{fail} 新增行数{added} 续跑文件 {args.resume}", flush=True)
    if tripped or total == 0:
        return 1
    return 1 if fail / total > BAD_RATIO_MAX else 0


if __name__ == "__main__":
    sys.exit(main())
