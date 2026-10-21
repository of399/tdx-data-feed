# CONTRIBUTING.md · tdx-data-feed 贡献指南

> 欢迎贡献 tdx-data-feed！本文档说明开发环境搭建、代码风格、提交规范、PR 流程、社区行为准则。
>
> 最后更新：2026-09-27 · 维护者：jiuben

---

## 目录

- [1. 开发环境搭建](#1-开发环境搭建)
- [2. 代码风格规范](#2-代码风格规范)
- [3. 提交规范](#3-提交规范)
- [4. 分支管理策略](#4-分支管理策略)
- [5. Issue 提交要求](#5-issue-提交要求)
- [6. Pull Request 流程](#6-pull-request-流程)
- [7. 评审标准与合并条件](#7-评审标准与合并条件)
- [8. 冲突处理](#8-冲突处理)
- [9. 社区行为准则](#9-社区行为准则)

---

## 1. 开发环境搭建

### 1.1 系统要求

| 项目 | 要求 |
|---|---|
| OS | Ubuntu 22.04+（systemd user timer 需要）|
| Python | 3.14+ |
| 内存 | 16GB+（32GB 推荐）|
| 磁盘 | 200GB+ |
| GPU | NVIDIA 16GB+（仅训练需要）|

### 1.2 完整搭建步骤

```bash
# 1. 克隆仓库
git clone https://github.com/jiuben/tdx-data-feed.git
cd tdx-data-feed

# 2. 创建虚拟环境
python3.14 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install --upgrade pip
pip install -r requirements.txt
pip install lightgbm llamafactory peft

# 4. 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &

# 5. 安装 llama.cpp + gguf
mkdir -p ~/llama.cpp
pip install --upgrade gguf

# 6. 数据初始化（首次）
python scripts/download_universe.py
python -m tdxfeed.cli sync
python scripts/download_adj_factor_b.py

# 7. 部署模型（可选）
bash v5/scripts/deploy_v5.sh
bash v5/scripts/deploy_v6.sh

# 8. 启用 systemd 自动化
cp systemd/*.{service,timer,path} ~/.config/systemd/user/
systemctl --user daemon-reload
for t in tdxfeed-sync predict-daily plan-e-journal plan-e-weekly canary-health; do
    systemctl --user enable --now $t.timer
done
systemctl --user enable --now obsidian-sync.path

# 9. 验证
make help    # 查看 Makefile 目标
make lint    # ruff 检查
make test    # pytest
ollama list  # 模型列表
```

### 1.3 IDE 推荐

- **VSCode**：装 Python + Ruff + Mypy + YAML 扩展
- **PyCharm Professional**：内置 ruff / mypy 集成

---

## 2. 代码风格规范

### 2.1 Python 代码风格

**遵循 PEP 8 + ruff 默认规则**。

```toml
# ruff.toml
line-length = 100
target-version = "py314"

[lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "C4", "SIM", "BLE"]
ignore = ["E501"]  # 行长度由 formatter 控制
```

### 2.2 命名约定

| 类型 | 风格 | 示例 |
|---|---|---|
| 模块名 | snake_case | `find_double_up_c.py` |
| 类名 | PascalCase | `class FeatureExtractor:` |
| 函数名 | snake_case | `def compute_features():` |
| 变量名 | snake_case | `n_stocks = 5235` |
| 常量 | UPPER_SNAKE | `INITIAL_CAPITAL = 100000` |
| 私有函数 | _leading_underscore | `def _validate():` |
| 私有变量 | _leading_underscore | `self._cache` |

### 2.3 文件结构

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块描述（中文 + 英文双行）

【背景】
【输入】
【输出】
【运行示例】
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# 常量定义
ROOT = Path(__file__).parent.parent
DEFAULT_THRESHOLD = 0.7


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(description='...')
    parser.add_argument('--top', type=int, default=20)
    args = parser.parse_args()
    # ...


if __name__ == '__main__':
    main()
```

### 2.4 注释规范

- **模块/类/函数 docstring**：中文说明 + 输入输出 + 示例
- **行内注释**：解释 **why**，不解释 what
- **TODO**：`# TODO(username): 描述`

### 2.5 错误处理

```python
# ✅ 推荐：捕获具体异常 + 提示
try:
    df = pd.read_parquet(fp)
except FileNotFoundError:
    print(f"❌ 文件不存在: {fp}")
    return None
except Exception as e:
    print(f"❌ 读取失败: {e}")
    return None

# ❌ 不推荐：捕获所有异常 + 静默
try:
    df = pd.read_parquet(fp)
except:
    pass
```

### 2.6 Lint / Format

```bash
make lint     # ruff check
make fix      # ruff auto-fix
make format   # ruff format
```

提交前 **必须** 通过 lint。

---

## 3. 提交规范

### 3.1 Commit Message 格式

采用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 3.2 Type 类型

| Type | 说明 | 示例 |
|---|---|---|
| `feat` | 新功能 | `feat(predict): 新增北交所过滤` |
| `fix` | Bug 修复 | `fix(predict): 修复 adj_factor 列冲突` |
| `docs` | 文档 | `docs(readme): 补充故障排查章节` |
| `style` | 代码格式（无逻辑变更）| `style(predict): ruff format` |
| `refactor` | 重构 | `refactor(journal): 拆分 buy/sell 函数` |
| `test` | 测试 | `test(predict): 添加单元测试` |
| `chore` | 构建/工具 | `chore(deps): 升级 lightgbm 4.5` |
| `perf` | 性能优化 | `perf(predict): ProcessPoolExecutor 并行` |

### 3.3 Scope 范围

| Scope | 范围 |
|---|---|
| `data` | 数据采集 |
| `predict` | 方案 D 预测 |
| `plan_e` | 方案 E 策略 |
| `train` | 模型训练 |
| `deploy` | 模型部署 |
| `systemd` | 自动化调度 |
| `docs` | 文档 |
| `ci` | CI/CD |
| `deps` | 依赖管理 |

### 3.4 示例

```bash
# 好的 commit
git commit -m "feat(predict): 新增 --no-skip-st 参数

允许保留 ST 股票在预测范围内（默认跳过 ST）。

Closes #123"

# 不好的 commit
git commit -m "fix bug"
git commit -m "update"
git commit -m "wip"
```

### 3.5 Commit 粒度

- ✅ 每个 commit 一个逻辑变更
- ❌ 不要把不相关的修改塞到同一个 commit
- ✅ 大功能拆成多个 commit

---

## 4. 分支管理策略

### 4.1 分支类型

| 分支 | 命名 | 用途 | 生命周期 |
|---|---|---|---|
| `main` | main | 主分支（生产）| 永久 |
| `develop` | develop | 开发分支 | 永久 |
| `feature/*` | feature/predict-bj | 功能分支 | PR 合并后删除 |
| `fix/*` | fix/adjust-factor-merge | Bug 修复 | PR 合并后删除 |
| `release/*` | release/v1.2.0 | 发布分支 | 发布后删除 |
| `hotfix/*` | hotfix/ollama-down | 紧急修复 | PR 合并后删除 |

### 4.2 工作流（Git Flow 简化版）

```
main (生产)
  ↑ PR
develop (开发)
  ↑ PR
feature/xxx (功能)
```

### 4.3 命名规范

```bash
# 功能
git checkout -b feature/plan-e-journal

# 修复
git checkout -b fix/predict-adj-factor-merge

# 文档
git checkout -b docs/architecture-adr

# 紧急修复
git checkout -b hotfix/ollama-service-down
```

---

## 5. Issue 提交要求

### 5.1 Issue 类型

使用 **GitHub Issue 模板**：

| 模板 | 用途 |
|---|---|
| `Bug Report` | 报告 Bug |
| `Feature Request` | 新功能建议 |
| `Question` | 提问 |
| `Documentation` | 文档问题 |
| `Performance` | 性能问题 |

### 5.2 Bug Report 模板

```markdown
## 描述

简要描述 Bug。

## 复现步骤

1. 执行 `python scripts/predict.py --codes 600519`
2. 看到错误 `KeyError: 'adj_factor'`
3. ...

## 期望行为

应该正常返回 Top 3 预测结果。

## 实际行为

抛出 KeyError。

## 环境

- OS: Ubuntu 24.04
- Python: 3.14
- 项目 commit: abc123

## 日志

```
[timestamp] KeyError: 'adj_factor'
  File "predict.py", line 297, in load_all_stocks
```

## 截图（如适用）
```

### 5.3 Feature Request 模板

```markdown
## 背景

为什么需要这个功能？解决什么问题？

## 提案

具体的功能描述。

## 备选方案

考虑过哪些其他方案？

## 影响范围

- 影响哪些模块？
- 是否需要新增依赖？
- 是否影响性能？
```

---

## 6. Pull Request 流程

### 6.1 PR 流程图

```
1. Fork / 创建分支
      ↓
2. 本地开发 + 测试
      ↓
3. make lint + make test 通过
      ↓
4. git push origin feature/xxx
      ↓
5. 创建 PR (feature/xxx → develop)
      ↓
6. CI 自动检查（lint + test）
      ↓
7. 评审者 review
      ↓
8. 修改 + push（更新 PR）
      ↓
9. 审批通过 + merge
      ↓
10. 删除 feature 分支
```

### 6.2 PR 标题

遵循 Commit Message 格式：

```
feat(predict): 新增 --no-skip-bj 参数
```

### 6.3 PR 描述模板

```markdown
## 变更说明

简要描述本次 PR 的目的和变更内容。

## 关联 Issue

Closes #123

## 变更类型

- [ ] 新功能 (feature)
- [ ] Bug 修复 (fix)
- [ ] 重构 (refactor)
- [ ] 文档 (docs)
- [ ] 测试 (test)

## 测试

- [ ] 添加了单元测试
- [ ] 添加了集成测试
- [ ] 手动验证通过

## 检查清单

- [ ] 代码通过 `make lint`
- [ ] 测试通过 `make test`
- [ ] 文档已更新
- [ ] commit message 符合规范

## 截图 / 输出

（如适用）

## 风险与影响

（如适用）
```

### 6.4 PR Size

- ✅ 单 PR ≤ 500 行（不含 lockfile）
- ✅ 单 PR 只解决一个问题
- ❌ 避免巨型 PR（> 1000 行）

---

## 7. 评审标准与合并条件

### 7.1 评审标准（Reviewer Checklist）

#### 功能性

- [ ] 功能按预期工作
- [ ] 边界条件已处理
- [ ] 错误处理合理
- [ ] 性能可接受

#### 代码质量

- [ ] 代码可读性高
- [ ] 无重复代码（DRY）
- [ ] 函数单一职责
- [ ] 命名清晰规范

#### 测试

- [ ] 单元测试覆盖核心逻辑
- [ ] 集成测试覆盖关键流程
- [ ] 测试数据真实（不是 mock）

#### 文档

- [ ] 函数 docstring 完整
- [ ] 复杂逻辑有注释
- [ ] README / 架构文档已更新
- [ ] CHANGELOG 已更新

#### 安全性

- [ ] 无硬编码密钥
- [ ] 无 SQL 注入风险
- [ ] 无路径遍历漏洞
- [ ] 输入验证充分

### 7.2 合并条件

必须满足 **全部** 条件：

| 条件 | 说明 |
|---|---|
| ✅ CI 全绿 | lint + pytest + sop-quick |
| ✅ 至少 1 个 Reviewer 批准 | 核心模块需 2 个 |
| ✅ 无未解决的 conversation | 评审讨论必须解决 |
| ✅ 与目标分支无冲突 | 或已手动解决 |
| ✅ commit 规范 | 遵循 Conventional Commits |
| ✅ PR 描述完整 | 模板全部填写 |

### 7.3 合并方式

- **Squash merge**（推荐）：feature → develop，保持历史清洁
- **Merge commit**：release 分支合并时使用，保留完整历史
- **Rebase**：个人分支，保持线性

---

## 8. 冲突处理

### 8.1 预防冲突

- ✅ 频繁 rebase 自己的分支
- ✅ PR 保持小且独立
- ✅ 模块职责清晰（参考 ARCHITECTURE.md）

### 8.2 解决冲突

```bash
# 1. 拉取最新 develop
git fetch origin
git checkout feature/my-branch
git rebase origin/develop

# 2. 解决冲突（编辑器）
# 修改冲突文件后：
git add <resolved_file>
git rebase --continue

# 3. 强制推送（rebase 后必须 force）
git push --force-with-lease origin feature/my-branch
```

### 8.3 冲突场景与策略

| 冲突场景 | 策略 |
|---|---|
| 同文件不同行 | 直接 rebase |
| 同文件同行 | 与对方协商 |
| 删除 vs 修改 | 确认意图后选择 |
| 大段冲突 | 召集双方 review |

### 8.4 升级路径

冲突无法解决时：

1. 在 PR 中 @ 涉及方
2. 同步到团队会议讨论
3. 必要时拆分 PR

---

## 9. 社区行为准则

### 9.1 我们的承诺

为了营造开放和友好的环境，我们作为贡献者和维护者承诺：

- 无论年龄、身材、残疾、族裔、性别认同与表达、经验水平、国籍、个人形象、种族、宗教或性取向如何，参与项目都不受骚扰。
- 营造安全、包容的讨论环境。
- 关注对社区最有利的事情。
- 对其他社区成员表示同理心。

### 9.2 不可接受的行为

- 使用性化语言或图像
- 人身攻击、侮辱性/贬损性评论
- 公开或私下骚扰
- 未经许可发布他人隐私信息
- 其他不专业或不恰当的行为

### 9.3 责任

项目维护者有权利和责任：

- 删除不符合准则的评论、提交、代码、wiki 编辑、issue 等
- 暂时或永久禁止违规者参与项目

### 9.4 报告

如遇违规行为，请通过以下渠道报告：

- 内部：联系 `jiuben@jiuben.com`
- GitHub：私聊项目维护者

所有投诉都会被审查和调查。

---

## 附录

### A. 常用命令速查

```bash
# 环境
source venv/bin/activate
pip install -r requirements.txt

# 开发
make lint        # ruff check
make fix         # ruff auto-fix
make format      # ruff format
make test        # pytest

# 数据
python -m tdxfeed.cli sync
python scripts/download_adj_factor_b.py

# 训练
python /tmp/v5_train_single.py

# 部署
bash v5/scripts/deploy_v5.sh

# 运行
python scripts/predict.py --top 30
python scripts/plan_e_backtest.py --compare

# 监控
python scripts/canary_health_check.py
systemctl --user list-timers --all

# Git
git checkout -b feature/xxx
git commit -m "feat(scope): description"
git push origin feature/xxx
gh pr create
```

### B. 项目关键数字

| 指标 | 值 |
|---|---:|
| 日 K 文件 | 5,235 |
| 复权因子 | 5,221 |
| 翻倍样本 | 22972 |
| 方案 D AUC | 0.8144 |
| 方案 E 胜率 | 55.6% |
| Ollama 模型 | 4 个 |
| systemd timer | 5 个 |

### C. 相关文档

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 系统总览 + 入门 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构决策记录（ADR）|
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | 工作流全景 |
| [docs/PLAN_E.md](docs/PLAN_E.md) | 方案 E 策略详解 |
| [docs/PLAN_E_LIVE.md](docs/PLAN_E_LIVE.md) | 方案 E 实盘指南 |
| [docs/API.md](docs/API.md) | OpenAPI 风格 API 文档 |
| [DELIVERY.md](DELIVERY.md) | 交付报告 |

---

## 许可

私有项目 · 内部使用 · 未经授权不得传播

> 最后更新：2026-09-27 · 维护者：jiuben
