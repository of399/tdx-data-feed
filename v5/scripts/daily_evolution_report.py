#!/usr/bin/env python3
"""daily_evolution_report.py · M5 Phase 1 · Vault 演化日报

每日 09:00 由 systemd timer 触发，生成 Markdown 报告到 v5/reports/evolution-YYYYMMDD.md

日报内容：
1. Agent 跑批统计（scheduler.jsonl 聚合）
2. vault 演化进度（笔记数 / #tag / [[wikilink]]）
3. scan_obsidian_vault.py 最新结果摘要
4. tdx_evolution heartbeat + 健康检查
5. 异常告警（如 vault 长时间无变化 / agent 失败率高）

用法：
  venv/bin/python v5/scripts/daily_evolution_report.py

环境变量：
  V5_DIR: v5 根目录（默认 /home/jiuben/tdx-data-feed/v5）
  VAULT_PATH: Obsidian vault 目录
  MATH_AUDIT_DIR: audit log 目录
  REPORT_DIR: 报告输出目录
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

V5_DIR = Path(os.environ.get("V5_DIR", "/home/jiuben/tdx-data-feed/v5"))
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/home/jiuben/StockVault"))
AUDIT_DIR = Path(os.environ.get("MATH_AUDIT_DIR", str(V5_DIR / "audit")))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", str(V5_DIR / "reports")))

SCHEDULER_LOG = AUDIT_DIR / "scheduler.jsonl"
EVOLUTION_LOG = AUDIT_DIR / "evolution.jsonl"
SYSTEM_HEALTH_LOG = AUDIT_DIR / "system-health.jsonl"

# 告警阈值
AGENT_FAILURE_THRESHOLD = 0.20  # 20%
VAULT_GROWTH_DAYS = 7  # 7 天无增长触发告警


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _read_jsonl(path: Path, since: datetime) -> list[dict]:
    """读取 since 时间以来的 jsonl"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            ts_str = d.get("ts", "")
            if not ts_str:
                continue
            ts_dt = _parse_ts(ts_str)
            if ts_dt >= since:
                out.append(d)
        except json.JSONDecodeError, ValueError:
            continue
    return out


def _vault_stats() -> dict:
    """统计 vault 笔记 / #tag / [[wikilink]]"""
    stats = {
        "md_files": 0,
        "tags": set(),
        "wikilinks": set(),
        "total_chars": 0,
        "by_dir": Counter(),
    }
    if not VAULT_PATH.exists():
        return {**stats, "tags": 0, "wikilinks": 0}
    for md in VAULT_PATH.rglob("*.md"):
        stats["md_files"] += 1
        rel = md.relative_to(VAULT_PATH)
        top_dir = rel.parts[0] if rel.parts else "(root)"
        stats["by_dir"][top_dir] += 1
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
            stats["total_chars"] += len(text)
            import re

            # 1) 内联 #tag
            stats["tags"].update(re.findall(r"#([\w\u4e00-\u9fa5][\w\u4e00-\u9fa5-]*)", text))
            # 2) frontmatter tags: [a, b, c]
            fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
            if fm_match:
                fm_body = fm_match.group(1)
                # 解析 tags: [tag1, tag2, tag3]
                tags_match = re.search(r"^tags:\s*\[([^\]]*)\]", fm_body, re.MULTILINE)
                if tags_match:
                    tags_str = tags_match.group(1)
                    for t in re.split(r"[,，]\s*", tags_str):
                        t = t.strip().strip("'\"")
                        if t:
                            stats["tags"].add(t)
            stats["wikilinks"].update(re.findall(r"\[\[([^\]]+)\]\]", text))
        except Exception:
            pass
    return {
        "md_files": stats["md_files"],
        "tags": len(stats["tags"]),
        "wikilinks": len(stats["wikilinks"]),
        "total_chars": stats["total_chars"],
        "by_dir": dict(stats["by_dir"].most_common(10)),
    }


def _scheduler_summary(since: datetime) -> dict:
    """聚合 scheduler.jsonl（agent 跑批统计）"""
    entries = _read_jsonl(SCHEDULER_LOG, since)
    by_agent = defaultdict(lambda: {"total": 0, "ok": 0, "fail": 0, "skip": 0, "duration_s": []})
    for e in entries:
        agent = e.get("agent", "?")
        decision = e.get("decision", "?")
        by_agent[agent]["total"] += 1
        if decision in ("run", "manual_override"):
            if e.get("ok"):
                by_agent[agent]["ok"] += 1
            else:
                by_agent[agent]["fail"] += 1
            d = e.get("duration_s", 0)
            if d > 0:
                by_agent[agent]["duration_s"].append(d)
        else:
            by_agent[agent]["skip"] += 1
    # 计算失败率
    for _agent, d in by_agent.items():  # agent 没用, by_agent 字典已聚合

        exec_total = d["ok"] + d["fail"]
        d["failure_rate"] = round(d["fail"] / exec_total, 3) if exec_total > 0 else 0
        d["avg_duration_s"] = (
            round(sum(d["duration_s"]) / len(d["duration_s"]), 2) if d["duration_s"] else 0
        )
        d.pop("duration_s")
    return dict(by_agent)


def _evolution_heartbeats(since: datetime) -> dict:
    """统计 tdx_evolution.py heartbeat"""
    entries = _read_jsonl(EVOLUTION_LOG, since)
    if not entries:
        return {"heartbeats": 0, "last_heartbeat": None, "first_in_window": None}
    return {
        "heartbeats": len(entries),
        "last_heartbeat": entries[-1].get("ts"),
        "first_in_window": entries[0].get("ts"),
    }


def _vault_growth_check() -> str:
    """检查 vault 增长（过去 N 天有无新增笔记）"""
    if not VAULT_PATH.exists():
        return "vault_missing"
    # 简化检查：用文件 mtime
    cutoff = _utc_now() - timedelta(days=VAULT_GROWTH_DAYS)
    recent = 0
    for md in VAULT_PATH.rglob("*.md"):
        try:
            mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                recent += 1
        except Exception:
            pass
    return f"{recent} 个笔记在最近 {VAULT_GROWTH_DAYS} 天有改动"


def _generate_report() -> str:
    """生成 Markdown 日报"""
    now = _utc_now()
    yesterday = now - timedelta(days=1)
    today_str = now.strftime("%Y-%m-%d")

    vault = _vault_stats()
    sched = _scheduler_summary(yesterday)
    evo = _evolution_heartbeats(yesterday)

    # 调度统计
    total_agent_runs = sum(d["ok"] + d["fail"] for d in sched.values())
    total_agent_fails = sum(d["fail"] for d in sched.values())
    fail_rate = total_agent_fails / total_agent_runs if total_agent_runs > 0 else 0

    alerts = []
    if fail_rate > AGENT_FAILURE_THRESHOLD:
        alerts.append(
            f"🟠 Agent 失败率 {fail_rate * 100:.1f}% > {AGENT_FAILURE_THRESHOLD * 100:.0f}% 阈值"
        )
    growth_status = _vault_growth_check()
    if growth_status.startswith("0"):
        alerts.append(f"🟡 vault 过去 {VAULT_GROWTH_DAYS} 天无变化（{growth_status}）")
    if evo["heartbeats"] == 0:
        alerts.append("🔴 tdx_evolution heartbeat 缺失（服务可能挂了）")

    lines = [
        f"# Vault 演化日报 · {today_str}",
        "",
        f"> 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"> 数据源: `{AUDIT_DIR}/`",
        f"> Vault: `{VAULT_PATH}`",
        "",
        "## Vault 健康度",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 笔记数 | {vault['md_files']} |",
        f"| #tag 数 | {vault['tags']} |",
        f"| [[wikilink]] 数 | {vault['wikilinks']} |",
        f"| 总字符数 | {vault['total_chars']:,} |",
        f"| 最近活跃 | {growth_status} |",
        "",
    ]
    if vault["by_dir"]:
        lines.append("### Top 目录分布")
        lines.append("")
        for d, c in list(vault["by_dir"].items())[:10]:
            lines.append(f"- `{d}/`: {c} 个笔记")
        lines.append("")

    lines.extend(
        [
            "## Agent 跑批统计（最近 24h）",
            "",
            "| Agent | 总调用 | 成功 | 失败 | 跳过 | 失败率 | 平均耗时 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    if sched:
        for agent, d in sched.items():
            lines.append(
                f"| {agent} | {d['total']} | {d['ok']} | {d['fail']} | {d['skip']} | {d['failure_rate'] * 100:.1f}% | {d['avg_duration_s']}s |"
            )
    else:
        lines.append("| (无数据) | 0 | 0 | 0 | 0 | - | - |")
    lines.append("")
    lines.extend(
        [
            "## tdx_evolution 心跳",
            "",
            f"- 心跳数: {evo['heartbeats']}",
            f"- 首次: {evo['first_in_window'] or '-'}",
            f"- 最近: {evo['last_heartbeat'] or '-'}",
            "",
            "## 告警与建议",
            "",
        ]
    )
    if alerts:
        lines.extend(f"- {a}" for a in alerts)
    else:
        lines.append("- ✅ 一切正常")
    lines.append("")
    lines.append("---")
    lines.append("*报告由 `daily_evolution_report.py` 自动生成 | M5 Phase 1 · Vault 5 Agent*")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = _utc_now().strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"evolution-{today}.md"

    print("📊 生成 vault 演化日报...", file=sys.stderr)
    print(f"  audit_dir: {AUDIT_DIR}", file=sys.stderr)
    print(f"  vault: {VAULT_PATH}", file=sys.stderr)
    print(f"  report_dir: {REPORT_DIR}", file=sys.stderr)

    md = _generate_report()
    report_path.write_text(md, encoding="utf-8")
    size = report_path.stat().st_size
    print(f"✅ 报告已生成: {report_path}", file=sys.stderr)
    print(f"   大小: {size} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
