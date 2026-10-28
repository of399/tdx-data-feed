# MCP Server 池 — tdx-data-feed 训练场景
===========================================

本目录定义 **6 个 MCP Server**，统一封装数据/工具接入层，供上层 Skill / Agent 调用。

> ⚠️ `skill_router/runtime.py` 的 `SERVER_SCRIPT_MAP` 与 `skills/*.yaml` 按 `mcp/servers/` 硬编码引用本目录，移动或归档会导致 YAML 技能链启动失败。

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
│   ├── vault (RAG / StockVault / Obsidian)            │
│   ├── news (个股新闻 / 公告 / 市场要闻)               │
│   ├── finance-deep (三大报表 / 指标扩展)              │
│   └── backtest (内置策略 / 显式信号回测 / 打板 Top-N)  │
│       ↓                                              │
│  L1 · 数据源 (Parquet / 计算引擎 / 向量库 / akshare)   │
└─────────────────────────────────────────────────────┘
```

## 六个 Server 速览

| Server | 工具数 | 数据源 | 用途 |
|---|---|---|---|
| **parquet-reader** | 4 | data/parquet/{daily,finance,xdxr} | 读行情/财务/股本 |
| **calc** | 6 | pandas/numpy | 技术指标/复权 |
| **vault** | 3 | data/vault_store.jsonl + 可选 Ollama embed | 文档检索（向量 / lexical 兜底，支持目录过滤）|
| **news** | 3 | akshare（东财 / 巨潮 / 财新）| 实时新闻、公告、市场要闻 |
| **finance-deep** | 3 | finance.parquet + akshare + data/parquet/finance_deep 缓存 | 三大报表与扩展指标 |
| **backtest** | 4 | data/parquet/daily + data/adj_factor + daban 模型 | 单股策略回测 / 信号验证 / 打板 Top-N 选股 |

合计 23 个工具，详细清单见 `schemas/tools.json`，协议遵循 MCP 2024-11-05 标准（JSON-RPC over stdio）。

> news 依赖外网与 akshare，接口不可用时返回错误而非编造数据；finance-deep 对非核心标的首次查询会联网拉取并落盘缓存 7 天；backtest 使用前复权价格，信号收盘触发、次日开盘成交，买卖均计佣金 2.5bps + 滑点 5bps，卖出另计印花税 5bps。

## Vault 索引

vault 检索依赖 `data/vault_store.jsonl`。首次使用或笔记更新后重建索引：

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python scripts/build_vault_index.py            # 词法索引（默认，无需 Ollama）
venv/bin/python scripts/build_vault_index.py --embed    # 附加 nomic-embed-text 向量（较慢）
```

当前索引：6,923 chunks / 5,668 notes（2026-10-21 接入财经七书讲义后重建）。

`--embed` 生成的向量不写进 JSONL，而是落成同名 sidecar：`data/vault_store.vectors.npy`（float32，n×768）+ `data/vault_store.vectors.json`（清单：`model`/`dim`/`n`/`rows_hash`/`built_at`/`elapsed_sec`）。原因是内联向量会把索引从 8MB 撑到 ~80MB，而每次 MCP 调用都要重新读一遍。加载时四道校验（numpy 缺失、sidecar 缺失、行数不一致、`rows_hash` 不匹配）任一失败都退回词法并带 `hint`，绝不会在错位的行上算相似度。

`search` 返回 `mode` 标明实际走的通道：`vector`（附 `embed: {model, dim}`）/ `lexical`（附 `hint`）/ `no_match` / `empty`。索引里也收 vault 下的 PDF（PyMuPDF 逐页取文，正文带 `〔p页码〕` 标记），`--no-pdf` 可跳过。

`search` 支持三个可选过滤参数，用于在多个目录之间隔离检索范围：

| 参数 | 类型 | 作用 |
|---|---|---|
| `category` | string | 只检索该一级目录（笔记相对路径的首段），如 `09-理论讲义` |
| `path_prefix` | string | 只检索该路径前缀下的笔记，如 `09-理论讲义/06-计量经济学` |
| `exclude_category` | string \| string[] | 排除指定一级目录，个股/新闻类检索用它避开讲义正文 |

返回体含 `n_matched`（过滤后参与打分的文档数）与 `filter`（回显过滤条件）；过滤后为空时返回 `mode=no_match` + `hint`，不会静默退化成全库检索。

讲义笔记由 `scripts/import_theory_notes.py` 从讲义 md 拆分生成，落在 `/home/jiuben/ObsidianVaults/tdx-data-feed/09-理论讲义/`（75 篇），改完源文件重跑该脚本再重建索引即可。笔记 frontmatter 里的 `related_skills` 会被索引进 `meta`，`search` 的每条结果都原样带出，调用方据此把理论接到实盘技能上。

## 启动方式

### 单个 server（stdio 模式，Claude Desktop / IDE 标准接入）
```bash
# parquet-reader
venv/bin/python mcp/servers/parquet_reader.py

# calc
venv/bin/python mcp/servers/calc.py

# vault
venv/bin/python mcp/servers/vault.py

# news
venv/bin/python mcp/servers/news.py

# finance-deep
venv/bin/python mcp/servers/finance_deep.py

# backtest
venv/bin/python mcp/servers/backtest.py
```

### Claude Desktop 配置示例
```json
{
  "mcpServers": {
    "tdx-parquet": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/parquet_reader.py"]
    },
    "tdx-calc": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/calc.py"]
    },
    "tdx-vault": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/vault.py"]
    },
    "tdx-news": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/news.py"]
    },
    "tdx-finance-deep": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/finance_deep.py"]
    },
    "tdx-backtest": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/mcp/servers/backtest.py"]
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
  - mcp:tdx-parquet-reader.read_daily_kline
  - mcp:tdx-calc.calc_macd
  - mcp:tdx-vault.search
```

agent 加载 skill 时，会自动按依赖关系注入 MCP server 子进程。

## 扩展记录（2026-10-21）

原「未来扩展」三项已落地：

- `mcp-news` → `tdx-news`：东财个股新闻、巨潮公告、财新市场要闻；5 分钟内存缓存，失败直接报错。
- `mcp-backtest` → `tdx-backtest`：5 个内置策略 + 显式信号回测；前复权、T+1、费用滑点可配。
- `tdx-backtest.daban_top_picks`（2026-10-22）：全市场 29 维 daban 模型打分，输出 T+1 收盘涨停概率 Top-N；剔除 T 日开盘即涨停（买不进）样本，可选剔除 T 日已涨停 / ST / 低流动性。
- `mcp-finance-deep` → `tdx-finance-deep`：三大报表全字段 + 指标查询；核心 300 走本地，其余按需拉取并缓存 7 天。

后续候选：新闻情绪打分、组合级回测、财报 PDF 解析入库。
