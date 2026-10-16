#!/usr/bin/env python3
"""verify_obsidian_links.py (v1.1)

Obsidian 链接完整性 + 循环依赖校验（Phase 2 链接功能）。

1. 扫描全库 *.md 的 [[wikilinks]]（**先剥离代码块**，避免误匹配 Python 列表等）
2. 解析目标（去 alias/heading/block）
3. 链接分类：
   - 硬断链：笔记名链接 → 按 basename 查索引，目标不存在
   - 软缺失：日期链接 [[YYYY-MM-DD]] → 07-日记/<date>.md 不存在（Obsidian 按需创建）
   - 路径链接：[[dir/note]] → 按 vault 根解析，不存在为硬断链
4. 从 frontmatter depends 构建依赖图，DFS 检测循环依赖
只读扫描，不改盘。输出结构化报告 + 退出码。

规则源：docs/obsidian_metadata_schema.md

用法：
  python3 scripts/verify_obsidian_links.py [--vault PATH] [--json] [--max-issues N]

退出码：
  0 = 无硬断链且无循环依赖
  1 = 存在硬断链或循环依赖
  2 = 参数或依赖错误
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("✗ 缺少依赖: PyYAML (pip install pyyaml)\n")
    sys.exit(2)

DEFAULT_VAULT = "/home/jiuben/StockVault"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}
# 创作/草稿空间：其中的链接多为模板示例与待办引用，不算真实内容断链
WORKING_DIRS = {"06-模板", "00-收件箱"}
DIARY_DIR = "07-日记"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)     # 围栏代码块
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")                       # 行内代码


def strip_code(text: str) -> str:
    """移除围栏代码块与行内代码，避免 [[...]] 误匹配。"""
    text = FENCE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(" ", text)
    return text


def build_index(vault: Path) -> dict[str, Path]:
    """建立 basename→路径 索引，覆盖 .md 及 Obsidian 资源 (.base/.canvas)。"""
    idx: dict[str, Path] = {}
    for ext in ("*.md", "*.base", "*.canvas"):
        for p in vault.rglob(ext):
            if any(s in p.parts for s in SKIP_DIRS):
                continue
            idx.setdefault(p.stem, p)
    return idx


def resolve_target(raw: str, vault: Path, index: dict[str, Path]) -> Path | None:
    raw = raw.strip()
    if DATE_RE.match(raw):
        cand = vault / DIARY_DIR / f"{raw}.md"
        return cand if cand.exists() else None
    if raw in index:
        return index[raw]
    # 支持非 .md 扩展名 (如 .base / .canvas) 的 Obsidian 资源
    cand_direct = vault / raw
    if cand_direct.exists():
        return cand_direct
    stem = Path(raw).stem
    if stem in index:
        return index[stem]
    cand2 = vault / f"{raw}.md"
    if cand2.exists():
        return cand2
    cand3 = vault / f"{stem}.md"
    if cand3.exists():
        return cand3
    return None


def parse_link_target(inner: str) -> str:
    return inner.split("|", 1)[0].split("#", 1)[0].strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Obsidian 链接完整性 + 循环依赖校验")
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-issues", type=int, default=30)
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        sys.stderr.write(f"✗ vault 不存在: {vault}\n")
        return 2

    index = build_index(vault)
    hard_broken: list[dict] = []
    working_broken: list[dict] = []   # 来源在 WORKING_DIRS 的断链（模板/草稿）
    soft_date_missing: list[dict] = []
    total_links = 0
    unique_targets: set[str] = set()

    graph: dict[str, list[str]] = defaultdict(list)
    parse_errors: list[dict] = []

    files = [p for p in vault.rglob("*.md") if not any(s in p.parts for s in SKIP_DIRS)]
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        clean = strip_code(text)
        rel = str(p.relative_to(vault))

        for m in WIKILINK_RE.finditer(clean):
            inner = m.group(1)
            target = parse_link_target(inner)
            if not target:
                continue
            total_links += 1
            unique_targets.add(target)
            if DATE_RE.match(target):
                resolved = resolve_target(target, vault, index)
                if resolved is None:
                    soft_date_missing.append({"file": rel, "target": target})
            else:
                resolved = resolve_target(target, vault, index)
                if resolved is None:
                    entry = {"file": rel, "target": target, "kind": "wikilink"}
                    if rel.split("/")[0] in WORKING_DIRS:
                        working_broken.append(entry)
                    else:
                        hard_broken.append(entry)

        # depends 依赖图
        fm = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
        if fm:
            try:
                data = yaml.safe_load(fm.group(1))
            except yaml.YAMLError as e:
                parse_errors.append({"file": rel, "problem": f"YAML 错误: {e}"})
                data = None
            if isinstance(data, dict) and "depends" in data:
                deps = data["depends"]
                if isinstance(deps, list):
                    for d in deps:
                        lm = re.search(r"\[\[([^\[\]\n]+?)\]\]", str(d))
                        if lm:
                            tgt = parse_link_target(lm.group(1))
                            if tgt:
                                graph[rel].append(tgt)

    # 循环依赖检测（DFS 三色）
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cycles: list[list[str]] = []

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            target_key = next((k for k in graph if k.split("/")[-1] == nxt), None)
            if target_key is None:
                continue
            if color[target_key] == GRAY:
                idx = stack.index(target_key)
                cycles.append(stack[idx:] + [target_key])
            elif color[target_key] == WHITE:
                dfs(target_key, stack)
        stack.pop()
        color[node] = BLACK

    for start in list(graph.keys()):
        if color[start] == WHITE:
            dfs(start, [])

    seen = set()
    uniq_cycles = []
    for c in cycles:
        key = tuple(sorted(c))
        if key not in seen:
            seen.add(key)
            uniq_cycles.append(c)

    if args.json:
        import json
        print(json.dumps({
            "vault": str(vault),
            "total_files": len(files),
            "total_wikilinks": total_links,
            "unique_targets": len(unique_targets),
            "hard_broken_links": hard_broken,
            "working_space_broken_links": working_broken,
            "soft_date_missing": soft_date_missing,
            "cycles": uniq_cycles,
            "parse_errors": parse_errors,
        }, ensure_ascii=False, indent=2))
        return 1 if (hard_broken or uniq_cycles) else 0

    print(f"# Obsidian 链接完整性校验")
    print(f"vault: {vault}")
    print(f"扫描文件: {len(files)}  提取 wikilink(去代码块): {total_links}  唯一目标: {len(unique_targets)}")
    print(f"---")
    print(f"硬断链(内容模块, 笔记名不存在): {len(hard_broken)}")
    print(f"工作空间断链(模板/草稿, 非内容): {len(working_broken)}")
    print(f"软缺失(日期链接→日记未建): {len(soft_date_missing)}")
    print(f"循环依赖: {len(uniq_cycles)}")
    print(f"frontmatter 解析错误: {len(parse_errors)}")

    if hard_broken:
        print(f"\n[硬断链明细] (最多 {args.max_issues}):")
        for b in hard_broken[:args.max_issues]:
            print(f"  [{b['file']}] -> [[{b['target']}]]")

    if working_broken:
        print(f"\n[工作空间断链(模板示例/草稿引用, 非内容断链)] 共 {len(working_broken)} 例, 示例:")
        for b in working_broken[:args.max_issues]:
            print(f"  [{b['file']}] -> [[{b['target']}]]")

    if parse_errors:
        print(f"\n[解析错误] (最多 {args.max_issues}):")
        for e in parse_errors[:args.max_issues]:
            print(f"  [{e['file']}] {e['problem']}")

    if uniq_cycles:
        print(f"\n[循环依赖] (最多 {args.max_issues}):")
        for c in uniq_cycles[:args.max_issues]:
            print(f"  {' → '.join(c)}")

    if soft_date_missing:
        print(f"\n[软缺失(日期链接, 按需创建, 非硬错误)] 共 {len(soft_date_missing)} 例, 示例:")
        for b in soft_date_missing[:5]:
            print(f"  [{b['file']}] -> [[{b['target']}]]")

    print(f"---")
    if not hard_broken and not uniq_cycles:
        print(f"✓ 链接校验通过（硬断链 0；循环依赖 0；软缺失 {len(soft_date_missing)} 为按需创建）")
        return 0
    print(f"✗ 链接校验发现 {len(hard_broken)} 硬断链 / {len(uniq_cycles)} 循环依赖")
    return 1


if __name__ == "__main__":
    sys.exit(main())
