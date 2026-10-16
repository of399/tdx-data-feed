#!/usr/bin/env python3
"""fix_obsidian_metadata.py (Phase 2 自动修复)

对强校验模块（标的/框架/回测/知识库）笔记，增量补全缺失/空的必填字段：
  状态(active) / 模板(vault_seeder) / 创建日期(mtime 或文件名日期)
缺整块 frontmatter 的笔记：按模块补全默认 frontmatter。
仅增量补全，绝不删除/覆盖已有字段；幂等（重跑无副作用）。
结构性问题（标的重复、贵州茅台文件名无代码）仅报告不自动改。

用法：python3 scripts/fix_obsidian_metadata.py [--vault PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("✗ 缺少 PyYAML (pip install pyyaml)\n")
    sys.exit(2)

DEFAULT_VAULT = "/home/jiuben/StockVault"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
STRONG = {"01-标的", "02-框架", "04-回测", "05-知识库"}
MODULE_TAGS = {
    "01-标的": ["标的"],
    "02-框架": ["框架"],
    "04-回测": ["回测"],
    "05-知识库": ["知识库"],
}


def parse_fm(text: str):
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def derive_created(path: Path) -> str:
    mn = DATE_IN_NAME.search(path.name)
    if mn:
        return mn.group(1)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def dump_fm(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip("\n") + "\n---\n"


def fix_file(path: Path, vault: Path, report: dict) -> None:
    rel = str(path.relative_to(vault))
    text = path.read_text(encoding="utf-8", errors="replace")
    data = parse_fm(text)
    if data is None:
        created = derive_created(path)
        top = path.parts[0]
        fm = {
            "tags": MODULE_TAGS.get(top, ["知识库"]),
            "创建日期": created,
            "状态": "active",
            "模板": "vault_seeder",
        }
        new = dump_fm(fm) + "\n" + text.lstrip("\n")
        path.write_text(new, encoding="utf-8")
        report["created_fm"].append(rel)
        return
    changed = False
    for fld, val in (
        ("状态", "active"),
        ("模板", "vault_seeder"),
        ("创建日期", derive_created(path)),
    ):
        if fld not in data or is_empty(data.get(fld)):
            data[fld] = val
            changed = True
    if changed:
        body = re.sub(r"^---\s*\n.*?\n---\s*(?:\n|$)", "", text, count=1, flags=re.DOTALL)
        path.write_text(dump_fm(data) + body, encoding="utf-8")
        report["fixed_fields"].append(rel)
    else:
        report["already_ok"].append(rel)


def main() -> None:
    ap = argparse.ArgumentParser(description="Obsidian 元数据自动修复")
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    vault = Path(args.vault)
    report = {"created_fm": [], "fixed_fields": [], "already_ok": [], "skipped": []}

    files = [p for p in vault.rglob("*.md") if not any(s in p.parts for s in SKIP_DIRS)]
    for p in files:
        rel = p.relative_to(vault)
        top = rel.parts[0] if rel.parts else ""
        # 数学笔记有独立 schema（不含 模板/创建日期），由 verify 单独校验，此处跳过
        if "数学" in rel.parts:
            continue
        if top not in STRONG:
            continue
        if p.name == "贵州茅台.md":
            # 仅补字段，不动文件名（结构性重命名待人工确认）
            if args.dry_run:
                report["skipped"].append(str(p.relative_to(vault)) + " (文件名无代码，仅补字段)")
            else:
                fix_file(p, vault, report)
            continue
        if args.dry_run:
            continue
        fix_file(p, vault, report)

    print("# 元数据自动修复")
    print(f"vault: {vault}")
    print(f"新建 frontmatter: {len(report['created_fm'])}")
    print(f"补全缺失字段: {len(report['fixed_fields'])}")
    print(f"无需改动: {len(report['already_ok'])}")
    print(f"跳过(结构性): {len(report['skipped'])}")
    for f in report["created_fm"]:
        print(f"  +FM  {f}")
    for f in report["fixed_fields"]:
        print(f"  ~字段 {f}")
    for s in report["skipped"]:
        print(f"  !跳过 {s}")
    print("---")
    print("结构性待确认项（不自动改）：")
    print("  1) 000002 标的重复：000002-万科A.md / 000002-万科Ａ.md（全/半角，需人工合并）")
    print("  2) 贵州茅台.md 文件名无 6 位代码前缀（建议重命名 600519-贵州茅台.md 并同步 [[贵州茅台]] 引用）")


if __name__ == "__main__":
    main()
