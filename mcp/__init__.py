#!/usr/bin/env python3
"""tdx-data-feed MCP 服务包（stdio JSON-RPC over stdio）。

子模块：
    servers/  : 各 MCP server 实现（parquet_reader / news / backtest / ...）
    schema_validation: 入站/出站结构化校验（P1-5）
"""

# 与 pip 安装的 mcp SDK 共存：合并子模块搜索路径（server/types 来自 SDK，
# servers/schema_validation 来自本仓库）。缺此一行时本包会整体遮蔽 SDK，
# CI（requirements-ci 装 mcp>=1.2,<2）里 mcp.server 不可达 → 收集错误。
__path__ = __import__("pkgutil").extend_path(__path__, __name__)
