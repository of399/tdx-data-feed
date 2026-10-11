---
description: tdx-data-feed 项目约定。所有 Agent / Skill 强制遵守的目录结构、文件命名、提交规范、数据 schema、调度清单。
alwaysApply: true
enabled: true
updatedAt: 2026-09-27T10:35:00.000Z
---

<system_reminder>
You are working in the tdx-data-feed project. Apply these conventions strictly:

## 目录结构

| 路径 | 用途 | 备注 |
|---|---|---|
| `scripts/` | 所有 Python 入口脚本 | 单文件 < 30 KB |
| `scripts/predict.py` | 方案 D 每日预测 | 7620 只约 1 分钟 |
| `scripts/plan_e_backtest.py` | 方案 E 回测 | 支持 --compare |
| `train/` | 训练数据 / 输出 | 含 `data/` `output/` `v*/` |
| `data/parquet/daily/` | 日 K 行情 | 8035 个文件，零填充 6 位 |
| `data/adj_factor/` | 复权因子 | 5565 个文件 |
| `data/double_up/` | 翻倍样本 | 22972 行 |
| `data/predictions/` | 每日预测 | `predict_YYYYMMDD_HHMMSS.{csv,md}` |
| `data/plan_e/` | 方案 E 回测 | v1/v2 对比 |
| `models/Qwen3-14B-tdx-v*-merged/` | LoRA 合并模型 | 各 ~28 GB |
| `models/*-merged-f16.gguf` | GGUF 模型 | 各 ~29.5 GB |
| `systemd/` | systemd timer + service | user 级 |
| `.codebuddy/agents/*.md` | 项目级 Agent | 同名覆盖用户级 |
| `.codebuddy/rules/*.md` | 项目级规则 | Markdown + frontmatter |
| `.codebuddy/memory/` | 跨会话记忆 | 每日追加 + MEMORY.md |
| `.codebuddy/skills/` | 项目级技能 | SKILL.md 格式 |
| `deploy/` | 容器化配置 | Dockerfile / docker-compose.yml |
| `docs/` | 设计文档 | PLAN_E.md / WORKFLOW.md |

## 命名规范

- **股票代码**：6 位数字，零填充（`600519` 而非 `6005`）
- **北交所**：前缀 `920XXX` 或 `bj920XXX`
- **文件名日期**：`YYYYMMDD` 或 `YYYYMMDD_HHMMSS`（按精度递增）
- **Agent 名**：`kebab-case`（如 `tdx-data-collector`）
- **Skill 名**：`kebab-case`（如 `tdx-quant-workflow`）

## 提交规范

- `feat:` 新功能 / 新 Agent
- `fix:` Bug 修复
- `docs:` 文档 / README
- `chore:` 配置 / 依赖
- `data:` 数据集更新（不提交 parquet，仅更下载脚本）
- `model:` 训练输出（不提交 GGUF）

## 模型命名约定

- `qwen3-14b-tdx-v{1,2,...}` — 训练版本
- `Qwen3-14B-tdx-v{N}-merged` — 合并后
- `Qwen3-14B-tdx-v{N}-merged-f16.gguf` — GGUF
- `Modelfile-v{N}` — Ollama 配置

## 错误处理规范

- **不删除**用户数据（`data/` `models/`）除非明确指令
- **大文件**（GGUF / parquet）不提交到 git
- **日志**写入 `logs/` 目录，`max-size: 20m, max-file: 5`
- **失败重试**：3 次指数退避（1s / 4s / 9s）

## 调度规范

| Timer | 时间 | 动作 |
|---|---|---|
| `tdxfeed-sync.timer` | 18:00 daily | 数据同步 |
| `predict-daily.timer` | 18:30 daily | 全市场预测 |
| `plan-e-journal.timer` | 18:35 daily | 写实盘 journal |
| `plan-e-weekly.timer` | 周五 20:00 | 周报汇总 |
| `canary-health.timer` | 5 min | 健康检查 |

## 代码风格

- **Python**：3.14+ · 4 空格缩进 · PEP 8
- **类型注解**：必填（核心函数）
- **文档字符串**：Google 风格
- **异常处理**：捕获具体类型，不裸 `except`
- **日志**：用 `logging`，不 print 裸 `print`
- **路径**：用 `pathlib.Path`，不裸字符串拼接

## 验证清单（提交前）

1. `make canary` 通过（6 项健康检查）
2. `make lint` 无错误
3. `make status` 所有 timer 服务正常
4. 文档更新（如有 API 变化）
5. 数据 schema 兼容（如有字段变更）
</system_reminder>
