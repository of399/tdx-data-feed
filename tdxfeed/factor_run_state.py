"""因子任务（adj / hfq）的进度心跳落盘。

治的是 KI-OPS-004 的后半段：`ExecStartPost=-` 把后置任务的失败吞成 `Finished`，
而 `TimeoutStartSec` 截断整轮时，**后半段一个完成标记都不留**——日志里只有最后
一次 `[N/总数]` 的进度行，事后没人能回答"还剩多少只没跑"。这里每 200 只落一次
状态，整轮结束（含中止）再落一次，于是三种形态都能区分：

| 现场 | 心跳里看到 |
|---|---|
| 正常跑完 | `finished=true`、`unfinished=0` |
| 被超时截断 | `finished=false`、`unfinished>0`、`updated_at` 停在截断时刻 |
| 登录彻底失败 | `finished=false`、`processed=0`、`unfinished=target`（心跳先落再登录） |
| 连续失败中止 | `finished=true`、`aborted=true`、`fail` 有数 |

写的是 `data/.state/factor_run.json`（`data/` 不在 git），两张表共用一份文件、
各占一个键，读改写只覆盖自己那一张，别把对方的心跳冲掉。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from tdxfeed import paths
from tdxfeed.atomic import atomic_write_text

# 吞错计数（SEM002）：心跳读不出/写不进都不能中断主任务，但必须计入统计。
_ERROR_COUNT: Counter[str] = Counter()

# 节流：与主循环的进度打印同频（每 200 只一次），全市场一轮约 26 次小文件写。
HEARTBEAT_EVERY = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read(path: Path) -> dict[str, Any]:
    """读现有状态；读不出就当空表（心跳不能反噬主任务）。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # SEM002：坏文件降级成空，但要计数
        _ERROR_COUNT["_read"] += 1
        print(f"⚠️ factor_run.json 读不出，按空处理: {type(e).__name__}: {e}", flush=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def write_progress(table: str, *, today: str, started_at: str, target: int,
                   processed: int, ok: int, skip: int, fail: int,
                   rebase: int = 0, added_rows: int = 0,
                   finished: bool = False, aborted: bool = False,
                   path: Path | None = None) -> dict[str, Any]:
    """落一次心跳，返回写入的那条记录（调用方可直接打印，测试可直接断言）。"""
    p = Path(path) if path is not None else paths.factor_run_path()
    rec = {
        "table": table,
        "today": today,
        "started_at": started_at,
        "updated_at": _now(),
        "target": int(target),
        "processed": int(processed),
        "ok": int(ok),
        "skip": int(skip),
        "fail": int(fail),
        "rebase": int(rebase),
        "added_rows": int(added_rows),
        # 「还剩多少只」的唯一算式：目标减去已处理（skip 也算处理过）。
        "unfinished": max(int(target) - int(processed), 0),
        "finished": bool(finished),
        "aborted": bool(aborted),
    }
    disk = _read(p)
    disk[table] = rec
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(disk, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n")
    except Exception as e:  # SEM002：写不进去不能拖垮整轮，但要计数并喊出来
        _ERROR_COUNT["write_progress"] += 1
        print(f"⚠️ factor_run.json 写入失败: {type(e).__name__}: {e}", file=sys.stderr)
    return rec


def read_progress(table: str, *, path: Path | None = None) -> dict[str, Any]:
    """读某张表的最近一次心跳；没有就返回空 dict。"""
    p = Path(path) if path is not None else paths.factor_run_path()
    rec = _read(p).get(table)
    return rec if isinstance(rec, dict) else {}
