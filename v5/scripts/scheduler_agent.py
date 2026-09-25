#!/usr/bin/env python3
"""scheduler_agent.py · M5 Phase 1 · 智能 Agent 调度器

职责：
1. 根据时间窗口 + 资源使用 + 优先级决定哪个 Agent 跑
2. 调用 Agent 子进程并捕获 stdout/stderr
3. 写入 audit/scheduler.jsonl（每次调度决策）
4. 支持手动 override：scheduler_agent.py --run <agent_name>

5 Agent 调度策略（M5 Phase 1）：
┌─────────────────┬───────────────────────────────┬──────────────────┐
│ Agent            │ 触发条件                      │ 频率             │
├─────────────────┼───────────────────────────────┼──────────────────┤
│ HealthAgent      │ 持续监控（已有 tdx_monitor）  │ 每 5min          │
│ LinkingAgent     │ 周末 + 空闲                   │ 每周日 03:00     │
│ TagAgent         │ vault 有写入                  │ event-driven     │
│ MOCAgent         │ 链接完成后                    │ 每月 1 号 03:00  │
│ PromotionAgent   │ 笔记完成 7 天后                │ 每周一 03:00     │
└─────────────────┴───────────────────────────────┴──────────────────┘

用法：
  venv/bin/python v5/scripts/scheduler_agent.py --list
  venv/bin/python v5/scripts/scheduler_agent.py --run linking_agent
  venv/bin/python v5/scripts/scheduler_agent.py --once           # 单次评估（cron 模式）
  venv/bin/python v5/scripts/scheduler_agent.py --loop --interval 300  # 长驻循环（systemd simple 模式）

环境变量：
  V5_DIR: v5 根目录（默认 /home/jiuben/tdx-data-feed/v5）
  VAULT_PATH: Obsidian vault 目录（默认 /home/jiuben/StockVault）
  MATH_AUDIT_DIR: audit log 目录
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

V5_DIR = Path(os.environ.get("V5_DIR", "/home/jiuben/tdx-data-feed/v5"))
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/home/jiuben/StockVault"))
AUDIT_DIR = Path(os.environ.get("MATH_AUDIT_DIR", str(V5_DIR / "audit")))
SCHEDULER_LOG = AUDIT_DIR / "scheduler.jsonl"


# ============================================================================
# 5 Agent 定义（Phase 1：stub 实现，记录决策 + 调用真实 Python 工具）
# ============================================================================

AGENTS = {
    "health_agent": {
        "description": "持续监控 health（CPU/GPU/Disk/Services）",
        "schedule": "every_5min",
        "handler": "tdx_monitor.py",
        "estimated_duration_s": 5,
    },
    "linking_agent": {
        "description": "扫描 vault 找缺失 [[wikilink]] 并建议（M5 Phase 2 简化：只出建议不改 vault）",
        "schedule": "weekly_sun_0300",
        "handler": "suggestion_agent.py --agent linking_agent",
        "estimated_duration_s": 10,
    },
    "tag_agent": {
        "description": "扫描 vault 建议 #tag（M5 Phase 2 简化：基于关键词输出建议）",
        "schedule": "weekly_mon_0300",
        "handler": "suggestion_agent.py --agent tag_agent",
        "estimated_duration_s": 10,
    },
    "moc_agent": {
        "description": "MOC（Map of Content）按目录自动建议（M5 Phase 2 简化：每个目录 ≥2 笔记生成 MOC 建议）",
        "schedule": "monthly_1_0300",
        "handler": "suggestion_agent.py --agent moc_agent",
        "estimated_duration_s": 10,
        "min_vault_size": 10,  # 调整阈值（vault <10 笔记时跳过）
    },
    "promotion_agent": {
        "description": "笔记升级（inbox → 标的 → 框架 → 知识库，M5 Phase 2 简化：自动分类建议）",
        "schedule": "weekly_mon_0300",
        "handler": "suggestion_agent.py --agent promotion_agent",
        "estimated_duration_s": 5,
    },
}


# ============================================================================
# 资源使用检查（避免高峰时段跑批）
# ============================================================================


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _cpu_load_ok(threshold: float = 0.8) -> bool:
    """检查 CPU load5 是否 < 阈值（避免高峰期跑）"""
    try:
        with open("/proc/loadavg") as f:
            load5 = float(f.read().split()[1])
        # load5 < cores * threshold
        nproc = os.cpu_count() or 4
        return load5 < nproc * threshold
    except Exception:
        return True  # 检测失败时允许


def _disk_free_gb(path: Path = V5_DIR, min_gb: float = 1.0) -> bool:
    """检查磁盘剩余空间"""
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024**3)
        return free_gb > min_gb
    except Exception:
        return True


def _vault_size() -> int:
    """vault .md 文件数"""
    try:
        return len(list(VAULT_PATH.rglob("*.md")))
    except Exception:
        return 0


# ============================================================================
# 调度决策（基于时间 + 资源 + vault 大小）
# ============================================================================


def _should_run(agent_name: str, now: datetime) -> tuple[bool, str]:
    """判断是否该跑 agent，返回 (should_run, reason)"""
    cfg = AGENTS.get(agent_name)
    if not cfg:
        return False, f"unknown agent: {agent_name}"

    # 资源检查
    if not _cpu_load_ok():
        return False, "cpu_load_high"
    if not _disk_free_gb():
        return False, "disk_low"

    schedule = cfg["schedule"]
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    hour = now.hour
    day = now.day

    # 调度策略匹配
    if schedule == "every_5min":
        return True, "always"
    elif schedule == "weekly_sun_0300":
        if weekday == 6 and hour == 3:
            return True, "weekly_sun_0300"
        return False, "not_window"
    elif schedule == "weekly_mon_0300":
        if weekday == 0 and hour == 3:
            return True, "weekly_mon_0300"
        return False, "not_window"
    elif schedule == "monthly_1_0300":
        if day == 1 and hour == 3:
            # 检查 vault 大小
            min_size = cfg.get("min_vault_size", 0)
            if _vault_size() < min_size:
                return False, f"vault_too_small({_vault_size()}<{min_size})"
            return True, "monthly_1_0300"
        return False, "not_window"
    elif schedule == "event_driven":
        # 暂用手动触发
        return False, "manual_only"
    return False, "no_schedule_match"


def _audit_decision(
    agent_name: str,
    decision: str,
    reason: str,
    duration_s: float = 0,
    ok: bool = True,
    output: str = "",
):
    """写入 scheduler.jsonl"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _utc_now().isoformat().replace("+00:00", "Z"),
        "agent": agent_name,
        "decision": decision,  # "run" | "skip" | "manual_override"
        "reason": reason,
        "duration_s": round(duration_s, 3),
        "ok": ok,
        "output_excerpt": output[:500] if output else "",
    }
    with SCHEDULER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================================
# Agent 执行
# ============================================================================


def _run_agent(agent_name: str, force: bool = False) -> dict:
    """执行 agent 子进程，返回结果"""
    cfg = AGENTS.get(agent_name)
    if not cfg:
        return {"ok": False, "error": f"unknown agent: {agent_name}"}

    t0 = time.time()
    try:
        # 解析 handler：scan_obsidian_vault.py --xxx
        parts = cfg["handler"].split()
        script_name = parts[0]
        script_args = parts[1:]
        script_path = V5_DIR / "scripts" / script_name
        if not script_path.exists():
            elapsed = time.time() - t0
            _audit_decision(
                agent_name,
                "manual_override" if force else "skip",
                "script_not_found",
                elapsed,
                ok=False,
            )
            return {"ok": False, "error": f"script not found: {script_path}"}

        cmd = (
            [str(V5_DIR.parent / "venv" / "bin" / "python"), str(script_path), *script_args, "--vault", str(VAULT_PATH)]
        )
        # 对部分 agent，scan_obsidian_vault.py 可能还没实现对应 flag，仅打印 stub
        # 真实实现时 scan_obsidian_vault.py 会支持
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=cfg.get("estimated_duration_s", 60) * 2
        )
        elapsed = time.time() - t0
        ok = proc.returncode == 0
        _audit_decision(
            agent_name,
            "manual_override" if force else "run",
            "executed",
            elapsed,
            ok,
            proc.stdout + proc.stderr,
        )
        return {
            "ok": ok,
            "returncode": proc.returncode,
            "duration_s": round(elapsed, 3),
            "stdout": proc.stdout[:1000],
            "stderr": proc.stderr[:1000],
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        _audit_decision(
            agent_name, "manual_override" if force else "skip", "timeout", elapsed, ok=False
        )
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        elapsed = time.time() - t0
        _audit_decision(
            agent_name, "manual_override" if force else "skip", f"exception: {e}", elapsed, ok=False
        )
        return {"ok": False, "error": str(e)}


# ============================================================================
# 评估循环（每次决策）
# ============================================================================


def evaluate_once(now: datetime | None = None) -> dict:
    """单次评估：根据当前时间 + 资源，决定跑哪些 agent"""
    if now is None:
        now = _utc_now()
    results = {}
    for agent_name in AGENTS:
        should_run, reason = _should_run(agent_name, now)
        results[agent_name] = {"should_run": should_run, "reason": reason}
        if should_run:
            results[agent_name]["result"] = _run_agent(agent_name)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 scheduler_agent.py")
    parser.add_argument("--list", action="store_true", help="列出所有 agent")
    parser.add_argument("--run", metavar="AGENT", help="手动运行指定 agent")
    parser.add_argument("--once", action="store_true", help="单次评估（cron / timer 模式）")
    parser.add_argument("--loop", action="store_true", help="长驻循环（systemd simple 模式）")
    parser.add_argument(
        "--interval", type=int, default=300, help="长驻循环间隔秒数（默认 300=5min）"
    )
    args = parser.parse_args()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    if args.list:
        print("=" * 70)
        print(f"{'Agent':<20} {'Schedule':<22} {'Description':<40}")
        print("=" * 70)
        for name, cfg in AGENTS.items():
            print(f"{name:<20} {cfg['schedule']:<22} {cfg['description']:<40}")
        return 0

    if args.run:
        agent = args.run
        if agent not in AGENTS:
            print(f"❌ 未知 agent: {agent}", file=sys.stderr)
            print(f"   可用: {', '.join(AGENTS.keys())}", file=sys.stderr)
            return 1
        print(f"▶ 手动运行 {agent}...")
        result = _run_agent(agent, force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.loop:
        # 长驻循环模式（systemd simple）
        print(f"[scheduler_agent] loop mode, interval={args.interval}s", file=sys.stderr)
        import signal as _signal

        _stop = False

        def _handle_sig(*_):
            nonlocal _stop
            _stop = True

        _signal.signal(_signal.SIGTERM, _handle_sig)
        _signal.signal(_signal.SIGINT, _handle_sig)
        cycle = 0
        while not _stop:
            cycle += 1
            print(f"[scheduler_agent] cycle {cycle} evaluating...", file=sys.stderr)
            evaluate_once()
            for _ in range(args.interval):
                if _stop:
                    break
                time.sleep(1)
        return 0

    # 默认：--once 模式（cron/timer 触发）
    results = evaluate_once()
    # 仅输出执行的 agent（不输出 skip）
    executed = {k: v for k, v in results.items() if v.get("should_run") and "result" in v}
    print(
        json.dumps(
            {
                "evaluated_at": _utc_now().isoformat(),
                "total_agents": len(AGENTS),
                "executed_count": len(executed),
                "executed": executed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
