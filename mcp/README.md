# MCP Server 池 — tdx-data-feed 训练场景
===========================================

本目录定义 **3 个 MCP Server**，统一封装数据/工具接入层，供上层 Skill / Agent 调用。

## 架构
```
┌─────────────────────────────────────────────────────┐
│  L4 · Agent 编排层 (orchestrator)                    │
│       ↓ 调 skill                                     │
│  L3 · Skill 层 (yaml 注册)                           │
│       ↓ 调 MCP 工具                                  │
│  L2 · MCP 协议层 (本目录)                             │
│   ├── parquet-reader (日 K / 财务 / xdxr)            │
│   ├── calc (技术指标 / 复权因子 / 形态)               │
│   └── vault (RAG / StockVault / Obsidian)            │
│       ↓                                              │
│  L1 · 数据源 (Parquet / 计算引擎 / 向量库)             │
└─────────────────────────────────────────────────────┘
```

## 三个 Server 速览

| Server | 工具数 | 数据源 | 用途 |
|---|---|---|---|
| **parquet-reader** | 4 | data/parquet/{daily,finance,xdxr} | 读行情/财务/股本 |
| **calc** | 6 | pandas/numpy | 技术指标/复权/相似 |
| **vault** | 3 | StockVault/Obsidian + Ollama embed | 文档 RAG 检索 |

详细工具清单见 `schemas/tools.json`，协议遵循 MCP 2024-11-05 标准（JSON-RPC over stdio）。

## 启动方式

### 单个 server（stdio 模式，Claude Desktop / IDE 标准接入）
```bash
# parquet-reader
python mcp/servers/parquet_reader.py

# calc
python mcp/servers/calc.py

# vault
python mcp/servers/vault.py
```

### Claude Desktop 配置示例
```json
{
  "mcpServers": {
    "tdx-parquet": {
      "command": "python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/parquet_reader.py"]
    },
    "tdx-calc": {
      "command": "python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/calc.py"]
    },
    "tdx-vault": {
      "command": "python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/vault.py"]
    }
  }
}
```

## 开发规范

1. **每个 tool 必须**：name（动词_对象）、description（清晰功能）、inputSchema（JSON Schema）、handler（实现）
2. **错误处理**：异常抛 `McpError`，含 errno + message，不静默吞错
3. **缓存**：高频读加 LRU（如 `read_daily_kline` 加 100 项缓存）
4. **日志**：每个调用打 `[mcp:server.tool] latency=XXms` 到 `logs/mcp.log`

## 与 Skill 层配合

每个 skill 在 yaml 中按以下方式引用 MCP 工具：
```yaml
tools:
  - mcp:tdx-parquet.read_daily_kline
  - mcp:tdx-calc.calc_macd
  - mcp:tdx-vault.search
```

agent 加载 skill 时，会自动按依赖关系注入 MCP server 子进程。

## 未来扩展

- 加 `mcp-news`（实时新闻 API，目前可选接入）
- 加 `mcp-backtest`（回测引擎）
- 加 `mcp-finance-deep`（三大报表详细字段）