#!/usr/bin/env python3
"""
canary_automation.py · M3 W5 灰度自动化

基于 daily_fallback_report 日报自动调整 canary_ratio:
  - 当前成功率 ≥ 99% 且 p95 ≤ 10s → 爬坡 (+5%)
  - 当前成功率 < 95% 或 p95 > 30s → 回滚 (-10%)
  - 当前成功率 95~99% → 保持

可加 systemd timer 每小时检查。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPORTS = Path("/home/jiuben/tdx-data-feed/v5/reports")
STATE_FILE = Path("/home/jiuben/tdx-data-feed/v5/audit/canary_state.json")

INITIAL_RATIO = 0.05
MAX_RATIO = 1.0
MIN_RATIO = 0.0
STEP_UP = 0.05
STEP_DOWN = 0.10
SUCCESS_THRESHOLD = 0.99
ROLLBACK_SUCCESS = 0.95
P95_THRESHOLD = 15.0  # Ollama 推理 5-10s，给 5s buffer
P95_ROLLBACK = 30.0


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"ratio": 0.0, "history": [], "created": str(date.today())}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _parse_today_report() -> dict:
    """从 daily_fallback_report 日报提取指标"""
    md = REPORTS / f"fallback-{date.today().isoformat()}.md"
    if not md.exists():
        return {}
    text = md.read_text()
    # 解析: canary vs control 段
    out = {
        "canary_count": 0,
        "control_count": 0,
        "canary_success": 0,
        "control_success": 0,
        "canary_p50": 0.0,
        "p95": 0.0,
    }
    import re

    m = re.search(r"control\s*\|\s*(\d+)\s*\|\s*(\d+\.\d+)%\s*\|\s*(\d+\.\d+)s", text)
    if m:
        out["control_count"] = int(m.group(1))
        out["control_success"] = float(m.group(2))
        out["control_p50"] = float(m.group(3))
    m = re.search(r"canary\s*\|\s*(\d+)\s*\|\s*(\d+\.\d+)%\s*\|\s*(\d+\.\d+)s", text)
    if m:
        out["canary_count"] = int(m.group(1))
        out["canary_success"] = float(m.group(2))
        out["canary_p50"] = float(m.group(3))
    m = re.search(r"p95.*?(\d+\.\d+)s", text)
    if m:
        out["p95"] = float(m.group(1))
    return out


def decide_next_ratio(state: dict, metrics: dict) -> tuple[float, str]:
    """返回 (新 ratio, 决策原因)"""
    cur = state["ratio"]

    # 数据不足（canary 调用太少，无法决策）→ 用初始值开始
    if metrics.get("canary_count", 0) < 10 and cur == 0.0:
        return INITIAL_RATIO, "首次启动 → 初始 5%"

    # 回滚条件
    if metrics.get("canary_success", 100) < ROLLBACK_SUCCESS * 100:
        new = max(MIN_RATIO, cur - STEP_DOWN)
        return (
            new,
            f"成功率 {metrics.get('canary_success', 0):.1f}% < {ROLLBACK_SUCCESS * 100:.0f}% → 回滚",
        )
    if metrics.get("p95", 0) > P95_ROLLBACK:
        new = max(MIN_RATIO, cur - STEP_DOWN)
        return new, f"p95 {metrics.get('p95', 0):.1f}s > {P95_ROLLBACK}s → 回滚"

    # 爬坡条件
    if (
        metrics.get("canary_count", 0) >= 10
        and metrics.get("canary_success", 0) >= SUCCESS_THRESHOLD * 100
        and metrics.get("p95", 0) <= P95_THRESHOLD
    ):
            new = min(MAX_RATIO, cur + STEP_UP)
            return (
                new,
                f"成功率 {metrics.get('canary_success', 0):.1f}% ≥ {SUCCESS_THRESHOLD * 100:.0f}% → 爬坡",
            )

    return cur, "保持"


def main():
    state = _load_state()
    metrics = _parse_today_report()
    old_ratio = state["ratio"]
    new_ratio, reason = decide_next_ratio(state, metrics)

    state["ratio"] = new_ratio
    state["last_decision"] = {
        "ts": str(date.today()),
        "old_ratio": old_ratio,
        "new_ratio": new_ratio,
        "reason": reason,
        "metrics": metrics,
    }
    state["history"].append(state["last_decision"])
    state["history"] = state["history"][-30:]  # 只保留 30 天
    _save_state(state)

    # M3 W5: 决策追加到 fallback-{date}.md 报告末尾
    try:
        report = REPORTS / f"fallback-{date.today().isoformat()}.md"
        if report.exists():
            arrow = "↑" if new_ratio > old_ratio else ("↓" if new_ratio < old_ratio else "→")
            section = (
                f"\n## Canary 自动化决策 · {date.today()}\n\n"
                f"- 旧 ratio: {old_ratio * 100:.1f}%\n"
                f"- 新 ratio: {new_ratio * 100:.1f}% {arrow}\n"
                f"- 决策原因: {reason}\n"
                f"- canary 调用: {metrics.get('canary_count', 0)} (成功 {metrics.get('canary_success', 0):.1f}%)\n"
                f"- p95 延迟: {metrics.get('p95', 0):.1f}s\n"
                f"- 控制调用: {metrics.get('control_count', 0)} (成功 {metrics.get('control_success', 0):.1f}%)\n"
            )
            with report.open("a", encoding="utf-8") as f:
                f.write(section)
    except Exception as e:
        print(f"  warn: 写报告失败: {e}", file=sys.stderr)

    print("=== Canary Automation ===")
    print(f"  current ratio: {old_ratio * 100:.1f}%")
    print(f"  new ratio:    {new_ratio * 100:.1f}%")
    print(f"  reason:       {reason}")
    print(
        f"  metrics:      canary={metrics.get('canary_count', 0)} ({metrics.get('canary_success', 0):.1f}%) "
        f"p95={metrics.get('p95', 0):.1f}s"
    )
    print(f"  state file:   {STATE_FILE}")

    # 输出可被其他脚本读的 ratio
    print(f"\nCANARY_RATIO={new_ratio}")


if __name__ == "__main__":
    main()
