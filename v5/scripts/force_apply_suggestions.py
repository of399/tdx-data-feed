#!/usr/bin/env python3
"""
force_apply_suggestions.py - M5 Phase 3 强制版

把 suggestion_agent 输出的建议批量应用到 vault：
  - tag_agent: 把缺失 tag 加到对应笔记 frontmatter
  - linking_agent: 在正文合适位置插入 [[wikilink]]
  - moc_agent: 自动创建 MOC 笔记
  - promotion_agent: 默认只建议，不强制执行（避免误删）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

VAULT = Path("/home/jiuben/StockVault")
AUDIT = Path("/home/jiuben/tdx-data-feed/v5/audit")
TODAY = date.today().isoformat()


def _load_suggestions() -> dict:
    files = sorted(AUDIT.glob("suggestion-*.json"), reverse=True)
    if not files:
        print("no suggestion files found", file=sys.stderr)
        return {"suggestions": []}
    return json.loads(files[0].read_text(encoding="utf-8"))


def _parse_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            fm_text = text[4:end]
            body = text[end + 5 :]
            fm = {}
            for line in fm_text.splitlines():
                m = re.match(r"^(\w+):\s*(.*)$", line.strip())
                if m:
                    fm[m.group(1)] = m.group(2).strip()
            return fm, body
    return {}, text


def _all_tags(fm):
    tags = []
    raw = fm.get("tags", "")
    if raw.startswith("[") and raw.endswith("]"):
        for t in re.split(r"[,，]\s*", raw[1:-1]):
            t = t.strip().strip("'\"")
            if t:
                tags.append(t)
    return tags


def _set_tags(fm, tags):
    fm["tags"] = "[" + ", ".join(tags) + "]"
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def apply_tag_suggestions(suggestions, dry):
    applied = 0
    skipped = 0
    by_note = {}
    for s in suggestions:
        if s.get("type") != "tag":
            continue
        in_note = s.get("in_note")
        proposed = s.get("proposed", [])
        if not in_note or not proposed:
            continue
        for t in proposed:
            by_note.setdefault(in_note, []).append(t)

    for rel_path, new_tags in by_note.items():
        path = VAULT / rel_path
        if not path.exists():
            skipped += 1
            continue
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        existing = _all_tags(fm)
        before = len(existing)
        for t in new_tags:
            if t and t not in existing:
                existing.append(t)
        if len(existing) > before:
            new_text = _set_tags(fm, existing) + body
            if not dry:
                path.write_text(new_text, encoding="utf-8")
            applied += 1
        else:
            skipped += 1
    return applied, skipped


def apply_linking_suggestions(suggestions, dry):
    applied = 0
    skipped = 0
    by_note = {}
    for s in suggestions:
        if s.get("type") != "linking":
            continue
        in_note = s.get("in_note")
        suggest_link = s.get("suggest_link", "")
        if not in_note or not suggest_link:
            continue
        # 解 [[link]] → link
        sl = str(suggest_link).strip("[]").strip()
        if not sl or "built-in" in sl or "method" in sl or "isoformat" in sl or len(sl) > 60:
            skipped += 1
            continue
        # M5 Phase 3 全市场: 保留日期 wikilink（让今天日记关联所有新笔记）
        by_note.setdefault(in_note, set()).add(sl)

    for rel_path, links in by_note.items():
        path = VAULT / rel_path
        if not path.exists():
            skipped += 1
            continue
        text = path.read_text(encoding="utf-8")
        section_re = re.compile(r"(## 相关链接\n)(.*?)(\n## |\n---|\Z)", re.DOTALL)
        m = section_re.search(text)
        if not m:
            skipped += 1
            continue
        existing_links = m.group(2).strip()
        to_add = []
        for l in sorted(links):
            if f"[[{l}]]" not in existing_links:
                to_add.append(l)
        if to_add:
            add_str = " ".join(f"[[{l}]]" for l in to_add)
            new_section = m.group(1) + existing_links + " " + add_str + "\n" + m.group(3)
            new_text = text.replace(m.group(0), new_section, 1)
            if not dry:
                path.write_text(new_text, encoding="utf-8")
            applied += 1
        else:
            skipped += 1
    return applied, skipped


def apply_moc_suggestions(suggestions, dry):
    applied = 0
    skipped = 0
    for s in suggestions:
        if s.get("type") != "moc":
            continue
        moc_path_str = s.get("suggest_create", "")
        if not moc_path_str:
            continue
        path = VAULT / moc_path_str
        if path.exists():
            skipped += 1
            continue
        title = path.stem + "（MOC）"
        # 解 [[link]] → link
        raw_links = s.get("links", [])
        clean_links = []
        for l in raw_links:
            l = str(l).strip("[]").strip()
            if l and "built-in" not in l:
                clean_links.append(l)
        tags = s.get("tags", ["MOC", "目录"])
        body = (
            f"---\ntags: [{', '.join(tags)}]\n创建日期: {TODAY}\n状态: auto-generated\n模板: moc_agent\n---\n\n# {title}\n\n> 自动生成 - M5 Phase 3 - moc_agent\n\n## 包含笔记\n\n"
            + "\n".join(f"- [[{l}]]" for l in clean_links)
            + "\n\n## 相关链接\n\n- [[朝堂制量化投研体系]]\n- [[00-收件箱]]\n"
        )
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        applied += 1
    return applied, skipped


def apply_promotion_suggestions(suggestions, dry):
    return 0, sum(1 for s in suggestions if s.get("type") == "promotion")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent",
        choices=["tag_agent", "linking_agent", "moc_agent", "promotion_agent", "all"],
        default="all",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = _load_suggestions()
    suggestions_by_agent = data.get("suggestions", {})
    # 兼容 list 格式（旧 JSON）
    if isinstance(suggestions_by_agent, list):
        flat = suggestions_by_agent
        suggestions_by_agent = {}
        for s in flat:
            suggestions_by_agent.setdefault(s.get("agent", "unknown"), []).append(s)
    total = sum(len(v) for v in suggestions_by_agent.values())
    print(f"=== Force Apply Suggestions ({'DRY' if args.dry_run else 'APPLY'}) ===")
    print(f"  loaded: {total} suggestions")
    for agent, lst in suggestions_by_agent.items():
        print(f"    {agent}: {len(lst)}")

    agents = (
        ["tag_agent", "linking_agent", "moc_agent", "promotion_agent"]
        if args.agent == "all"
        else [args.agent]
    )

    total_applied = 0
    total_skipped = 0
    for agent in agents:
        fn = {
            "tag_agent": apply_tag_suggestions,
            "linking_agent": apply_linking_suggestions,
            "moc_agent": apply_moc_suggestions,
            "promotion_agent": apply_promotion_suggestions,
        }[agent]
        sug_list = suggestions_by_agent.get(agent, [])
        applied, skipped = fn(sug_list, args.dry_run)
        print(f"  [{agent:15}] applied={applied} skipped={skipped}")
        total_applied += applied
        total_skipped += skipped

    print()
    print(f"  合计: applied={total_applied} skipped={total_skipped}")
    if args.dry_run:
        print("  DRY-RUN, 未实际修改")
    else:
        print("  vault 已更新")


if __name__ == "__main__":
    main()
