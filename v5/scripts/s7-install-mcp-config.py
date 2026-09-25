#!/usr/bin/env python3
"""
v5-VE1-M2 · S7 CodeBuddy MCP 配置合并脚本

把 v5/scripts/s7-mcp-servers.json 合并到 CodeBuddy 的 settings.json：
  - 备份原文件（settings.json.bak.<timestamp>）
  - 合并 mcpServers 到 mcp.servers 字段（已存在则覆盖）
  - 移除带 _comment / _comment_2 等下划线前缀的辅助字段
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SETTINGS_PATH = Path.home() / ".config/CodeBuddy CN/User/settings.json"
SNIPPET_PATH = Path("/home/jiuben/tdx-data-feed/v5/scripts/s7-mcp-servers.json")


def strip_underscore_keys(obj):
    """递归移除所有 _ 开头 key（注释字段）"""
    if isinstance(obj, dict):
        return {k: strip_underscore_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_underscore_keys(x) for x in obj]
    return obj


def main():
    if not SNIPPET_PATH.exists():
        print(f"❌ 缺失片段文件：{SNIPPET_PATH}", file=sys.stderr)
        sys.exit(1)

    # 读片段
    snippet_raw = json.loads(SNIPPET_PATH.read_text())
    snippet_clean = strip_underscore_keys(snippet_raw)
    servers = snippet_clean.get("mcpServers", {})
    if not servers:
        print("❌ 片段中无 mcpServers", file=sys.stderr)
        sys.exit(1)

    # 读 settings.json
    if SETTINGS_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = SETTINGS_PATH.with_suffix(f".bak.{ts}")
        shutil.copy2(SETTINGS_PATH, backup)
        print(f"[S7] 备份：{backup}")
        settings = json.loads(SETTINGS_PATH.read_text())
    else:
        settings = {}

    # 合并 mcp.servers
    settings.setdefault("mcp", {})
    settings["mcp"].setdefault("servers", {})
    existing = settings["mcp"]["servers"]

    added, updated = [], []
    for name, cfg in servers.items():
        existing[name] = cfg
        if name in settings["mcp"].get("servers", {}):
            updated.append(name)
        else:
            added.append(name)

    # 写回
    SETTINGS_PATH.write_text(json.dumps(settings, indent=4, ensure_ascii=False))
    print(f"[S7] 写入：{SETTINGS_PATH}")
    print(f"[S7] 新增：{added or '无'}")
    print(f"[S7] 更新：{updated or '无'}")
    print(f"[S7] 总计 servers：{len(existing)} 个")
    print()
    print("[S7] 下一步：重启 CodeBuddy 使 MCP 配置生效")


if __name__ == "__main__":
    main()
