# tdx-data-feed

> A 股 / 北交所数据采集 + 量化回测 + 告警链路完整工具链
> 维护者：jiuben · 创建：2026-09-25

## 概述

本项目是 `tdx-data-feed` v5 的核心代码仓，覆盖：

1. **数据采集**：`tdxfeed/` — A 股 / 北交所日线、财务、复权因子、分钟线（akshare + 自建源）
2. **数学 MCP**：`v5/mcp_math/` + `v5/mcp_servers/` — sympy + ask 策略路由 + LLM 兜底 (qwen3-14b-tdx-fast)
3. **告警链路**：`v5/tests/test_alerting_pipeline.py` + `v5/scripts/sop-weekly.sh` — prom + alertmanager + webhook sink 全链路
4. **量化回测**：`agents/` + `training/` + `scripts/` — 多 Agent 框架 + K 线特征 + LoRA 微调

## 快速开始

```bash
# 安装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 启动业务
bash scripts/start.sh

# 跑链路验证 (quick, 5s)
bash v5/tests/test_alerting_pipeline.sh

# 跑 lint
ruff check .
```

## 目录结构

| 目录 | 用途 |
|---|---|
| `tdxfeed/` | 数据采集层 (akshare / 自建 HTTP 源 / parquet 落盘) |
| `v5/mcp_math/` | 数学计算核心 (sympy 策略路由 + ask + LLM 兜底) |
| `v5/mcp_servers/` | MCP server 实现 (math_sympy_http.py: 8002 端口) |
| `v5/tests/` | pytest 集成测试 (告警链路 / ask 路径) |
| `v5/scripts/` | 长跑脚本 (sop-weekly / scheduler_agent / daily_evolution) |
| `v5/audit/` | 决策审计 + 故障复盘报告 |
| `v5/docs/` | SOP 文档 (cron 接入 / 告警 / LoRA 训练) |
| `agents/` | 多 Agent 框架 (CLI + runtime) |
| `scripts/` | 数据下载 + 因子计算 + 离线分析 |
| `mcp/` | MCP server 实现 (vault / parquet_reader / calc) |
| `training/` | LoRA 训练监控 + eval |
| `tests/` | 旧版 pytest 测试 |

## 关键 SOP

- **告警链路验证**：`v5/docs/cron-sop.md` + `v5/scripts/sop-weekly.sh`（已接入 cron `0 9 * * 1`）
- **BLE001 / lint 策略**：`v5/audit/2026-09-25-task-{2,4}-*.md`
- **vLLM 量化探索**：`v5/audit/2026-09-21-vllm-quantize-exploration.md`

## CI

- `.github/workflows/lint.yml` — ruff check + format check
- `.github/workflows/sop-quick.yml` — quick SOP 链路验证 (周一 01:00 UTC 触发)

## 许可

私有项目，未经授权不得使用 / 转载。