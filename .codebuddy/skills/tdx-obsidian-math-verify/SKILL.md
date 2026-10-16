---
name: tdx-obsidian-math-verify
description: "验证 tdx-data-feed 数学手写 Obsidian 库（StockVault / math-study）源↔部署零漂移。覆盖 make verify-math 调用、零漂移完整执行步骤、预期输出与通过标准。当用户要求校验数学笔记库一致性、排查 install/verify 漂移、确认模板与 MOC 总览是否同步时使用。"
allowed-tools: Read,Grep,Glob,Bash,Edit,Write
display_name: "tdx-obsidian-math-verify"
visibility: "project"
---

# tdx-obsidian-math-verify

## 适用工作空间（具体路径，缺一不可）

| 角色 | 路径 |
|---|---|
| 工作空间根 | `/home/jiuben/tdx-data-feed` |
| 源（单一事实源） | `/home/jiuben/tdx-data-feed/etc/obsidian/math/`（含 `templates/T-*.md` 7 份 + `MOC/总览.md` + README）|
| 部署 vault（Makefile 默认） | `/home/jiuben/StockVault`（数学笔记根前缀 `05-知识库/数学`，模板目录 `06-模板/math`）|
| 部署 vault（脚本默认） | `${HOME}/ObsidianVaults/math-study`（注意：与 Makefile 默认不一致，见下文）|
| 验证脚本 | `/home/jiuben/tdx-data-feed/scripts/verify-math-templates.sh` |
| Make 目标 | `/home/jiuben/tdx-data-feed/Makefile` 中的 `verify-math`（行 447–448）|
| 安装脚本（漂移时用于回写） | `/home/jiuben/tdx-data-feed/scripts/install-math-templates.sh` |

> ⚠️ **路径不一致（已纳入"同步与备份/可维护性"维度）**：
> - `Makefile verify-math` 默认校验 `/home/jiuben/StockVault`
> - `install-math-templates.sh` / `verify-math-templates.sh` 默认指向 `~/ObsidianVaults/math-study`
> 验证前必须先确认你要校验的是哪一个部署 vault，必要时用 `MATH_VAULT` 等环境变量覆盖。

## 使用场景

- 模板或 MOC 总览改动后，确认"源 ↔ 部署"是否完全一致
- `install-math-templates.sh` 之后做一致性复验
- 提交/CI 前快速检查数学库无漂移
- 跨设备或切换 vault（如 StockVault ↔ math-study）时确认目标
- 排查"我手动改了部署侧为什么没生效"——大概率是被下次 install 覆盖；此 skill 帮你把"是不是漂移"量化出来

## 预期功能范围

| 在范围内（In scope） | 不在范围内（Out of scope） |
|---|---|
| 源 vs 部署的 **7 个模板 + 1 个 MOC** 逐字节比对（`cmp -s`）| 数学笔记正文内容校验 |
| 退出码（0/1）反映一致性 | 插件配置（`.obsidian/*.json`）校验 |
| 漂移时打印缺失/不一致文件 | Dataview / SR 运行时行为校验 |
| `make verify-math` 入口封装 | vault 性能、文件数量统计 |
| 漂移时引导 `install` 复验 | 北交所/项目库 `tdx-data-feed`（用 `make verify-obsidian`）|

## 所在环境与依赖

| 依赖 | 说明 |
|---|---|
| Shell | bash 4+（脚本 `set -uo pipefail`、流程控制） |
| 工具 | `make`、`cmp`、`find`、`dirname`、`realpath`（POSIX 标准） |
| 源目录 | `etc/obsidian/math/templates/T-*.md` 与 `etc/obsidian/math/MOC/总览.md` 必须存在 |
| 部署 vault | 路径可读（不必可写——verify 是只读） |
| 权限 | 当前用户对源与部署均需 `r` 权限 |
| OS | Linux（脚本未做 macOS 兼容验证，`realpath` 在 macOS 需 `brew install coreutils`） |

## 数学库用途说明

数学手写库是 tdx-data-feed 项目中**独立于量化项目库**的 Obsidian vault，用于沉淀数学学习笔记（定义 / 定理 / 书索引等），并通过自动化链实现结构化复习：

- **单一事实源**：`etc/obsidian/math/`（模板 + MOC + README，共 9 个 `.md`）
- **部署 vault**：默认 `/home/jiuben/StockVault`，独立 git 仓库
- **安装方式**：`scripts/install-math-templates.sh` 幂等复制（等价于 `replace_in_file` 直写副本），覆盖前备份 `.bak-<ts>`（不删用户数据），支持 `--init-config` / `--copy-plugin-bins` / `--patch-templater` / `--dry-run`
- **自动化链**：
  - **Templater**：动态生成标签与字段（注意 `<% %>` 写在双引号 YAML flow 列表内不展开、整项被丢；须用无引号标量或 block 序列）
  - **Dataview**：以 `#math-notes` 标签驱动的依赖图（`depends: ["[[链接]]"]`、`used_by` 反查 `contains(depends, this.file.link)`）
  - **SR（Spaced Repetition）**：间隔重复；`flashcards` 自动标签藏在 `<% 'flashcards/' + ... %>` 表达式中以匹配 SR 前缀匹配

目标：保证"源(`etc/obsidian/math`) ↔ 部署 vault"的模板与 MOC 总览**逐字节一致**，任何手工改动或安装差异都会触发漂移告警。

## ⚠️ 验证前确认

1. 当前要校验的部署 vault 路径（StockVault 还是 math-study？）
2. 源目录就绪：`etc/obsidian/math/templates` 与 `etc/obsidian/math/MOC/总览.md` 存在
3. 工作空间根确认：`pwd` 落在 `/home/jiuben/tdx-data-feed`

## 涉及的操作步骤（完整执行流程）

```bash
# 步骤 0：cd 到工作空间根
cd /home/jiuben/tdx-data-feed

# 步骤 1：确认源与部署目录存在
ls etc/obsidian/math/templates/T-*.md
ls etc/obsidian/math/MOC/总览.md
ls -d /home/jiuben/StockVault            # 默认部署 vault（make 目标）

# 步骤 2：执行零漂移校验
make verify-math

# 步骤 2 备选：env 覆盖部署 vault / 前缀 / 模板目录
MATH_VAULT=~/ObsidianVaults/math-study MATH_PREFIX= MATH_TPL_DIR=templates/math make verify-math

# 步骤 2 备选：直接调脚本（与 make 等价）
bash scripts/verify-math-templates.sh --vault /home/jiuben/StockVault --prefix "05-知识库/数学" --tpl-dir "06-模板/math"

# 步骤 3：查看退出码（0=一致，1=漂移）
echo "exit=$?"

# 步骤 4：若漂移，重新同步源→部署，再校验
scripts/install-math-templates.sh --vault /home/jiuben/StockVault --prefix "05-知识库/数学" --tpl-dir "06-模板/math"
make verify-math
```

## `make verify-math` 调用方式

```bash
# 默认（Makefile 内置参数）
make verify-math
# 等价于：
# bash scripts/verify-math-templates.sh \
#   --vault /home/jiuben/StockVault \
#   --prefix "05-知识库/数学" \
#   --tpl-dir "06-模板/math"

# 环境变量覆盖（不改 Makefile 也能换目标）
MATH_VAULT=<部署vault路径> MATH_PREFIX=<根前缀> MATH_TPL_DIR=<模板相对目录> make verify-math
```

脚本逻辑（`scripts/verify-math-templates.sh`）：只读比对 `etc/obsidian/math/templates/T-*.md` 与总览 MOC 是否 `cmp -s` 一致，**不改盘**。全一致 `exit 0`，否则 `exit 1`。

## 预期输出

```
源: etc/obsidian/math
目标 vault: /home/jiuben/StockVault
根前缀: 05-知识库/数学
模板目录: 06-模板/math
---
[一致] 模板 T-定理.md
[一致] 模板 T-定义.md
[一致] 模板 T-反例.md
[一致] 模板 T-开放问题.md
[一致] 模板 T-例子.md
[一致] 模板 T-书索引.md
[一致] 模板 T-证明.md
[一致] 总览 MOC
---
✓ 全部一致（源 <-> 部署）
```

漂移时则出现 `[漂移] ...` 或 `[缺失] ...`，结尾为：

```
✗ 存在不一致，请先跑 install-math-templates.sh 同步
```

## 通过标准（零漂移判据）

| 判据 | 要求 |
|---|---|
| 退出码 | `echo $?` = **0** |
| 模板文件 | 所有 `T-*.md` 显示 `[一致]`，无 `[漂移]` / `[缺失]` |
| MOC 总览 | `总览 MOC` 显示 `[一致]` |
| 结尾标识 | 必须打印 `✓ 全部一致（源 <-> 部署）` |

任一模板或 MOC 出现 `[漂移]` / `[缺失]`，或退出码非 0，即判定**未通过**，须执行 `install-math-templates.sh` 重新同步后复验。

## 正常使用标准（在本工作空间中的判据）

按以下顺序逐项确认；全部满足即视为"在本工作空间正常使用"。

1. **工作空间根定位**：`pwd` 显示 `/home/jiuben/tdx-data-feed`，否则 `cd` 回来
2. **源目录就绪**：`ls etc/obsidian/math/templates` 至少含 7 份 `T-*.md`；`ls etc/obsidian/math/MOC/总览.md` 存在
3. **部署 vault 存在**：`ls -d /home/jiuben/StockVault`（或 env 覆盖后的路径）成功
4. **Makefile 目标可达**：`make -n verify-math` 不报错，dry-run 展开正常
5. **执行零漂移校验**：`make verify-math` 退出码 = 0
6. **输出一致性**：7 模板 + 1 MOC 全 `[一致]`，结尾 `✓ 全部一致（源 <-> 部署）`
7. **重复执行幂等**：连跑两次 `make verify-math` 结果一致（脚本只读，天然幂等）

> ✅ 本工作空间已通过上述全部 7 项（2026-10-15 实际跑通，详见今日记忆）。

## 相关命令

- `make verify-obsidian`：校验量化项目库 `tdx-data-feed` 的 sync 与 systemd 重载（与数学库相互独立）
- `scripts/install-math-templates.sh --dry-run`：预演安装，不落盘
