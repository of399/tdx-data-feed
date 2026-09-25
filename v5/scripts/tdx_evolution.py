#!/usr/bin/env python3
"""tdx_evolution.py · M5 Phase 1 · vault 自演化 supervisor

职责（M5 升级）：
1. 每 5 分钟（默认）调 scheduler_agent.py --once 评估调度决策
2. 写 audit/evolution.jsonl（event: heartbeat / scheduled_agent_run）
3. 维护自身 heartbeat（systemd watchdog 检测存活）
4. 处理 SIGTERM 优雅退出

升级历史：
- M4: stub（仅 heartbeat）
- M5 Phase 1: 调 scheduler_agent --once
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

V5_DIR = Path("/home/jiuben/tdx-data-feed/v5")
AUDIT_LOG = V5_DIR / "audit" / "evolution.jsonl"
SCHEDULER_SCRIPT = V5_DIR / "scripts" / "scheduler_agent.py"
PYTHON_BIN = V5_DIR.parent / "venv" / "bin" / "python"

CYCLE_INTERVAL_S = int(os.environ.get("EVOLUTION_INTERVAL_S", "300"))  # 5 分钟

_stop = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def handle_sigterm(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def _audit(event: str, **kwargs):
    """写入 audit log"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _utc_now().isoformat(),
        "event": event,
        **kwargs,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _call_scheduler() -> dict:
    """调用 scheduler_agent.py --once，返回结果"""
    try:
        proc = subprocess.run(
            [str(PYTHON_BIN), str(SCHEDULER_SCRIPT), "--once"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_excerpt": proc.stdout[:500],
            "stderr_excerpt": proc.stderr[:500],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "scheduler_timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    print(f"[tdx-evolution] M5 Phase 1 starting at {_utc_now().isoformat()}", file=sys.stderr)
    print(f"  cycle_interval: {CYCLE_INTERVAL_S}s", file=sys.stderr)
    print(f"  scheduler: {SCHEDULER_SCRIPT}", file=sys.stderr)

    _audit("startup", cycle_interval_s=CYCLE_INTERVAL_S)

    cycle = 0
    while not _stop:
        cycle += 1
        t0 = time.time()

        # 调 scheduler_agent --once
        scheduler_result = _call_scheduler()
        elapsed = round(time.time() - t0, 3)

        # 写 audit log（heartbeat + 调度结果）
        _audit(
            "cycle",
            cycle=cycle,
            scheduler_ok=scheduler_result.get("ok"),
            scheduler_returncode=scheduler_result.get("returncode"),
            elapsed_s=elapsed,
        )

        # sleep（可中断）
        for _ in range(CYCLE_INTERVAL_S):
            if _stop:
                break
            time.sleep(1)

    _audit("shutdown")
    print(f"[tdx-evolution] stopping at {_utc_now().isoformat()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
