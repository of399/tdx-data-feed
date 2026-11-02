#!/usr/bin/env python3
"""同步完成哨兵校验（KI-COMPETE-001 的修复点之二）。

作为 `systemd/predict-daily.service` 的**第二条** ExecStartPre 运行（在 `precheck_freshness.sh`
之后）。`precheck_freshness.sh` 只挡"整体陈旧"，本脚本挡"部分标的正在被写"的并发竞态：
18:05 的 predict 启动时，系统级 tdx-sync（16:30 起、最长 6h）可能还没跑完。

双通道判定（任一满足即放行）：
  A. `data/.state/sync-done` 存在且 mtime 在 `--max-age-hours` 内；
  B. 系统级 `tdx-sync.service` 的 Result=success 且成功退出时间在窗口内
     （首次上线兜底：哨兵文件尚未生成时，只要单元近期确已成功过就不误杀出榜）。

两者都不满足 => exit 2（与 `precheck_freshness.sh` 一致的语义：数据未就绪，不出榜）。
`--dry-run` 只打印判定、不 exit 非零，用于上线首日只读观察。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = ROOT / "data" / ".state" / "sync-done"
SYNC_UNIT = "tdx-sync.service"


def _sentinel_fresh(max_age_hours: float) -> bool:
    if not SENTINEL.exists():
        return False
    age_h = (datetime.now().timestamp() - SENTINEL.stat().st_mtime) / 3600
    return age_h <= max_age_hours


def _unit_success_recent(max_age_hours: float) -> bool:
    """通道 B：系统级单元近期确已成功过（兜底首次上线哨兵文件缺失）。"""
    try:
        out = subprocess.run(
            ["systemctl", "show", SYNC_UNIT, "-p", "Result", "-p", "ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return False
    info: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    if info.get("Result") != "success":
        return False
    ts = info.get("ExecMainExitTimestamp", "")
    if not ts:
        return False
    try:
        exited = datetime.fromisoformat(ts)
    except Exception:
        return False
    age_h = (datetime.now() - exited).total_seconds() / 3600
    return age_h <= max_age_hours


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步完成哨兵校验（predict 出榜前门禁）")
    ap.add_argument("--max-age-hours", type=float, default=12,
                    help="哨兵文件 / 单元最近成功的最长允许年龄（小时）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印判定，不 exit 非零（上线首日只读观察用）")
    args = ap.parse_args(argv)

    if _sentinel_fresh(args.max_age_hours):
        print(f"[sentinel] 放行：sync-done 新鲜（窗口 {args.max_age_hours}h）")
        return 0
    if _unit_success_recent(args.max_age_hours):
        print(f"[sentinel] 放行：{SYNC_UNIT} Result=success（哨兵文件尚缺，兜底通道）")
        return 0

    print(
        "[sentinel] 拦截：本轮全市场同步未完成（sync-done 缺失/过期 且 "
        f"{SYNC_UNIT} 未确认 success）=> 不出榜",
        file=sys.stderr,
    )
    return 0 if args.dry_run else 2


if __name__ == "__main__":
    sys.exit(main())
