"""done 集与磁盘对账：续跑状态是否还在说真话。

回填的唯一状态是 `data/.state/history_backfill.json` 的 done 集，它自己不校验磁盘：
文件被删 / 表被迁移 / 审计后来发现源能给到更早的历史，done 都会原样留着。
"""
from __future__ import annotations

from pathlib import Path

from tdxfeed.paths import DAILY_BAK_SUFFIX


def list_daily_symbols(daily: str | Path) -> set[str]:
    d = Path(daily)
    if not d.is_dir():
        return set()
    return {p.stem for p in d.glob("*.parquet")
            if not p.name.endswith(DAILY_BAK_SUFFIX)}


def reconcile(done, files, audit_states: dict[str, str] | None = None) -> dict:
    """把 done 集、磁盘、最近一次深度审计三方对齐。

    - done_no_file：done 说补过，磁盘没有（删了/改名了的重复记录）
    - done_shallow：done 说补过，审计仍判 extend（补的深度不够就记成了完成）
    - pending_backfill：审计要补、done 没有（正常积压）
    - done_not_needed：done 有但审计不再要求补（无害，例如表已重建）
    """
    done = {str(s) for s in done}
    files = {str(s) for s in files}
    states = dict(audit_states or {})

    done_no_file = sorted(done - files)
    done_shallow = sorted(s for s in done & files if states.get(s) == "extend")
    if states:
        pending = sorted(s for s in files - done if states.get(s) == "extend")
        not_needed = sorted(s for s in done if s in states and states[s] != "extend")
    else:
        pending, not_needed = [], []

    anomalies = sorted(set(done_no_file) | set(done_shallow))
    return {
        "done": len(done),
        "files": len(files),
        "done_no_file": done_no_file,
        "file_no_done": sorted(files - done),
        "done_shallow": done_shallow,
        "pending_backfill": pending,
        "done_not_needed": not_needed,
        "anomalies": anomalies,
        "ok": not anomalies,
    }
