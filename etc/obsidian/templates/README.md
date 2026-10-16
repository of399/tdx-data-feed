# 非数学模板源（v1）

5 个非数学模板的**单一事实源**。部署位置为 `StockVault/06-模板/`，由本仓库源-部署零漂移闭环管理。

## 模板清单

| 源文件 | 部署路径（默认） | 用途 |
|---|---|---|
| `00-MOC.md` | `06-模板/00-MOC.md` | 模板目录的 MOC |
| `个股研究模板.md` | `06-模板/个股研究模板.md` | 个股研究笔记 |
| `回测实验模板.md` | `06-模板/回测实验模板.md` | 回测实验笔记 |
| `日记模板.md` | `06-模板/日记模板.md` | 日常研究日志 |
| `v4技术面分析模板.md` | `06-模板/v4技术面分析模板.md` | v4 技术面分析 |

## 同步流程

1. 改源（本目录）
2. 跑 `bash scripts/install-obsidian-templates.sh --vault /home/jiuben/StockVault`（或 `make install-obsidian-templates`）
3. 跑 `make verify-obsidian-templates` 确认零漂移

## 与数学模板的关系

- 数学 7 模板 + MOC 源：`etc/obsidian/math/templates/` 与 `etc/obsidian/math/MOC/总览.md`
- 非数学 5 模板源：`etc/obsidian/templates/`
- verify 脚本 v2 同时覆盖两类源 + 5 份配置 JSON
- 旧名 `verify-math-templates` 已 alias 为 `verify-obsidian-templates`
