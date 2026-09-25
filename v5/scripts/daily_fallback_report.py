#!/usr/bin/env python3
"""M3 W6: 每日 fallback 链路日报（systemd timer 触发）

输出 Markdown 报告到 v5/reports/fallback-YYYYMMDD.md，包含：
- 当日 vs 昨日对比（success rate / latency p50/p95）
- fallback 策略分布
- canary vs control A/B 摘要
- 失败 case top 10（按耗时）
- 异常告警（如 error rate > 阈值）

用法：
    venv/bin/python v5/scripts/daily_fallback_report.py
    # 或
    0 9 * * * venv/bin/python v5/scripts/daily_fallback_report.py

环境变量：
    MATH_AUDIT_DIR: audit log 目录（默认 v5/audit）
    REPORT_DIR: 报告输出目录（默认 v5/reports）
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
DEFAULT_REPORT_DIR = Path("/home/jiuben/tdx-data-feed/v5/reports")
FALLBACK_CHAIN_LOG = DEFAULT_AUDIT_DIR / "fallback-chain.jsonl"

# 告警阈值
ERROR_RATE_THRESHOLD = 0.05  # 5%
P95_THRESHOLD_S = 5.0  # 5 秒


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * p) - 1)
    return round(s[idx], 3)


def _load_audit(audit_dir: Path, start: datetime, end: datetime) -> list[dict]:
    """读 audit log 在 [start, end) 区间的所有记录"""
    log = audit_dir / "fallback-chain.jsonl"
    if not log.exists():
        return []
    entries = []
    try:
        with log.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = entry.get("ts", "")
                if not ts_str:
                    continue
                try:
                    ts = _parse_ts(ts_str)
                except ValueError:
                    continue
                if start <= ts < end:
                    entries.append(entry)
    except OSError as e:
        print(f"⚠️ 读 audit log 失败: {e}", file=sys.stderr)
    return entries


def _summarize(entries: list[dict]) -> dict:
    if not entries:
        return {
            "count": 0,
            "ok": 0,
            "success_rate": 0.0,
            "p50_s": 0.0,
            "p95_s": 0.0,
            "p99_s": 0.0,
            "mean_s": 0.0,
            "strategies": {},
            "groups": {},
        }
    ok = sum(1 for e in entries if e.get("ok"))
    lats = [e.get("total_elapsed_s", 0) for e in entries]
    strats = Counter(e.get("success_strategy") or "(failed)" for e in entries)
    groups = Counter(e.get("group", "control") for e in entries)
    return {
        "count": len(entries),
        "ok": ok,
        "success_rate": round(ok / len(entries), 3),
        "p50_s": _percentile(lats, 0.5),
        "p95_s": _percentile(lats, 0.95),
        "p99_s": _percentile(lats, 0.99),
        "mean_s": round(statistics.mean(lats), 3) if lats else 0.0,
        "strategies": dict(strats.most_common()),
        "groups": dict(groups.most_common()),
    }


def _canary_summary(entries: list[dict]) -> dict:
    """A/B 摘要（canary vs control）"""
    canary = [e for e in entries if e.get("group") == "canary"]
    control = [e for e in entries if e.get("group") != "canary"]
    return {
        "canary": _summarize(canary),
        "control": _summarize(control),
        "canary_ratio_actual": round(len(canary) / len(entries), 3) if entries else 0.0,
    }


def _failed_top10(entries: list[dict]) -> list[dict]:
    """失败 case top 10（按 elapsed 倒序）"""
    failed = [e for e in entries if not e.get("ok")]
    failed.sort(key=lambda e: e.get("total_elapsed_s", 0), reverse=True)
    return failed[:10]


def _alerts(today: dict, canary_summary: dict) -> list[str]:
    """生成告警列表（达阈值则报警）"""
    alerts = []
    if today["count"] == 0:
        alerts.append("⚪ 今日无 fallback 调用数据（可能 audit log 未生成）")
        return alerts
    err_rate = 1 - today["success_rate"]
    if err_rate > ERROR_RATE_THRESHOLD:
        alerts.append(f"🔴 错误率 {err_rate * 100:.1f}% > {ERROR_RATE_THRESHOLD * 100:.0f}% 阈值")
    if today["p95_s"] > P95_THRESHOLD_S:
        alerts.append(f"🟠 p95 延迟 {today['p95_s']:.2f}s > {P95_THRESHOLD_S:.1f}s 阈值")
    canary = canary_summary["canary"]
    if canary["count"] >= 10 and canary["success_rate"] < today["success_rate"] - 0.1:
        alerts.append(
            f"🟡 canary 成功率 {canary['success_rate'] * 100:.1f}% "
            f"< control {today['success_rate'] * 100:.1f}% - 10pp"
        )
    if not alerts:
        alerts.append("✅ 所有指标正常")
    return alerts


def _hourly_buckets(entries: list[dict]) -> list[dict]:
    """按小时聚合（24h 趋势）"""
    buckets: dict[str, dict] = {}
    for e in entries:
        ts_str = e.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = _parse_ts(ts_str)
        except ValueError:
            continue
        hour_key = ts.strftime("%Y-%m-%d %H:00")
        if hour_key not in buckets:
            buckets[hour_key] = {
                "hour": hour_key,
                "count": 0,
                "ok": 0,
                "lats": [],
            }
        buckets[hour_key]["count"] += 1
        if e.get("ok"):
            buckets[hour_key]["ok"] += 1
        buckets[hour_key]["lats"].append(e.get("total_elapsed_s", 0))
    result = []
    for k in sorted(buckets.keys()):
        v = buckets[k]
        result.append(
            {
                "hour": k,
                "count": v["count"],
                "ok": v["ok"],
                "p50_s": _percentile(v["lats"], 0.5),
            }
        )
    return result


def _format_table(headers: list[str], rows: list[list]) -> str:
    """Markdown 表格"""
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _format_strat_table(strats: dict) -> str:
    """策略分布表"""
    total = sum(strats.values())
    rows = []
    for name, cnt in strats.items():
        pct = round(cnt / total * 100, 1) if total else 0
        rows.append([name, cnt, f"{pct}%"])
    return _format_table(["策略", "调用", "占比"], rows)


def _format_alerts(alerts: list[str]) -> str:
    return "\n".join(f"- {a}" for a in alerts)


def generate_report(audit_dir: Path, report_dir: Path) -> Path:
    """生成今日报告（vs 昨日对比）"""
    now = _utc_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    today_entries = _load_audit(audit_dir, today_start, now)
    yesterday_entries = _load_audit(audit_dir, yesterday_start, today_start)

    today = _summarize(today_entries)
    yesterday = _summarize(yesterday_entries)
    canary_sum = _canary_summary(today_entries)
    failed = _failed_top10(today_entries)
    alerts = _alerts(today, canary_sum)
    hourly = _hourly_buckets(today_entries)

    today_str = now.strftime("%Y-%m-%d")
    report_md = f"""# Fallback 链路日报 · {today_str}

> 生成时间: {now.strftime("%Y-%m-%d %H:%M:%S UTC")}
> 数据源: `{audit_dir}/fallback-chain.jsonl`

## 今日 vs 昨日

{
        _format_table(
            ["指标", "今日", "昨日", "趋势"],
            [
                [
                    "总调用",
                    today["count"],
                    yesterday["count"],
                    f"{today['count'] - yesterday['count']:+d}",
                ],
                ["成功数", today["ok"], yesterday["ok"], f"{today['ok'] - yesterday['ok']:+d}"],
                [
                    "成功率",
                    f"{today['success_rate'] * 100:.1f}%",
                    f"{yesterday['success_rate'] * 100:.1f}%",
                    f"{(today['success_rate'] - yesterday['success_rate']) * 100:+.1f}pp",
                ],
                [
                    "p50 延迟",
                    f"{today['p50_s']:.3f}s",
                    f"{yesterday['p50_s']:.3f}s",
                    f"{today['p50_s'] - yesterday['p50_s']:+.3f}s",
                ],
                [
                    "p95 延迟",
                    f"{today['p95_s']:.3f}s",
                    f"{yesterday['p95_s']:.3f}s",
                    f"{today['p95_s'] - yesterday['p95_s']:+.3f}s",
                ],
                [
                    "p99 延迟",
                    f"{today['p99_s']:.3f}s",
                    f"{yesterday['p99_s']:.3f}s",
                    f"{today['p99_s'] - yesterday['p99_s']:+.3f}s",
                ],
                [
                    "平均延迟",
                    f"{today['mean_s']:.3f}s",
                    f"{yesterday['mean_s']:.3f}s",
                    f"{today['mean_s'] - yesterday['mean_s']:+.3f}s",
                ],
            ],
        )
    }

## Fallback 策略分布

{_format_strat_table(today["strategies"])}

## A/B 实验（canary vs control）

{
        _format_table(
            ["组", "调用数", "成功率", "p50"],
            [
                [
                    "control",
                    canary_sum["control"]["count"],
                    f"{canary_sum['control']['success_rate'] * 100:.1f}%",
                    f"{canary_sum['control']['p50_s']:.3f}s",
                ],
                [
                    "canary",
                    canary_sum["canary"]["count"],
                    f"{canary_sum['canary']['success_rate'] * 100:.1f}%",
                    f"{canary_sum['canary']['p50_s']:.3f}s",
                ],
            ],
        )
    }

实际 canary 占比：{canary_sum["canary_ratio_actual"] * 100:.1f}%
"""

    if hourly:
        report_md += f"""
## 24 小时趋势

{
            _format_table(
                ["小时", "调用", "成功", "p50 延迟"],
                [
                    [h["hour"].split(" ")[1], h["count"], h["ok"], f"{h['p50_s']:.3f}s"]
                    for h in hourly
                ],
            )
        }
"""

    if failed:
        report_md += f"""
## 失败 case top {len(failed)}

{
            _format_table(
                ["时间", "group", "action", "latex 片段", "attempts"],
                [
                    [
                        e.get("ts", "")[:19],
                        e.get("group", "?"),
                        e.get("action", "?"),
                        (e.get("latex", "")[:40] + "...")
                        if len(e.get("latex", "")) > 40
                        else e.get("latex", ""),
                        " → ".join(
                            f"{a['strategy']}{'✓' if a['ok'] else '✗'}"
                            for a in e.get("attempts", [])
                        ),
                    ]
                    for e in failed
                ],
            )
        }
"""

    report_md += f"""
## 告警与建议

{_format_alerts(alerts)}

---
*报告由 `daily_fallback_report.py` 自动生成 | M3 W6 监控灰度*
"""

    # 写报告
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"fallback-{today_str}.md"
    out.write_text(report_md, encoding="utf-8")
    return out


def main() -> int:
    audit_dir = Path(os.environ.get("MATH_AUDIT_DIR", DEFAULT_AUDIT_DIR))
    report_dir = Path(os.environ.get("REPORT_DIR", DEFAULT_REPORT_DIR))
    print("📊 生成 fallback 日报...")
    print(f"  audit_dir: {audit_dir}")
    print(f"  report_dir: {report_dir}")
    out = generate_report(audit_dir, report_dir)
    print(f"✅ 报告已生成: {out}")
    print(f"   大小: {out.stat().st_size} bytes")

    # M3 W5: 日报生成后自动触发 canary_automation 决策
    try:
        import subprocess

        canary_script = Path(__file__).parent / "canary_automation.py"
        print("\\n🤖 自动触发 canary_automation 决策...")
        result = subprocess.run(
            ["python3", str(canary_script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"  ⚠ canary_automation 退出码 {result.returncode}: {result.stderr[:200]}")
    except Exception as e:
        print(f"  ⚠ 触发 canary_automation 失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
