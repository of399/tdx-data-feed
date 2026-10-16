#!/usr/bin/env python3
"""link_cross_vault.py (Phase 2.4 跨 vault 双向互链)

在 StockVault 与项目库(ObsidianVaults/tdx-data-feed) 关键笔记间建立
obsidian:// URI 互链（原生可用、无需插件）：
  - StockVault 笔记加 frontmatter 字段 related_project_vault
  - 项目库笔记加 frontmatter 字段 related_stockvault
  - 两库各生成 00-跨库索引.md MOC
幂等；项目库为 sync 自动生成，重跑脚本即可重建（建议接 sync 钩子）。

用法：python3 scripts/link_cross_vault.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("✗ 缺少 PyYAML (pip install pyyaml)\n")
    sys.exit(2)

STOCK = Path("/home/jiuben/StockVault")
PROJ = Path("/home/jiuben/ObsidianVaults/tdx-data-feed")
SKIP = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}

# StockVault 相对路径 -> 项目库相对路径(含 .md)
LINKS = [
    ("02-框架/朝堂制量化投研体系.md", "02-架构与工作流/ARCHITECTURE-架构决策.md"),
    ("02-框架/v4模型.md", "04-策略与方案/PLAN_E-翻倍当日跟随策略.md"),
    ("02-框架/系统学方法论-朝堂制.md", "02-架构与工作流/WORKFLOW-工作流全景.md"),
    ("02-框架/Agent体系与系统学.md", "02-架构与工作流/ARCHITECTURE-架构决策.md"),
    ("04-回测/沪深300动量回测.md", "04-策略与方案/PLAN_E-翻倍当日跟随策略.md"),
    ("04-回测/双均线趋势回测.md", "04-策略与方案/PLAN_E-翻倍当日跟随策略.md"),
    ("03-数据/数据字典.md", "05-数据与配置/数据字典.md"),
    ("05-知识库/数学/00_索引/总览.md", "02-架构与工作流/WORKFLOW-工作流全景.md"),
    ("05-知识库/00-MOC.md", "01-项目概览/README.md"),
    ("05-知识库/模型蒸馏与LoRA训练-2026-09-26.md", "04-策略与方案/README.md"),
    ("00-收件箱/00-收件箱.md", "01-项目概览/README.md"),
]


def uri(vault: str, rel_no_ext: str) -> str:
    p = urllib.parse.quote(rel_no_ext, safe="/")
    return f"obsidian://open?vault={vault}&file={p}"


def parse_fm(text: str):
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None


def set_field(path: Path, field: str, value: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    data = parse_fm(text)
    if data is None:
        return False
    if data.get(field) == value:
        return False
    data[field] = value
    body = re.sub(r"^---\s*\n.*?\n---\s*(?:\n|$)", "", text, count=1, flags=re.DOTALL)
    path.write_text(
        "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip("\n") + "\n---\n" + body,
        encoding="utf-8",
    )
    return True


def build_moc(title: str, mapping: dict[str, str]) -> str:
    out = [
        f"# 跨库索引（{title}）",
        "",
        "> 由 link_cross_vault.py 生成。obsidian:// URI 原生可用（Obsidian 中点击即跳转另一 vault）。",
        "> 注：项目库为 sync 自动生成，重跑本脚本可重建本文件。",
        "",
        "## 互链清单",
        "",
    ]
    for k, v in mapping.items():
        out.append(f"- [{k}]({v})")
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="跨 vault 双向互链")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stock_uris: dict[str, str] = {}
    proj_uris: dict[str, str] = {}
    n_stock = 0
    n_proj = 0

    for s, p in LINKS:
        sp = STOCK / s
        pp = PROJ / p
        if not sp.exists():
            print(f"  ! StockVault 缺失: {s}")
            continue
        su = uri("tdx-data-feed", p[:-3])  # 去 .md
        pu = uri("StockVault", s[:-3])
        stock_uris[s] = su
        proj_uris[p] = pu
        if args.dry_run:
            if sp.exists():
                n_stock += 1
            if pp.exists():
                n_proj += 1
        else:
            if set_field(sp, "related_project_vault", su):
                n_stock += 1
            if pp.exists() and set_field(pp, "related_stockvault", pu):
                n_proj += 1

    if not args.dry_run:
        (STOCK / "00-跨库索引.md").write_text(
            build_moc("StockVault → 项目库 (tdx-data-feed)", stock_uris), encoding="utf-8"
        )
        (PROJ / "00-跨库索引.md").write_text(
            build_moc("项目库 (tdx-data-feed) → StockVault", proj_uris), encoding="utf-8"
        )

    print("# 跨 vault 互链")
    print(f"StockVault 加 related_project_vault: {n_stock}")
    print(f"项目库 加 related_stockvault: {n_proj}")
    print("MOC: StockVault/00-跨库索引.md, 项目库/00-跨库索引.md")


if __name__ == "__main__":
    main()
