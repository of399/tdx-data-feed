---
name: tdx-obsidian-version-control
description: "为 tdx-data-feed 项目的两个 Obsidian vault（StockVault / ObsidianVaults/tdx-data-feed）建立并维护 git 版本控制。覆盖 git init、.gitignore 模板、commit 策略（on-save/on-close/cron）、disablePush 默认、跨设备同步方案选型、故障排查。当用户提到 Obsidian 笔记版本控制、vault git 化、笔记历史回溯、跨设备同步、commit 失败排查时使用。"
allowed-tools: Read,Grep,Glob,Bash,Edit,Write
display_name: "tdx-obsidian-version-control"
visibility: "project"
---

# tdx-obsidian-version-control

## 适用工作空间

| 角色 | 路径 |
|---|---|
| 工作空间根 | `/home/jiuben/tdx-data-feed` |
| 个人综合 vault | `/home/jiuben/StockVault`（含数学子库 + 标的/框架/数据/回测）|
| 项目 vault | `/home/jiuben/ObsidianVaults/tdx-data-feed` |
| skill / 文档目录 | `/home/jiuben/tdx-data-feed/.codebuddy/skills/tdx-obsidian-version-control/` |
| 升级计划文档 | `/home/jiuben/tdx-data-feed/docs/obsidian_upgrade_plan_20261015.md` |

> 两个 vault 当前**均无 .git 目录**（截至 2026-10-15 盘点）。本 skill 负责从零开始建立版本控制。

## 使用场景

- "把两个 Obsidian vault 都用 git 管起来"
- "笔记改了想回溯历史 / 找回之前版本"
- "跨设备同步笔记（笔记本 ↔ 桌面 / 远程）"
- "obsidian-git 插件怎么配，commit 策略选哪个"
- "commit 失败 / push 被禁 / merge 冲突 怎么排查"
- "archive 报告太多想治理，又怕误删"

## 预期功能范围

| 在范围内（In scope） | 不在范围内（Out of scope） |
|---|---|
| 两 vault 的 `git init` | 笔记内容编辑、模板内容（用 `tdx-obsidian-math-verify`）|
| 标准 `.gitignore` 模板（Obsidian/插件/媒体/备份四类）| 远端 GitHub/Gitea 的运维（仅做 push 指引）|
| commit 策略三选一：on-save / on-close / cron | Syncthing / iCloud 客户端安装（仅做选型）|
| `disablePush=true` 默认安全姿态 | Obsidian 业务逻辑、笔记元数据规范 |
| 跨设备同步方案选型（git / syncthing / iCloud）| 数据迁移（用 sync-to-obsidian.sh）|
| 故障排查：commit 失败、`.gitignore` 不生效、冲突 | 备份恢复策略（用 verify-math / canary）|

## 所在环境与依赖

| 依赖 | 说明 |
|---|---|
| git 2.20+ | `git init` / `add` / `commit` / `log` / `restore` |
| bash 4+ | skill 内置脚本的 shell |
| Obsidian 插件 `obsidian-git`（可选）| 若启用，commit 自动化更顺；亦可用外部 cron |
| 跨设备同步：需至少一种（git remote / syncthing / iCloud） | git push 最通用但需远端；syncthing P2P 实时但配置多 |
| 权限 | 当前用户对两 vault 均有 `rwx` 权限 |

---

## 涉及的操作步骤（完整流程）

### 步骤 A：git init（首次建立）

```bash
# A.1 个人综合 vault
cd /home/jiuben/StockVault
[ ! -d .git ] && git init -b main
git config user.name  "vault-bot"
git config user.email "vault@local"
# A.2 项目 vault
cd /home/jiuben/ObsidianVaults/tdx-data-feed
[ ! -d .git ] && git init -b main
git config user.name  "vault-bot"
git config user.email "vault@local"
```

> 防御式：每个 vault 首次 git init 前**先做一次 dry-run 清单**（`git status -s` 模拟），避免把敏感文件纳入。skill 脚本内置此步。

### 步骤 B：写入标准 `.gitignore`

```bash
# B.1 在两 vault 根写入 .gitignore
cat > /home/jiuben/StockVault/.gitignore <<'EOF'
# --- Obsidian 临时/状态 ---
.trash/
.DS_Store
*.swp
workspace.json
workspace-*.json

# --- install 触发的备份 ---
*.bak-*
*.bak

# --- 大文件（>1MB 媒体/压缩）---
*.mp4
*.mov
*.zip
*.tar.gz
*.parquet

# --- 插件数据/嵌入缓存（隐私+体积）---
.obsidian/plugins/*/data.json
.obsidian/copilot/index.json
.obsidian/smart-connections/
.obsidian/plugins/obsidian-git/data.json
EOF

# B.2 同样写入项目 vault
cp /home/jiuben/StockVault/.gitignore /home/jiuben/ObsidianVaults/tdx-data-feed/.gitignore
```

### 步骤 C：commit 策略（三种选一）

#### 方案 C1：`obsidian-git` 插件（on-close，默认推荐）

`/home/jiuben/tdx-data-feed/etc/obsidian/obsidian-git.json`：
```json
{
  "version": "2.0.0",
  "settings": {
    "commitMessage": "vault: {{date}} {{hostname}}",
    "autoCommit": false,
    "autoPush": false,
    "autoPull": false,
    "disablePush": true,
    "commitOnClose": true,
    "syncMethod": "merge",
    "refreshSourceControl": true,
    "differentInterval": 10
  }
}
```

- 优点：写入即暂存、关窗即 commit，用户零感知
- 缺点：与 Templater 写入可能 race（写入中被 commit 截断）
- 缓解：commit message 不含内容 hash，只标记时间窗

#### 方案 C2：外部 cron（无插件，最稳）

```bash
# 每 30 min 扫描两 vault，modified > 5 min 则 commit
cat > /home/jiuben/.local/bin/vault-autocommit.sh <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
commit_one() {
  local v="$1"
  cd "$v" || return
  # 跳过 .gitignore 排除的文件
  local changed
  changed=$(git ls-files --modified --others --exclude-standard 2>/dev/null | head -5)
  [ -z "$changed" ] && return
  git add -A
  git commit -m "vault: auto $(date -Iseconds) ${changed// /,}" >/dev/null
}
commit_one /home/jiuben/StockVault
commit_one /home/jiuben/ObsidianVaults/tdx-data-feed
EOF
chmod +x /home/jiuben/.local/bin/vault-autocommit.sh

# 接入 systemd user timer
```

#### 方案 C3：纯手动（最小风险）

```bash
git add -A && git commit -m "vault: $(date -Iseconds) <说明>"
```

### 步骤 D：跨设备同步方案（按场景选）

| 方案 | 适用 | 优点 | 缺点 |
|---|---|---|---|
| git push (GitHub private / Gitea) | 多设备、有云端 | 历史可查、权限可控 | 非实时、需远端 |
| syncthing P2P | 局域网 / 同一账号 | 实时、点对点 | 多设备配置繁琐 |
| iCloud / OneDrive | macOS+iOS | 零配置 | 无版本历史（git 仍需另搭）|

> 建议：双轨——git push 做版本控制，syncthing 做实时同步。两者并行不冲突（git 看版本，syncthing 看文件）。

### 步骤 E：故障排查速查

| 症状 | 根因 | 处理 |
|---|---|---|
| `commitOnClose` 没生效 | obsidian-git 插件未启用 / 配置未重载 | 检查 `.obsidian/community-plugins.json` 含 `obsidian-git` |
| `git status` 显示大量 `*.bak-*` | `.gitignore` 未生效 | `git rm -r --cached .` 后重新 add |
| `git push` 被拒 | `disablePush=true` | 临时改 `disablePush=false` 或手工 push |
| commit 内容缺一半 | Templater 与 on-save race | 改用 on-close 或 cron |
| merge 冲突 | 多端同时改 | `git status` → 手动 resolve → commit |

---

## 预期输出

### `git init` 后

```
Initialized empty Git repository in /home/jiuben/StockVault/.git/
```

### 首次 commit

```
[main (root-commit) a1b2c3d] vault: init StockVault
 1234 files changed, 56789 insertions(+)
 create mode 100644 00-MOC.md
 create mode 100644 01-标的/000001-平安银行.md
 ...
```

### commit 历史

```
$ git log --oneline -5
a1b2c3d vault: 2026-10-15T18:30:00 01-标的/000001-平安银行.md
b2c3d4e vault: 2026-10-15T18:00:00 04-回测/Risk-Parity组合回测.md
c3d4e5f vault: 2026-10-15T17:30:00 templates/Daily-Note.md
...
```

---

## 通过标准（git 化完成判据）

| 判据 | 要求 |
|---|---|
| `.git` 存在 | `ls -d /home/jiuben/StockVault/.git /home/jiuben/ObsidianVaults/tdx-data-feed/.git` 均成功 |
| `.gitignore` 生效 | `git status` 不显示 `.trash/`、`*.bak-*`、大文件 |
| 首次 commit 完成 | `git log --oneline` 至少 1 条 `vault: init ...` |
| 后续修改可追踪 | 改一个文件后 `git status` 能看到，`git commit` 后 `git log` 增加 |
| `disablePush` 默认 | `grep disablePush .obsidian/obsidian-git.json` 显示 `true` |
| 配置 JSON 在源-部署闭环 | `make verify-obsidian-templates`（Phase 1.1 升级后）覆盖 |

## 正常使用标准（在本工作空间）

1. **两 vault .git 存在**
2. **`.gitignore` 模板就位**，且 `git check-ignore` 能命中 `.bak-*`、`.trash/` 等
3. **首次 commit 已创建**
4. **至少一次手动 commit 验证写入流程**
5. **`disablePush` 默认开启**
6. **commit message 格式一致**：`vault: <时间> <说明>`
7. **跨设备同步方案已选定并文档化**（git push / syncthing / iCloud 至少一种）
8. **故障排查速查表已就位**（步骤 E 表格在 skill 内可查）

---

## 与 `tdx-obsidian-math-verify` 的联动

`tdx-obsidian-version-control` 在 commit 之前，建议先跑 `tdx-obsidian-math-verify`（或 Phase 1.1 升级后的 `tdx-obsidian-version-control` 扩展）确认源-部署零漂移，避免把"已经漂移的状态"作为基线 commit 进去。

典型工作流：
```bash
cd /home/jiuben/tdx-data-feed
make verify-math           # 或升级后 make verify-obsidian-templates
git -C /home/jiuben/StockVault add -A
git -C /home/jiuben/StockVault commit -m "vault: $(date -Iseconds) verified baseline"
```

## 相关命令

- `make verify-math`：数学库源-部署零漂移（`tdx-obsidian-math-verify`）
- `make verify-obsidian-templates`（Phase 1.1 升级后）：全 vault 零漂移
- `scripts/sync-to-obsidian.sh`：项目库 sync（repo → vault）
- `scripts/install-math-templates.sh`：数学模板 install（源 → 部署）
- `systemctl --user status obsidian-sync.{path,service}`：sync 链路
