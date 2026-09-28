# Changelog · tdx-data-feed

> 所有重要变更记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

### Planned · 计划中

- v8 LoRA fusion（rank 对齐后）
- 方案 D predict.py 集成定时任务
- 方案 E 实盘 1 个月跟踪
- canary 异常自动告警（邮件 / 微信）
- GitHub Pages 文档站点
- ...

---

## [1.0.0] - 2026-09-27

### 🎉 首次完整发布

#### Added · 新增

**📚 完整文档体系（12 个文档 · 6016 行 · ~230KB）**
- `README.md`（657 行）— 系统总览 + 入门
- `CONTRIBUTING.md`（667 行）— 贡献指南
- `SECURITY.md`（769 行）— 安全策略 + 漏洞流程
- `DELIVERY.md`（630 行）— 交付报告
- `Makefile`（497 行 · 85 个目标）— 整合所有操作
- `docs/ARCHITECTURE.md`（650 行）— 10 个 ADR
- `docs/API.md`（902 行）— 13 个 OpenAPI 接口
- `docs/WORKFLOW.md`（473 行）— 工作流全景
- `docs/PLAN_E.md`（230 行）— 方案 E 策略
- `docs/PLAN_E_LIVE.md`（307 行）— 实盘启动指南
- `docs/tdx-data-feed-数据字典.md`（101 行）
- `docs/volume-单位治理计划书.md`（133 行）

**🤖 模型训练与部署**
- v5（rank=8, 12656 样本, eval_loss=0.1958）
- v6（rank=16, 5238 样本, eval_loss=0.2883）
- **v7**（rank=8, 8000 样本, eval_loss=**0.1914** · v5 续训）
- v4 fast baseline（eval_loss=0.2300）
- 全部部署到 Ollama（4 个模型）

**📈 量化策略**
- 方案 C：22972 翻倍样本
- 方案 C v2：前复权对比（v1 假阳性 4.5%）
- 方案 D：LightGBM 信号（AUC 0.8144，18 维特征）
- **方案 E**：翻倍当日跟随（v2 胜率 55.6%，盈亏比 3.00，期望收益 +12.22%）

**🛠️ 核心脚本**
- `scripts/predict.py`（18KB）— 方案 D 每日预测（7620 只）
- `scripts/plan_e_backtest.py`（16KB）— 方案 E 回测
- `scripts/plan_e_journal.py`（16KB）— 6 模式实盘跟踪
- `scripts/canary_health_check.py`（6KB）— 6 项健康检查
- `scripts/sync-to-obsidian.sh`（4KB）— 文档同步工具

**⏰ 自动化调度（5 个 systemd timer）**
- `canary-health.timer` — 每 5 分钟
- `tdxfeed-sync.timer` — 工作日 18:00
- `predict-daily.timer` — 工作日 18:05
- `plan-e-journal.timer` — 工作日 18:10
- `plan-e-weekly.timer` — 周日 20:00

**🔄 CI/CD 与同步**
- `.github/workflows/lint.yml` — ruff check
- `.github/workflows/quick-sop.yml` — pytest
- `.github/workflows/sync-docs.yml` — 文档自动同步
- `.github/ISSUE_TEMPLATE/` — 5 类 Issue 模板
- `PULL_REQUEST_TEMPLATE.md` — PR 模板

**📚 Obsidian 知识库**
- `/home/jiuben/ObsidianVaults/tdx-data-feed/`
- 21 个 Markdown · 6795 行 · 328KB
- 含 dataview 索引、CHANGELOG、双链

#### Fixed · 修复

- GGUF 转换 DFLASH 错误（升级 gguf ≥ 0.19）
- Modelfile stop token（v7 必须 `repeat_penalty 1.0`）
- predict.py `KeyError: 'adj_factor'`（先 drop 再 merge）
- plan_e 触发日期字段（从 signal_date 改 peak_date）
- vllm-venv 路径（直接用 venv/bin/python）
- llama.cpp 多个 broken 文件（从 zip 覆盖）

#### Security · 安全

- SECURITY.md 首次发布（含应急响应 Runbook）
- 文件权限分级（交易记录 600，模型权重 644）
- Ollama 仅监听 localhost
- canary 6 项检查覆盖 ollama / 数据 / GPU / timer / 磁盘

---

## [0.9.0] - 2026-09-22

### Added

- v5 训练完成（12656 样本，1188 steps）
- v6 训练完成（5238 样本，984 steps）
- v5 + v6 部署到 Ollama
- Modelfile 模板标准化

### Fixed

- merge_and_unload OOM（加 `device_map="cpu", low_cpu_mem_usage=True`）

---

## [0.5.0] - 2026-09-15

### Added

- **数据采集**：
  - baostock 接入（沪深日 K + 复权因子）
  - akshare 接入（北交所日 K + 复权因子）
  - 通联数据接入（股票清单）
- **数据规模**：
  - 8035 个日 K parquet
  - 5565 个复权因子 parquet
- **翻倍样本**：方案 C v1（22972 行）

---

## [0.1.0] - 2026-09-01

### Added

- 项目初始化
- 基础目录结构（`tdxfeed/`、`scripts/`、`data/`、`train/`、`v5/`）
- README.md v1
- Makefile v1
- requirements.txt

---

## 📋 格式说明

本 Changelog 遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范：

| 类型 | 含义 |
|---|---|
| **Added** | 新增功能 |
| **Changed** | 功能变更 |
| **Deprecated** | 即将移除（保留过渡期）|
| **Removed** | 已移除 |
| **Fixed** | Bug 修复 |
| **Security** | 安全修复 / 加固 |

## 🏷️ 版本号规则

`MAJOR.MINOR.PATCH`：

- **MAJOR**：不兼容的 API 变更
- **MINOR**：向下兼容的功能新增
- **PATCH**：向下兼容的 Bug 修复

## 🔗 相关文档

- [README.md](README.md) — 系统总览
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构决策
- [WORKFLOW.md](docs/WORKFLOW.md) — 工作流
- [API.md](docs/API.md) — 接口文档
- [SECURITY.md](SECURITY.md) — 安全策略
- [CONTRIBUTING.md](CONTRIBUTING.md) — 贡献指南

---

## 🤖 自动化

- **GitHub Actions** `.github/workflows/sync-docs.yml`：
  - push 到 main / develop 触发同步
  - PR 触发预览评论
  - 每天 UTC 0 点定时同步
- **同步脚本** `scripts/sync-to-obsidian.sh`：
  - 手动 / dry-run / watch 三种模式
  - 哈希追踪源文件变更
  - YAML frontmatter + 自动标签

---

> 最后更新：2026-09-27
> 维护者：jiuben
> 当前版本：**1.0.0**
> 下个版本（计划）：1.1.0 — v7 完整实盘 + canary 告警