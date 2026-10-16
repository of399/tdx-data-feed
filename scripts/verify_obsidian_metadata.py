#!/usr/bin/env python3
"""verify_obsidian_metadata.py (v1)

Obsidian 笔记元数据完整性校验（Phase 2 数据完整性）。

校验各模块笔记 frontmatter 必填字段完整、非空、格式正确；标的文件名编码与去重。
只读扫描，不改盘。输出结构化报告 + 退出码。

规则源：docs/obsidian_metadata_schema.md

用法：
  python3 scripts/verify_obsidian_metadata.py [--vault PATH] [--json] [--max-issues N]

退出码：
  0 = 无错误（允许警告）
  1 = 存在字段错误 / 格式错误 / 重复
  2 = 参数或依赖错误
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("✗ 缺少依赖: PyYAML (pip install pyyaml)\n")
    sys.exit(2)

DEFAULT_VAULT = "/home/jiuben/StockVault"

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STOCK_CODE_RE = re.compile(r"^(\d{6})-(.+)\.md$")
STOCK_DUP_RE = re.compile(r"^(\d{6})-")

# 模块分类 → schema
# required: 必填字段；date_fields: 须为 YYYY-MM-DD；exempt: 豁免
SCHEMA: dict[str, dict] = {
    "标的":   {"required": ["tags", "创建日期", "状态", "模板"], "date_fields": ["创建日期"], "code_from_file": True},
    "框架":   {"required": ["tags", "创建日期", "状态", "模板"], "date_fields": ["创建日期"]},
    "回测":   {"required": ["tags", "创建日期", "状态", "模板"], "date_fields": ["创建日期"]},
    "知识库": {"required": ["tags", "创建日期", "状态", "模板"], "date_fields": ["创建日期"]},
    "数学":   {
        "required": ["title", "type", "domain", "level", "status", "tags", "created"],
        "date_fields": ["created", "next_review"],
    },
    "数据":   {"exempt": True},
    "模板":   {"exempt": True},
    "日记":   {"exempt": True},
    "收件箱": {"exempt": True},
    "其他":   {"exempt": True},
}


def classify(rel_parts: tuple[str, ...]) -> str:
    top = rel_parts[0] if rel_parts else ""
    if top == "01-标的":
        return "标的"
    if top == "02-框架":
        return "框架"
    if top == "03-数据":
        return "数据"
    if top == "04-回测":
        return "回测"
    if top == "05-知识库":
        return "数学" if "数学" in rel_parts else "知识库"
    if top == "06-模板":
        return "模板"
    if top == "07-日记":
        return "日记"
    if top == "00-收件箱":
        return "收件箱"
    return "其他"


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """返回 (data, error)。data=None 且 error=None 表示无 frontmatter。"""
    if not text.startswith("---"):
        return None, None
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return None, "frontmatter 未正确闭合(缺少结束 ---)"
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return None, f"YAML 解析错误: {e}"
    if not isinstance(data, dict):
        return None, "frontmatter 非字典"
    return data, None


def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def validate_file(path: Path, vault: Path, issues: list[dict], warnings: list[dict],
                  stock_codes: dict[str, list[str]]) -> str:
    rel = path.relative_to(vault)
    mod = classify(rel.parts)
    schema = SCHEMA.get(mod, {"exempt": True})

    # MOC / 总览类 landing 页豁免（索引文件不遵循笔记 schema）
    is_moc = (path.stem.startswith("00-MOC") or path.stem.startswith("00-")
              or path.name == "总览.md" or "MOC" in path.stem)

    text = path.read_text(encoding="utf-8", errors="replace")
    data, err = parse_frontmatter(text)
    tags = []
    if data and not err:
        t = data.get("tags")
        if isinstance(t, list):
            tags = [str(x) for x in t]

    # 标的文件名编码 + 去重统计（仅对带「标的」标签的笔记强制；
    # MOC / 无标签文件不触发文件名规则）
    if mod == "标的" and not is_moc and "标的" in tags:
        m = STOCK_CODE_RE.match(path.name)
        if m:
            stock_codes.setdefault(m.group(1), []).append(str(path))
        else:
            issues.append({"file": str(rel), "field": "<filename>", "problem": "标的文件名不匹配 ^\\d{6}-名称.md$"})

    if schema.get("exempt") or is_moc:
        return mod

    if err:
        issues.append({"file": str(rel), "field": "<frontmatter>", "problem": err})
        return mod
    if data is None:
        issues.append({"file": str(rel), "field": "<frontmatter>", "problem": "缺少 frontmatter"})
        return mod

    # 必填字段
    for f in schema["required"]:
        if f not in data:
            issues.append({"file": str(rel), "field": f, "problem": "字段缺失"})
        elif is_empty(data[f]):
            issues.append({"file": str(rel), "field": f, "problem": "字段为空"})

    # 日期格式
    for f in schema.get("date_fields", []):
        if f in data and not is_empty(data[f]):
            val = str(data[f])
            if not DATE_RE.match(val):
                issues.append({"file": str(rel), "field": f, "problem": f"日期格式错误: {val!r} (期望 YYYY-MM-DD)"})

    # tags 合法性
    if "tags" in data and not is_empty(data["tags"]):
        tags2 = data["tags"]
        if not isinstance(tags2, list) or len(tags2) == 0:
            issues.append({"file": str(rel), "field": "tags", "problem": "tags 应为非空列表"})
        else:
            if mod == "数学" and "math-notes" not in tags2:
                issues.append({"file": str(rel), "field": "tags", "problem": "数学笔记 tags 应含 'math-notes'"})

    # status == skeleton → 警告（笔记未填充）
    if data.get("状态") == "skeleton" or data.get("status") == "skeleton":
        warnings.append({"file": str(rel), "field": "状态", "problem": "仍为 skeleton（笔记未填充）"})

    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description="Obsidian 元数据完整性校验")
    ap.add_argument("--vault", default=DEFAULT_VAULT, help=f"vault 根目录 (默认 {DEFAULT_VAULT})")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--max-issues", type=int, default=30, help="每类最多列出问题数")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        sys.stderr.write(f"✗ vault 不存在: {vault}\n")
        return 2

    issues: list[dict] = []
    warnings: list[dict] = []
    stock_codes: dict[str, list[str]] = {}
    counts: dict[str, int] = defaultdict(int)

    md_files = [p for p in vault.rglob("*.md") if not any(s in p.parts for s in SKIP_DIRS)]
    for p in md_files:
        mod = validate_file(p, vault, issues, warnings, stock_codes)
        counts[mod] += 1

    # 标的去重检测
    dup_groups: list[tuple[str, list[str]]] = []
    for code, files in stock_codes.items():
        if len(files) > 1:
            dup_groups.append((code, files))

    # 汇总
    total = len(md_files)
    err_count = len(issues)
    warn_count = len(warnings)
    dup_count = sum(len(f) - 1 for _, f in dup_groups)

    if args.json:
        import json
        print(json.dumps({
            "vault": str(vault),
            "total_notes": total,
            "by_module": dict(counts),
            "errors": issues,
            "warnings": warnings,
            "duplicate_stock_codes": {c: f for c, f in dup_groups},
        }, ensure_ascii=False, indent=2))
        return 1 if err_count else 0

    # 文本报告
    print(f"# Obsidian 元数据完整性校验")
    print(f"vault: {vault}")
    print(f"扫描笔记: {total}")
    print(f"---")
    print(f"各模块笔记数:")
    for mod in ["标的", "框架", "回测", "知识库", "数学", "数据", "模板", "日记", "收件箱", "其他"]:
        if counts.get(mod):
            print(f"  {mod}: {counts[mod]}")
    print(f"---")
    print(f"错误(字段/格式/去重): {err_count}")
    print(f"警告(skeleton 等): {warn_count}")

    if dup_groups:
        print(f"\n[标的重复代码] {len(dup_groups)} 组, 共 {dup_count} 个多余文件:")
        for code, files in dup_groups[:args.max_issues]:
            print(f"  {code}: {', '.join(f.split('/')[-1] for f in files)}")

    if issues:
        print(f"\n[错误明细] (最多 {args.max_issues}):")
        for it in issues[:args.max_issues]:
            print(f"  [{it['file']}] {it['field']}: {it['problem']}")

    if warnings:
        print(f"\n[警告明细] (最多 {args.max_issues}, skeleton 未填充):")
        for it in warnings[:args.max_issues]:
            print(f"  [{it['file']}] {it['field']}: {it['problem']}")

    print(f"---")
    if err_count == 0:
        print(f"✓ 元数据校验通过（错误 0；警告 {warn_count}；重复 {dup_count}）")
        return 0
    print(f"✗ 元数据校验发现 {err_count} 个错误")
    return 1


if __name__ == "__main__":
    sys.exit(main())
