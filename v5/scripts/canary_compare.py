#!/usr/bin/env python3
"""M3 W5: canary vs control A/B 比较脚本

读 fallback-canary.jsonl + fallback-chain.jsonl（control 部分），输出：
- 控制组 vs canary 组的成功率 / p50 / p95
- 卡方检验（统计显著性）

用法：
    venv/bin/python v5/scripts/canary_compare.py [--days 7] [--audit-dir v5/audit]

环境变量：
    MATH_AUDIT_DIR: audit log 目录（默认 v5/audit）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")


def _parse_ts(ts: str) -> datetime:
    """Parse ISO 8601 with trailing Z."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * p) - 1)
    return s[idx]


def _chi_square(group_success: list[bool], group_total: list[int]) -> tuple[float, float]:
    """2x2 卡方检验（control vs canary 成功率）

    Returns (chi2, p_value) — p_value 用 Wilson 简化近似。
    """
    if len(group_success) != 2 or len(group_total) != 2:
        return 0.0, 1.0
    # 2x2 列联表
    n1, n2 = group_total
    k1, k2 = group_success  # 成功数
    n = n1 + n2
    if n == 0 or n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p = (k1 + k2) / n
    if p == 0 or p == 1:
        return 0.0, 1.0  # 全成功或全失败，无显著差异
    # 期望值
    e11 = n1 * p
    e12 = n1 * (1 - p)
    e21 = n2 * p
    e22 = n2 * (1 - p)
    # 卡方
    chi2 = (
        (k1 - e11) ** 2 / e11
        + ((n1 - k1) - e12) ** 2 / e12
        + (k2 - e21) ** 2 / e21
        + ((n2 - k2) - e22) ** 2 / e22
    )
    # 简化 p 值（df=1, chi2 → p）
    # 用 Wilson-Hilferty 近似：chi² ~ df + sqrt(2*df) * z
    z = ((chi2 / 1) ** 0.5 - 1) / (2**0.5)
    # z → p（双尾）
    p_value = 2 * (1 - _norm_cdf(abs(z)))
    return round(chi2, 4), round(p_value, 4)


def _norm_cdf(z: float) -> float:
    """标准正态 CDF 近似（Abramowitz & Stegun 7.1.26）"""
    if z < 0:
        return 1 - _norm_cdf(-z)
    t = 1 / (1 + 0.2316419 * z)
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p


def load_audit(audit_dir: Path, days: int) -> tuple[list[dict], list[dict]]:
    """加载 audit log（最近 N 天），分 control / canary"""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    chain_log = audit_dir / "fallback-chain.jsonl"
    audit_dir / "fallback-canary.jsonl"

    control = []
    canary = []
    if chain_log.exists():
        with chain_log.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(entry["ts"])
                if ts < cutoff:
                    continue
                if entry.get("group") == "canary":
                    canary.append(entry)
                else:
                    control.append(entry)
    return control, canary


def summarize(entries: list[dict]) -> dict:
    if not entries:
        return {"count": 0, "ok": 0, "success_rate": 0.0, "p50": 0.0, "p95": 0.0, "strategies": {}}
    ok = sum(1 for e in entries if e.get("ok"))
    latencies = [e.get("total_elapsed_s", 0) for e in entries]
    strategies = defaultdict(int)
    for e in entries:
        s = e.get("success_strategy") or "(failed)"
        strategies[s] += 1
    return {
        "count": len(entries),
        "ok": ok,
        "success_rate": round(ok / len(entries), 3),
        "p50_s": round(_percentile(latencies, 0.5), 3),
        "p95_s": round(_percentile(latencies, 0.95), 3),
        "strategies_used": dict(strategies),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 W5 canary vs control A/B 比较")
    ap.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    ap.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    args = ap.parse_args()

    control, canary = load_audit(args.audit_dir, args.days)

    print(f"===== M3 W5 A/B 检验（最近 {args.days} 天） =====")
    print(f"audit_dir: {args.audit_dir}")
    print()
    print(f"控制组 control: {len(control)} 调用")
    cs = summarize(control)
    for k, v in cs.items():
        print(f"  {k}: {v}")
    print()
    print(f"canary 组: {len(canary)} 调用")
    bs = summarize(canary)
    for k, v in bs.items():
        print(f"  {k}: {v}")
    print()

    # 卡方检验（成功率）
    if len(control) > 0 and len(canary) > 0:
        k1 = cs["ok"]
        k2 = bs["ok"]
        n1 = cs["count"]
        n2 = bs["count"]
        chi2, p_val = _chi_square([k1, k2], [n1, n2])
        print("===== 统计显著性（卡方检验）=====")
        print(f"  chi² = {chi2}")
        print(f"  p-value = {p_val}")
        if p_val < 0.05:
            print("  ✅ 显著差异（p<0.05），canary 与 control 表现不同")
        else:
            print("  ⚪ 不显著（p≥0.05），canary 与 control 无统计差异")

        # 策略分布对比
        print()
        print("===== 策略分布对比 =====")
        all_strats = set(cs["strategies_used"]) | set(bs["strategies_used"])
        print(f"{'strategy':<12} {'control':<10} {'canary':<10} {'差异'}")
        for s in sorted(all_strats):
            c = cs["strategies_used"].get(s, 0)
            b = bs["strategies_used"].get(s, 0)
            c_pct = round(c / n1 * 100, 1) if n1 else 0
            b_pct = round(b / n2 * 100, 1) if n2 else 0
            diff = f"+{b_pct - c_pct:.1f}%" if b_pct > c_pct else f"{b_pct - c_pct:.1f}%"
            print(f"{s:<12} {c}/{c_pct}%  {b}/{b_pct}%  {diff}")
    else:
        print("⚠️ 样本不足，跳过卡方检验（至少需 control + canary 各 1 条）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
