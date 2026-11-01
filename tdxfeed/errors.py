"""吞错计数的统一汇总出口（SEM002 的运营闭环）。

背景
----
M-4 起，仓库用「每模块一个 `_ERROR_COUNT: Counter[str]`」记录被吞掉的错误：
SEM002 守卫要求每个 `except` 必须 `raise` 或**计入统计**。收敛 349 处后，
失败从"完全不可见"变成了"代码里可见、可 grep、可统计"——但计数是**模块级私有变量**，
没有任何读取方，跨进程也看不到（进程退出即丢失）。

本模块补上最后一段链路，且**零侵入**：
不改动那 141 个已改造文件，直接扫描当前进程内已加载模块的 `_ERROR_COUNT`，
因此不会引入任何跨包导入风险（`scripts/` 下部分脚本没把仓库根放进 `sys.path`）。

跨进程闭环
----------
进程内的计数随进程退出而消失，故提供 `flush()`：在**生产入口**用 `atexit` 登记，
把本进程的计数追加进持久化文件；`read_recent()` 再把最近一段时间的记录聚合回来。
canary 的 `swallowed_errors` 维度读的就是这个文件。

用法
----
    from tdxfeed.errors import flush, read_recent, snapshot, summary

    snapshot()                 # {'tdxfeed.full_sync': {'get_all_codes': 3}, ...}
    summary()                  # {'total': 12, 'modules': 4, 'top_modules': [...]}
    flush()                    # 落盘本进程计数（生产入口用 atexit 登记）
    read_recent(hours=24)      # 聚合最近 24 小时的落盘记录
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
COUNTER_ATTR = "_ERROR_COUNT"

# 本模块自身的吞错计数（SEM002：读取损坏记录也要计入统计）
_ERROR_COUNT: Counter[str] = Counter()
STATE_PATH = ROOT / "data" / "quality" / "swallowed_counts.jsonl"


def _module_key(name: str, mod: Any) -> str:
    """模块归属名。

    脚本以 `python scripts/xxx.py` 运行时模块名是 `__main__`（无法定位来源），
    这时改用脚本相对路径，让计数能落到具体文件上。
    """
    if name != "__main__":
        return name
    file = getattr(mod, "__file__", None)
    if not file:
        return name
    try:
        return str(Path(file).resolve().relative_to(ROOT))
    except ValueError:
        _ERROR_COUNT["_module_key"] += 1
        return str(file)


def _iter_counters() -> list[tuple[str, Counter[str]]]:
    """遍历当前进程内已加载模块的吞错计数器。"""
    out: list[tuple[str, Counter[str]]] = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        counter = getattr(mod, COUNTER_ATTR, None)
        if isinstance(counter, Counter):
            out.append((_module_key(name, mod), counter))
    return out


def snapshot() -> dict[str, dict[str, int]]:
    """全量快照：模块名 → {函数名: 次数}（只含非零模块）。"""
    return {name: dict(counter) for name, counter in _iter_counters() if counter}


def totals() -> dict[str, int]:
    """按模块汇总的总次数。"""
    return {name: sum(counts.values()) for name, counts in snapshot().items()}


def summary(top: int = 10) -> dict[str, Any]:
    """汇总视图：总数、模块数、最集中的模块与函数。"""
    snap = snapshot()
    by_func: Counter[str] = Counter()
    for counts in snap.values():
        by_func.update(counts)
    return {
        "total": sum(sum(v.values()) for v in snap.values()),
        "modules": len(snap),
        "top_modules": sorted(totals().items(), key=lambda kv: -kv[1])[:top],
        "top_functions": by_func.most_common(top),
    }


def reset() -> None:
    """清零当前进程内所有已登记计数器（测试用）。"""
    for _name, counter in _iter_counters():
        counter.clear()


def flush(path: Path | None = None) -> dict[str, Any]:
    """把本进程的计数追加到持久化文件（生产入口用 atexit 登记）。

    只记录**非零**模块，避免每次退出都写一行空记录。
    """
    target = Path(path) if path else STATE_PATH
    snap = snapshot()
    if not snap:
        return {"written": False, "reason": "no_counts"}
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pid": os.getpid(),
        "counts": snap,
        "total": sum(sum(v.values()) for v in snap.values()),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"written": True, "path": str(target), "total": record["total"],
            "modules": len(snap)}


def read_recent(hours: int = 24, path: Path | None = None) -> dict[str, Any]:
    """聚合最近 `hours` 小时的落盘记录。

    返回：{'total': n, 'modules': {...}, 'top_functions': [...], 'records': k}
    """
    target = Path(path) if path else STATE_PATH
    if not target.exists():
        return {"total": 0, "modules": {}, "top_functions": [], "records": 0,
                "exists": False}
    cutoff = time.time() - hours * 3600
    by_module: Counter[str] = Counter()
    by_func: Counter[str] = Counter()
    records = 0
    with open(target, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                _ERROR_COUNT["read_recent"] += 1
                continue
            ts = str(rec.get("ts", ""))
            try:
                epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                _ERROR_COUNT["read_recent"] += 1
                epoch = cutoff + 1
            if epoch < cutoff:
                continue
            records += 1
            for mod, counts in (rec.get("counts") or {}).items():
                by_module[mod] += sum(counts.values())
                for func, n in counts.items():
                    by_func[f"{mod}:{func}"] += n
    return {
        "total": sum(by_module.values()),
        "modules": dict(by_module.most_common()),
        "top_functions": by_func.most_common(10),
        "records": records,
        "exists": True,
    }
