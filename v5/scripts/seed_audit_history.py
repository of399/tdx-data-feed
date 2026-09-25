#!/usr/bin/env python3
"""seed_audit_history.py · M3 W6 模拟生成 24h 历史 audit log

让 24h 趋势图立即可见，无需真实等待 24 小时。

生成规则：
- 24 个小时 buckets，每小时 5~20 次调用（业务高峰时段更多）
- 策略分布：sympy 70%, ask 10%, numeric 15%, llm 5%（control）
- canary_ratio: 0.1（10% 流量走 canary 强制 llm）
- 失败率：~3%
- 时长：
    sympy/ask: 0.01~0.05s
    numeric:   0.5~3s
    llm:       8~15s

用法：
    venv/bin/python v5/scripts/seed_audit_history.py [--hours 24]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
CHAIN_LOG = DEFAULT_AUDIT_DIR / "fallback-chain.jsonl"
CANARY_LOG = DEFAULT_AUDIT_DIR / "fallback-canary.jsonl"

# 时段权重（业务高峰 9-12 / 14-18 / 20-22）
HOUR_WEIGHTS = [
    0.5,
    0.3,
    0.2,
    0.2,
    0.2,
    0.3,  # 00-05 深夜低谷
    0.5,
    0.8,
    1.2,
    1.5,
    1.4,
    1.3,  # 06-11 早高峰
    1.0,
    0.9,
    1.4,
    1.6,
    1.5,
    1.2,  # 12-17 下午高峰
    0.8,
    1.0,
    1.1,
    0.9,
    0.7,
    0.5,  # 18-23 晚高峰回落
]

# 示例 LaTeX 输入（模拟真实使用）
SAMPLE_LATEX = [
    ("x + 1", "simplify"),
    ("\\sin(x)", "simplify"),
    ("\\int x^2 dx", "integrate"),
    ("\\frac{d}{dx}(x^2)", "differentiate"),
    ("x^2 - 4", "solve"),
    ("(x+1)^2", "expand"),
    ("\\sqrt{x^2}", "simplify"),
    ("\\log(x*y)", "simplify"),
    ("\\sum_{n=1}^{10} n", "evaluate"),
    ("\\lim_{x \\to 0} \\frac{\\sin(x)}{x}", "limit"),
]

LATEXES = SAMPLE_LATEX * 5  # 50 个候选

# 策略概率（control 组）
STRATEGY_PROBS_CONTROL = {
    "sympy": 0.70,
    "ask": 0.10,
    "numeric": 0.15,
    "llm": 0.05,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _mock_duration(strategy: str, rng: random.Random) -> float:
    """根据策略生成合理耗时"""
    if strategy == "sympy":
        return round(rng.uniform(0.005, 0.05), 4)
    elif strategy == "ask":
        return round(rng.uniform(0.01, 0.08), 4)
    elif strategy == "numeric":
        return round(rng.uniform(0.5, 3.0), 3)
    elif strategy == "llm":
        return round(rng.uniform(8.0, 15.0), 3)
    return 0.01


def _pick_strategy(rng: random.Random, force: str | None = None) -> str:
    if force:
        return force
    r = rng.random()
    cum = 0.0
    for s, p in STRATEGY_PROBS_CONTROL.items():
        cum += p
        if r < cum:
            return s
    return "sympy"


def _make_entry(
    rng: random.Random,
    ts: datetime,
    latex: str,
    action: str,
    is_canary: bool,
    force: str | None = None,
) -> dict:
    strategy = _pick_strategy(rng, force)
    duration = _mock_duration(strategy, rng)

    # 失败：~3% 概率（随机）
    ok = rng.random() > 0.03

    # 计算 attempts 链
    chain = []
    if is_canary and force:
        chain.append(
            {"strategy": force, "ok": ok, "elapsed_s": duration, "error": None if ok else "timeout"}
        )
    elif strategy == "sympy":
        chain.append(
            {
                "strategy": "sympy",
                "ok": ok,
                "elapsed_s": duration,
                "error": None if ok else "timeout",
            }
        )
    elif strategy == "ask":
        chain.append(
            {
                "strategy": "ask",
                "ok": ok,
                "elapsed_s": duration,
                "error": None if ok else "no ask applicable",
            }
        )
    elif strategy == "numeric":
        chain.append(
            {
                "strategy": "numeric",
                "ok": ok,
                "elapsed_s": duration,
                "error": None if ok else "no roots found",
            }
        )
    elif strategy == "llm":
        chain.append(
            {
                "strategy": "llm",
                "ok": ok,
                "elapsed_s": duration,
                "error": None if ok else "ollama timeout",
            }
        )

    key_hash = hashlib.md5(f"{latex}|{action}".encode()).hexdigest()[:8]
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "tool": "math_strategy_route_and_execute",
        "latex": latex,
        "action": action,
        "var": "x",
        "key_hash": key_hash,
        "group": "canary" if is_canary else "control",
        "canary_ratio": 0.1,
        "canary_target": "llm" if is_canary else None,
        "primary": strategy,
        "chain": [strategy],
        "attempts": chain,
        "success_strategy": strategy if ok else None,
        "total_elapsed_s": duration,
        "ok": ok,
    }


def seed(hours: int, seed_val: int, dry_run: bool):
    rng = random.Random(seed_val)
    now = _utc_now()
    chain_entries = []
    canary_entries = []

    # 每个小时 bucket 决定调用次数
    for h in range(hours, 0, -1):
        ts_hour_start = now - timedelta(hours=h)
        hour_of_day = ts_hour_start.hour
        weight = HOUR_WEIGHTS[hour_of_day]
        calls_this_hour = max(1, int(rng.uniform(3, 10) * weight))

        for _ in range(calls_this_hour):
            # 时间点 = hour_start + 随机分钟
            offset_min = rng.uniform(0, 60)
            ts = ts_hour_start + timedelta(minutes=offset_min)
            latex, action = rng.choice(LATEXES)

            # 10% canary 流量
            is_canary = rng.random() < 0.1
            force = "llm" if is_canary else None

            entry = _make_entry(rng, ts, latex, action, is_canary, force)
            chain_entries.append(entry)
            if is_canary:
                canary_entries.append(entry)

    print(f"🧪 模拟生成 {hours}h 历史数据")
    print(f"  chain_entries: {len(chain_entries)} 条")
    print(
        f"  canary_entries: {len(canary_entries)} 条 (canary 占比 {len(canary_entries) / max(1, len(chain_entries)) * 100:.1f}%)"
    )
    print(f"  时间范围: {chain_entries[0]['ts']} → {chain_entries[-1]['ts']}")
    print()

    if dry_run:
        print("  [DRY-RUN] 跳过写入")
        return 0

    # 写 fallback-chain.jsonl（append）
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with CHAIN_LOG.open("a", encoding="utf-8") as f:
        for entry in chain_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 写 fallback-canary.jsonl（append）
    with CANARY_LOG.open("a", encoding="utf-8") as f:
        for entry in canary_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"✅ 已写入 {len(chain_entries)} 条到 {CHAIN_LOG}")
    print(f"✅ 已写入 {len(canary_entries)} 条到 {CANARY_LOG}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟生成 24h audit log 历史数据")
    ap.add_argument("--hours", type=int, default=24, help="生成多少小时（默认 24）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，可复现）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写")
    args = ap.parse_args()
    return seed(args.hours, args.seed, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
