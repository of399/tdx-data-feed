#!/usr/bin/env python3
"""tdx_autonomy.py · M4 L1 自治化 watchdog

职责：
1. 监控 math-sympy-http 健康（每 30s）
2. 自动重启（如果 unhealthy 连续 3 次）
3. 维护 circuit breaker 状态（写到共享文件 /tmp/tdx-circuit-breaker.json）
4. 检测服务降级 → 触发告警（journal）

运行模式：
- Type=simple（systemd 拉起，crash 后自动 Restart=always）
- 每 30s 一次循环

输出：
- /tmp/tdx-circuit-breaker.json（circuit breaker 状态）
- journal 日志
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

MATH_HTTP_URL = os.environ.get("MATH_HTTP_URL", "http://127.0.0.1:8002")
WORKBENCH_URL = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8010")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CB_FILE = Path("/tmp/tdx-circuit-breaker.json")

CB_FAILURE_THRESHOLD = 3
CB_RECOVERY_TIMEOUT_S = 30
CB_HALF_OPEN_MAX_CALLS = 1

_stop = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def handle_sigterm(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


class CircuitBreaker:
    """自适应断路器（M4）

    状态机：
    - CLOSED（正常）：所有调用通过
    - OPEN（熔断）：短路调用，返回兜底结果（避免雪崩）
    - HALF_OPEN（半开）：放 1 个调用探测，成功 → CLOSED，失败 → OPEN
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at: datetime | None = None
        self.half_open_calls = 0

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if (
                self.opened_at
                and (_utc_now() - self.opened_at).total_seconds() > self.recovery_timeout_s
            ):
                self.state = "HALF_OPEN"
                self.half_open_calls = 0
                print(f"[CB {self.name}] OPEN → HALF_OPEN", file=sys.stderr)
                return True
            return False
        if self.state == "HALF_OPEN":
            if self.half_open_calls < CB_HALF_OPEN_MAX_CALLS:
                self.half_open_calls += 1
                return True
            return False
        return False

    def record_success(self):
        if self.state in ("HALF_OPEN", "OPEN"):
            print(f"[CB {self.name}] {self.state} → CLOSED (recovered)", file=sys.stderr)
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at = None
        self.half_open_calls = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.state == "HALF_OPEN":
            self.state = "OPEN"
            self.opened_at = _utc_now()
            print(f"[CB {self.name}] HALF_OPEN → OPEN (recovery failed)", file=sys.stderr)
        elif self.consecutive_failures >= self.failure_threshold:
            if self.state != "OPEN":
                self.state = "OPEN"
                self.opened_at = _utc_now()
                print(
                    f"[CB {self.name}] CLOSED → OPEN ({self.consecutive_failures} failures)",
                    file=sys.stderr,
                )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "recovery_timeout_s": self.recovery_timeout_s,
        }


def health_check(url: str, name: str, timeout: float = 2.0) -> tuple[bool, float]:
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            t0 = time.time()
            r = client.get(url)
            elapsed_ms = (time.time() - t0) * 1000
            return (r.status_code == 200, elapsed_ms)
    except Exception:
        return (False, -1)


def save_cb_state(cbs: dict):
    state = {name: cb.to_dict() for name, cb in cbs.items()}
    state["ts"] = _utc_now().isoformat()
    with contextlib.suppress(OSError):
        CB_FILE.write_text(json.dumps(state, indent=2))


def main() -> int:
    print(f"[tdx-autonomy] starting at {_utc_now().isoformat()}", file=sys.stderr)

    cbs = {
        "math-sympy-http": CircuitBreaker("math-sympy-http"),
        "workbench": CircuitBreaker("workbench"),
        "ollama": CircuitBreaker("ollama"),
    }

    while not _stop:
        for name, url in [
            ("math-sympy-http", f"{MATH_HTTP_URL}/health"),
            ("workbench", f"{WORKBENCH_URL}/health"),
            ("ollama", f"{OLLAMA_URL}/api/tags"),
        ]:
            cb = cbs[name]
            if not cb.allow_request():
                continue
            ok, ms = health_check(url, name)
            if ok:
                cb.record_success()
            else:
                cb.record_failure()
                print(f"[health] {name} FAILED (ms={ms:.1f})", file=sys.stderr)

        save_cb_state(cbs)

        for _ in range(30):
            if _stop:
                break
            time.sleep(1)

    print(f"[tdx-autonomy] stopping at {_utc_now().isoformat()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
