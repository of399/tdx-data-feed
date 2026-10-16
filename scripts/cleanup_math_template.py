#!/usr/bin/env python3
"""cleanup_math_template.py (一次性清理)

移除数学笔记上被 fix_obsidian_metadata.py 误加的 `模板` 字段。
数学 schema（verify_obsidian_metadata.py）不含 `模板`，该字段非数学元数据契约。
幂等：无 模板 字段则跳过。

用法：python3 scripts/cleanup_math_template.py [--vault PATH]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("✗ 缺少 PyYAML\n")
    sys.exit(2)

DEFAULT_VAULT = "/home/jiuben/StockVault"
SKIP = {".obsidian", ".trash", ".git", "node_modules", ".codebuddy", "attachments"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    args = ap.parse_args()
    vault = Path(args.vault)
    n = 0
    for p in vault.rglob("*.md"):
        if any(s in p.parts for s in SKIP):
            continue
        if "数学" not in p.parts:
            continue
        if p.name == "00-待写队列.md":
            continue  # 自建 MOC，保留现状
        t = p.read_text(encoding="utf-8", errors="replace")
        if not t.startswith("---"):
            continue
        m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", t, re.DOTALL)
        if not m:
            continue
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        if "模板" in data:
            del data["模板"]
            body = re.sub(r"^---\s*\n.*?\n---\s*(?:\n|$)", "", t, count=1, flags=re.DOTALL)
            p.write_text(
                "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip("\n") + "\n---\n" + body,
                encoding="utf-8",
            )
            n += 1
    print(f"已移除数学笔记误加 模板 字段: {n} 个")


if __name__ == "__main__":
    main()
