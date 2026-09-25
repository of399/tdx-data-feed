#!/usr/bin/env python3
"""
suggestion_agent.py · M5 Phase 2 简化版

目标：vault 不够大（13 笔记 + 0 tag + 0 wikilink），但 Phase 2/3 的 4 个 Agent
    （LinkingAgent / TagAgent / MOCAgent / PromotionAgent）不能强制改笔记。
    改用"只出建议、不改 vault"模式 → audit/suggestion-{date}.md

4 个 Agent（每次跑全部）：
1. linking_agent   - 找 [[wikilink]] 缺失（基于笔记标题关键词重叠）
2. tag_agent       - 建议 #tag（基于 frontmatter + 目录 + 标题）
3. moc_agent       - 建议 MOC（Map of Content）按目录归类
4. promotion_agent - 建议从收件箱 → 标的 → 框架 → 知识库 升级

用法：
    python suggestion_agent.py            # 跑全部 4 个 Agent + 写 report
    python suggestion_agent.py --agent linking_agent
    python suggestion_agent.py --dry-run  # 只输出到 stdout

输出：
    /home/jiuben/tdx-data-feed/v5/audit/suggestion-{date}.md
    /home/jiuben/tdx-data-feed/v5/audit/suggestion-{date}.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------- 路径 ----------
V5_BASE = Path("/home/jiuben/tdx-data-feed/v5")
VAULT = Path("/home/jiuben/StockVault")
AUDIT_DIR = V5_BASE / "audit"
REPORTS_DIR = V5_BASE / "reports"


# ---------- 工具 ----------
def _today() -> str:
    return dt.date.today().isoformat()


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _read_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML 风格 frontmatter（---\nkey: val\n---）

    返回 ({key: val}, 剩余正文)
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_text = parts[2]
    body = parts[3] if len(parts) > 3 else ""
    fm = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


def _tokenize(text: str) -> set[str]:
    """中文友好的分词：2-gram + 英文单词"""
    text = text.lower()
    tokens: set[str] = set()
    # 英文/数字
    for w in re.findall(r"[a-z0-9]+", text):
        if len(w) >= 2:
            tokens.add(w)
    # 中文 2-gram
    for m in re.finditer(r"[\u4e00-\u9fa5]+", text):
        seg = m.group()
        for i in range(len(seg) - 1):
            tokens.add(seg[i : i + 2])
    return tokens


def _list_md_files() -> list[Path]:
    return sorted(VAULT.rglob("*.md"))


def _note_meta(path: Path) -> dict:
    """提取笔记元信息：标题、frontmatter tags、目录、关键词"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {
            "path": path,
            "rel_path": str(path.relative_to(VAULT)),
            "title": path.stem,
            "tags": [],
            "tokens": set(),
            "fm": {},
            "first_heading": "",
        }

    fm, body = _read_frontmatter(text)
    title = path.stem  # 文件名 = 标题（Obsidian 约定）
    first_heading = ""
    for line in body.split("\n"):
        if line.startswith("# "):
            first_heading = line[2:].strip()
            break
    if not first_heading:
        first_heading = title

    # 提取 tags（frontmatter 或 inline #tag）
    fm_tags = fm.get("tags", "")
    inline_tags = re.findall(r"#[a-zA-Z\u4e00-\u9fa5][a-zA-Z0-9\u4e00-\u9fa5_-]*", body)
    tags = set()
    if fm_tags:
        tags.update(t.strip() for t in re.split(r"[\s,]+", fm_tags) if t.strip())
    tags.update(t.lstrip("#") for t in inline_tags if len(t) >= 2)

    tokens = _tokenize(first_heading + " " + body[:2000])  # 取前 2KB 标题词

    return {
        "path": path,
        "rel_path": str(path.relative_to(VAULT)),
        "title": title,
        "first_heading": first_heading,
        "tags": sorted(tags),
        "tokens": tokens,
        "fm": fm,
        "size": len(text),
        "dir": str(path.parent.relative_to(VAULT)) if path.parent != VAULT else ".",
    }


# ---------- Agent 1: linking_agent ----------
def linking_agent(notes: list[dict]) -> list[dict]:
    """找 [[wikilink]] 缺失

    对每个笔记 A，找其他笔记 B：
    - A 的正文提到 B 的标题（或文件名）→ 建议加 [[B]]
    - A 的关键词与 B 的关键词重叠度 >= threshold → 建议互相链接
    """
    suggestions = []
    title_index = {n["title"]: n for n in notes}

    for note in notes:
        try:
            text = note["path"].read_text(encoding="utf-8")
        except Exception:
            continue

        # 1. 标题/文件名出现在正文 → 建议 wikilink
        for other_title, _other in title_index.items():
            if other_title == note["title"]:
                continue
            if len(other_title) < 4:  # 跳过太短的标题（误报多）
                continue
            if other_title in text and f"[[{other_title}]]" not in text:
                suggestions.append(
                    {
                        "type": "linking",
                        "in_note": note["rel_path"],
                        "suggest_link": f"[[{other_title}]]",
                        "reason": f"正文提到'{other_title}'但未 wikilink",
                        "priority": "high" if other_title in note["first_heading"] else "medium",
                    }
                )

    return suggestions


# ---------- Agent 2: tag_agent ----------
TAG_KEYWORDS = {
    # 中英文关键词 → 建议 tag
    "量化": "quant",
    "选股": "stock-picking",
    "回测": "backtest",
    "agent": "agent",
    "agent体系": "agent-system",
    "系统学": "systems-science",
    "系统工程": "systems-engineering",
    "控制论": "cybernetics",
    "知识库": "knowledge-base",
    "obsidian": "obsidian",
    "vault": "vault",
    "数据": "data",
    "数据字典": "data-dictionary",
    "tdx": "tdx",
    "math": "math",
    "vipdoc": "vipdoc",
    "volume": "volume",
    "单位治理": "unit-governance",
    "朝堂制": "chao-tang-system",
    "方法论": "methodology",
    "框架": "framework",
    "模板": "template",
    "导读": "intro",
    "书单": "booklist",
    "可用性": "usability",
    "可用性映射": "usability-map",
}


def tag_agent(notes: list[dict]) -> list[dict]:
    """建议 #tag（基于 frontmatter + 目录 + 标题 + 关键词）"""
    suggestions = []

    for note in notes:
        existing_tags = set(note["tags"])
        proposed = set()

        # 1. frontmatter 没有 tags
        if not note["fm"].get("tags"):
            suggestions.append(
                {
                    "type": "tag",
                    "in_note": note["rel_path"],
                    "issue": "missing_tags",
                    "proposed": [],
                    "reason": "笔记无 frontmatter tags",
                    "priority": "low",
                }
            )

        # 2. 关键词 → 建议 tag
        for keyword, tag in TAG_KEYWORDS.items():
            if (
                keyword in note["first_heading"] or keyword in note["rel_path"]
            ) and tag not in existing_tags:
                proposed.add(tag)

        if proposed:
            suggestions.append(
                {
                    "type": "tag",
                    "in_note": note["rel_path"],
                    "issue": "tag_based_on_keyword",
                    "proposed": sorted(proposed),
                    "reason": f"关键词命中 → 建议 tag: {', '.join(sorted(proposed))}",
                    "priority": "medium",
                }
            )

    return suggestions


# ---------- Agent 3: moc_agent ----------
def moc_agent(notes: list[dict]) -> list[dict]:
    """按目录自动建议 MOC（Map of Content）

    每个非空目录生成一个 MOC 笔记建议
    """
    dir_notes: dict[str, list[dict]] = defaultdict(list)
    for note in notes:
        dir_notes[note["dir"]].append(note)

    suggestions = []
    for d, ns in dir_notes.items():
        if d == "." or d.startswith("00-收件箱"):
            continue  # 跳过收件箱
        if len(ns) < 2:
            continue  # 单笔记目录不需要 MOC

        moc_name = f"{d}/00-MOC.md"
        ns_titles = sorted(n["title"] for n in ns)
        suggestions.append(
            {
                "type": "moc",
                "dir": d,
                "suggest_create": moc_name,
                "links": [f"[[{t}]]" for t in ns_titles],
                "reason": f"目录有 {len(ns)} 个笔记，建议建立 MOC",
                "priority": "high" if len(ns) >= 5 else "medium",
            }
        )

    return suggestions


# ---------- Agent 4: promotion_agent ----------
def promotion_agent(notes: list[dict]) -> list[dict]:
    """笔记升级建议

    路径：
      00-收件箱 → 01-标的 / 02-框架 / 03-数据 / 04-回测 / 05-知识库
    """
    suggestions = []

    for note in notes:
        dir_name = note["dir"]
        if not dir_name.startswith("00-收件箱"):
            continue

        # 看标题/内容判断属于哪个目录
        text = note["first_heading"].lower()
        target_dir = None

        # 简单的关键词分类
        if any(kw in text for kw in ["股票", "个股", "标的", "600", "300", "002"]):
            target_dir = "01-标的"
        elif any(kw in text for kw in ["策略", "方法论", "框架", "公式"]):
            target_dir = "02-框架"
        elif any(kw in text for kw in ["数据", "字典", "vipdoc", "tdx"]):
            target_dir = "03-数据"
        elif any(kw in text for kw in ["回测"]):
            target_dir = "04-回测"
        elif any(kw in text for kw in ["读书", "导读", "知识", "理论", "ai", "agent"]):
            target_dir = "05-知识库"

        if target_dir:
            suggestions.append(
                {
                    "type": "promotion",
                    "in_note": note["rel_path"],
                    "target_dir": target_dir,
                    "reason": f"建议从 00-收件箱 移至 {target_dir}",
                    "priority": "medium",
                }
            )
        else:
            suggestions.append(
                {
                    "type": "promotion",
                    "in_note": note["rel_path"],
                    "target_dir": None,
                    "reason": "无法自动分类，请人工 review",
                    "priority": "low",
                }
            )

    return suggestions


# ---------- 报告生成 ----------
def run_agent(name: str, notes: list[dict]) -> list[dict]:
    if name == "linking_agent":
        return linking_agent(notes)
    if name == "tag_agent":
        return tag_agent(notes)
    if name == "moc_agent":
        return moc_agent(notes)
    if name == "promotion_agent":
        return promotion_agent(notes)
    raise ValueError(f"unknown agent: {name}")


def all_agents() -> list[str]:
    return ["linking_agent", "tag_agent", "moc_agent", "promotion_agent"]


def build_report(suggestions_by_agent: dict[str, list[dict]], notes_count: int) -> str:
    """生成 Markdown 报告"""
    today = _today()
    lines = [
        f"# Vault 演化建议 · {today}",
        "",
        f"> 生成时间: {_now_iso()}",
        f"> Vault: `{VAULT}` ({notes_count} 个)",
        "> 数据源: `v5/scripts/suggestion_agent.py`",
        "",
        "## 总览",
        "",
        "| Agent | 建议数 | High | Medium | Low |",
        "|---|---|---|---|---|",
    ]

    total = 0
    for agent, sugs in suggestions_by_agent.items():
        high = sum(1 for s in sugs if s.get("priority") == "high")
        med = sum(1 for s in sugs if s.get("priority") == "medium")
        low = sum(1 for s in sugs if s.get("priority") == "low")
        total += len(sugs)
        lines.append(f"| {agent} | {len(sugs)} | {high} | {med} | {low} |")
    lines.append(f"| **合计** | **{total}** | | | |")
    lines.append("")

    # 每个 Agent 详情
    for agent, sugs in suggestions_by_agent.items():
        lines.append(f"## {agent} ({len(sugs)} 条建议)")
        lines.append("")
        if not sugs:
            lines.append("（无建议）")
            lines.append("")
            continue
        for i, s in enumerate(sugs, 1):
            lines.append(f"### {i}. {s.get('reason', '?')}")
            lines.append(f"- **优先级**: `{s.get('priority', '?')}`")
            for k, v in s.items():
                if k in ("reason", "priority", "type"):
                    continue
                v_str = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
                lines.append(f"- **{k}**: {v_str}")
            lines.append("")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=[*all_agents(), "all"], default="all")
    parser.add_argument("--vault", default=None, help="vault 路径（覆盖默认）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    agents = all_agents() if args.agent == "all" else [args.agent]

    # 允许 --vault 覆盖
    if args.vault:
        global VAULT
        VAULT = Path(args.vault)

    if not VAULT.exists():
        print(f"vault not found: {VAULT}", file=sys.stderr)
        return 1

    print(f"加载 vault: {VAULT}", file=sys.stderr)
    notes = [_note_meta(p) for p in _list_md_files()]
    print(f"笔记数: {len(notes)}", file=sys.stderr)

    suggestions_by_agent: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}

    for agent in agents:
        sugs = run_agent(agent, notes)
        suggestions_by_agent[agent] = sugs
        summary[agent] = {
            "total": len(sugs),
            "high": sum(1 for s in sugs if s.get("priority") == "high"),
            "medium": sum(1 for s in sugs if s.get("priority") == "medium"),
            "low": sum(1 for s in sugs if s.get("priority") == "low"),
        }
        print(f"  {agent}: {len(sugs)} 条建议", file=sys.stderr)

    today = _today()

    # 写 JSON（机器可读）
    json_data = {
        "ts": _now_iso(),
        "vault": str(VAULT),
        "notes_count": len(notes),
        "summary": summary,
        "suggestions": suggestions_by_agent,
    }
    json_data = json.loads(json.dumps(json_data, default=str, ensure_ascii=False))

    if args.dry_run:
        print(json.dumps(json_data, indent=2, ensure_ascii=False))
        return 0

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = AUDIT_DIR / f"suggestion-{today}.json"
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  → JSON: {json_path}", file=sys.stderr)

    md_path = REPORTS_DIR / f"suggestion-{today}.md"
    md_text = build_report(suggestions_by_agent, len(notes))
    md_path.write_text(md_text, encoding="utf-8")
    print(f"  → Markdown: {md_path}", file=sys.stderr)

    total = sum(summary[a]["total"] for a in agents)
    print(f"\n✓ 建议生成完成: {total} 条 ({', '.join(agents)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
