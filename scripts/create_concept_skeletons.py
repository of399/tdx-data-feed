#!/usr/bin/env python3
"""create_concept_skeletons.py (Phase 2 待写队列)

为链接断链检测出的真实数学概念（非模板占位符）批量创建占位骨架，
并生成 05-知识库/数学/00-待写队列.md MOC。同时修正 1 处路径式断链
[[数学/00_索引/总览]] → [[总览]]。
幂等：已存在的骨架不覆盖；MOC 每次重写（清单稳定）。

用法：python3 scripts/create_concept_skeletons.py [--vault PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

DEFAULT_VAULT = "/home/jiuben/StockVault"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}
MATH_ROOT = Path("05-知识库/数学")  # 相对 vault

# (概念名, 领域目录, 领域标签)
CONCEPTS = [
    ("协方差矩阵", "16_数值分析", "数值分析"),
    ("正定矩阵", "16_数值分析", "数值分析"),
    ("Ledoit-Wolf 协方差收缩的反例", "16_数值分析", "数值分析"),
    ("ERC 风险平价与整手约束的反例", "16_数值分析", "数值分析"),
    ("凸优化", "15_优化理论", "优化理论"),
    ("测度", "10_实分析", "实分析"),
    ("勒贝格积分", "10_实分析", "实分析"),
    ("收敛定理", "10_实分析", "实分析"),
    ("拉东-尼科迪姆定理", "10_实分析", "实分析"),
    ("多重假设检验", "14_概率统计", "概率统计"),
    ("p 值", "14_概率统计", "概率统计"),
    ("BH-FDR 多重检验校正的证明", "14_概率统计", "概率统计"),
    ("BH-FDR 多重检验校正的反例", "14_概率统计", "概率统计"),
    ("自相关", "14_概率统计", "概率统计"),
    ("OLS 渐近方差", "14_概率统计", "概率统计"),
    ("Newey-West HAC 标准误的反例", "14_概率统计", "概率统计"),
    ("经验分布", "14_概率统计", "概率统计"),
    ("Bootstrap 假设检验的反例", "14_概率统计", "概率统计"),
    ("夏普比率", "14_概率统计", "概率统计"),
    ("Deflated Sharpe Ratio 的反例", "14_概率统计", "概率统计"),
]

TODAY = date.today().isoformat()


def skeleton(name: str, domain_label: str) -> str:
    return f"""---
title: {name}
type: concept
domain: {domain_label}
level: 1
status: skeleton
tags: [math-notes, {domain_label}]
created: {TODAY}
depends: []
tdx_link: ""
next_review: 2099-12-31
---

# {name}

> 占位骨架：由链接断链检测（verify_obsidian_links.py）触发创建，待撰写。

## 定义

## 与量化投研的关联

- [[朝堂制量化投研体系]]
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="数学概念占位骨架批量创建")
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    vault = Path(args.vault)
    created: list[str] = []
    skipped: list[str] = []

    for name, dom_dir, dom_label in CONCEPTS:
        fp = vault / MATH_ROOT / dom_dir / f"{name}.md"
        if fp.exists():
            skipped.append(str(fp.relative_to(vault)))
            continue
        if args.dry_run:
            created.append("(dry) " + str(fp.relative_to(vault)))
            continue
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(skeleton(name, dom_label), encoding="utf-8")
        created.append(str(fp.relative_to(vault)))

    # MOC：先逐条 checklist，再 dataview 动态查询
    lines = [
        "# 数学概念待写队列",
        "",
        "> 由链接断链检测生成的占位骨架（status: skeleton）。完成撰写后移除对应条目。",
        "",
        "## 待写清单（逐条）",
        "",
    ]
    for name, _dom_dir, dom_label in CONCEPTS:
        lines.append(f"- [ ] [[{name}]] — {dom_label}")
    lines += [
        "",
        "## Dataview 动态查询",
        "",
        "```dataview",
        'TABLE domain AS 领域, status AS 状态 FROM #math-notes WHERE status = "skeleton" SORT file.name',
        "```",
        "",
    ]
    if not args.dry_run:
        (vault / MATH_ROOT / "00-待写队列.md").write_text("\n".join(lines), encoding="utf-8")

    # 修正路径式断链（兼容别名 [[数学/00_索引/总览|xxx]]）
    moc_link = vault / "05-知识库" / "00-MOC.md"
    fixed_link = None
    if moc_link.exists():
        t = moc_link.read_text(encoding="utf-8")
        if "[[数学/00_索引/总览" in t:
            if not args.dry_run:
                moc_link.write_text(t.replace("[[数学/00_索引/总览", "[[总览"), encoding="utf-8")
            fixed_link = str(moc_link.relative_to(vault))

    print("# 概念骨架批量创建")
    print(f"创建骨架: {len([c for c in created if not c.startswith('(dry)')])}  跳过(已存在): {len(skipped)}")
    for c in created:
        print(f"  + {c}")
    for s in skipped:
        print(f"  = {s}")
    print(f"MOC: {(vault / MATH_ROOT / '00-待写队列.md').relative_to(vault)}")
    if fixed_link:
        print(f"修正路径式断链: {fixed_link}  [[数学/00_索引/总览]]→[[总览]]")


if __name__ == "__main__":
    main()
