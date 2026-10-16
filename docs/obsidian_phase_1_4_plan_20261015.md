# Obsidian Phase 1.4 详细计划：双 vault Git 化（2026-10-15）

> 配套：`docs/obsidian_upgrade_plan_20261015.md`（总计划 Phase 1.4）、`docs/obsidian_phase_1_1_plan_20261015.md`（on-save 30s debounce 配置 + pre-commit 钩子文件就位）、`docs/obsidian_phase_1_2_plan_20261015.md`（verify v2.1 + 配置统一）。

## 1. 背景与目标

**Phase 1.1+1.2 末态**：
- `obsidian-git.json` v2 已就位（`autoSaveInterval=30` + `disablePush=true` + `autoPush/Pull=false`）
- `.gitignore` 双 vault 已就位（4 类：Obsidian/插件/媒体/备份）
- `scripts/hooks/pre-commit` 已就位（含 verify 拦截）
- `make verify-obsidian-templates` 双 vault exit 0（5 模板 + 5 JSON 全一致）

**目标**：
1. 两 vault `git init`（**仅本地**，决策 6）
2. 首次 commit 锁定基线
3. 启用 obsidian-git 插件接受 on-save 自动 commit（决策 2）
4. 挂载 pre-commit 钩子拦截漂移 commit（脚本已写，挂载需 Phase 1.4 实施）
5. 撰写 on-save 触发机制 / 提交信息 / 冲突处理文档

## 2. 涉及的文件与配置项

| 文件 | 操作 | 用途 |
|---|---|---|
| `StockVault/.git/` | git init -b main | StockVault 本地仓库 |
| `StockVault/.git/config` | git config user.name/email | commit author |
| `ObsidianVaults/tdx-data-feed/.git/` | git init -b main | Project vault 本地仓库 |
| `ObsidianVaults/tdx-data-feed/.git/config` | git config user.name/email | commit author |
| 两 vault `core.hooksPath` | `git config core.hooksPath <path>` | 挂载 pre-commit 钩子 |
| `scripts/hooks/pre-commit` | chmod +x（首次需要 execute_command 批准）| 钩子执行权限 |
| `scripts/git_init_vaults.sh` | 新建 | 一键 git init + config + 钩子挂载 |
| `docs/obsidian_git_workflow_20261015.md` | 新建 | on-save 触发 / 提交信息 / 冲突处理文档 |

## 3. 实施步骤

### Step 1：备份（手动执行）

Phase 1.4 实施前对两 vault 做完整快照（git 不可逆保险）：

```bash
TS=$(date +%Y%m%d-%H%M%S)
cp -r /home/jiuben/StockVault /home/jiuben/StockVault.pre-1.4.${TS}
cp -r /home/jiuben/ObsidianVaults/tdx-data-feed /home/jiuben/ObsidianVaults/tdx-data-feed.pre-1.4.${TS}
```

### Step 2：git init 两 vault

通过 `scripts/git_init_vaults.sh` 一键执行（用户跑一次）：

```bash
bash /home/jiuben/tdx-data-feed/scripts/git_init_vaults.sh
```

脚本内容：
1. 检查 vault 已存在、`.git` 不存在
2. `git init -b main` 两 vault
3. `git config user.name "vault-bot"`
4. `git config user.email "vault@local"`
5. `git config core.hooksPath /home/jiuben/tdx-data-feed/scripts/hooks` 两 vault
6. `chmod +x /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit`
7. 提示用户安装 obsidian-git 插件（若 vault `.obsidian/community-plugins.json` 未启用）

### Step 3：首次 commit（基线锁定）

```bash
# StockVault
cd /home/jiuben/StockVault
git add -A
git status  # 确认 .gitignore 生效（应看不到 .trash/、*.bak-* 等）
git commit -m "vault: init $(date -Iseconds) StockVault baseline (post-Phase-1.2)"

# Project vault
cd /home/jiuben/ObsidianVaults/tdx-data-feed
git add -A
git status
git commit -m "vault: init $(date -Iseconds) Project baseline (post-Phase-1.2)"
```

### Step 4：启用 obsidian-git on-save

打开 Obsidian → Settings → Community plugins → 确认 `obsidian-git` 已启用（已在 community-plugins.json 中）→ 加载配置（v2）：

```
commitMessage = "vault: {{date}} {{hostname}}"
autoCommit     = false         (关闭 autoCommit 以让 autoSaveInterval 单独控制)
autoPush       = false         (决策 6 仅本地)
autoPull       = false         (决策 3 不跨设备)
disablePush    = true          (决策 6)
commitOnClose  = false         (用 on-save 而非 on-close)
autoSaveInterval = 30          (决策 2 30s debounce 防 Templater race)
```

**触发时机**：`autoSaveInterval=30` → 每 30 秒扫描一次 staged/unstaged 变更；若有变更，自动 `git add -A && git commit`。

### Step 5：挂载 pre-commit 钩子

git_init_vaults.sh 已挂载 core.hooksPath 到 `scripts/hooks/`，**前提是 pre-commit 已是 +x**。

钩子触发时机：`git commit` 任何分支（包括 on-save 30s 触发的 commit）前。

钩子校验项：
1. `make verify-obsidian-templates` exit 0（源-部署零漂移）
2. （Phase 1.4+）敏感信息拦截（详见 Step 6）

### Step 6：升级 pre-commit 钩子 — 敏感信息拦截

`scripts/hooks/pre-commit` 新增敏感信息扫描：

```bash
# 敏感信息白名单（正则）
SENSITIVE_PATTERNS=(
  'AKIA[0-9A-Z]{16}'                       # AWS Access Key
  'sk-[A-Za-z0-9]{20,}'                    # OpenAI key
  'sk-ant-[A-Za-z0-9-]{20,}'               # Anthropic key
  'ghp_[A-Za-z0-9]{36,}'                   # GitHub PAT
  'glpat-[A-Za-z0-9-]{20,}'                # GitLab PAT
  'xox[baprs]-[A-Za-z0-9-]{10,}'           # Slack token
  'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' # JWT
  '(?i)(password|passwd|pwd)\s*=\s*.+'     # 明文密码
  '(\d{6})\s*[-持仓满]\s*(\d+)\s*股'         # 真实持仓行（code + 数量 + 股）
)
```

**校验范围**：`git diff --cached` 的新增行（`+` 开头的行）。

**校验逻辑**：若新增行匹配任一模式，pre-commit 拒绝 commit 并打印匹配位置。

**风险**：可能误报（如 OAuth test key）。用户可 `git commit --no-verify` 跳过（仅本地项目可接受）。

## 4. on-save 触发机制

```
Obsidian 编辑笔记
   ↓
Templater / 用户写入
   ↓
Obsidian 触发 on-save
   ↓
obsidian-git 插件 (autoSaveInterval=30s)
   ↓ (30s 后扫描 modified files)
git add -A
   ↓
pre-commit 钩子（若启用 core.hooksPath）
   ↓
verify-obsidian-templates (exit 0/1) + 敏感信息扫描
   ↓
git commit -m "vault: {{date}} {{hostname}}"
```

**关键时序**：
- 30s debounce：用户连续编辑期间不触发，停止编辑 30s 后一次性 commit
- Templater race 缓解：30s 已足够 Templater 完成复杂模板渲染
- 钩子拦截：commit 失败时 git status 看到 staged 但未 committed，Obsidian 不会重复触发

## 5. 提交信息生成规则

**obsidian-git 配置**（v2）：

```json
{
  "commitMessage": "vault: {{date}} {{hostname}}"
}
```

**变量展开**（obsidian-git 文档）：
- `{{date}}` → `YYYY-MM-DD-HHMM`（如 `2026-10-15-2130`）
- `{{hostname}}` → `vault-bot@host` 或 `jiuben@jiuben-PC`

**实际生成示例**：
```
vault: 2026-10-15-2130 jiuben@jiuben-PC
```

**多文件 commit**：obsidian-git 在一次 commit 中包含所有 modified 文件。`listChangedFilesInMessageBody=false`（v2 配置）→ commit message 不展开文件清单，避免冗长。

**手动 commit 增强**：

```bash
git commit -m "vault: <语义描述> [scope]

- 改动 1
- 改动 2
"
```

**禁止的 commit 信息**：
- 包含 API key、token、真实持仓、客户数据
- 包含个人隐私
- 单字符 commit（如 "fix"）

## 6. 冲突处理

### 6.1 syncMethod = merge（已配）

obsidian-git 配 `syncMethod: "merge"`，即冲突时自动 merge（fast-forward 优先）。

### 6.2 冲突场景

**场景 A：同文件同时被 Obsidian 修改 + 外部工具修改**
- obsidian-git 触发 commit
- 若同一文件在外部被 `git checkout` 或 stash 改过，pull/merge 时冲突
- 解决：`git status` → 找到冲突文件 → 手动 edit → `git add` → `git commit`

**场景 B：on-save 30s 期间 Templater 写入未完成**
- 30s 内 Templater 仍在写入文件
- obsidian-git 扫描 modified → 误提交不完整内容
- 解决：把 autoSaveInterval 调大（如 60s）；或暂停 Templater 自动渲染

**场景 C：pre-commit 钩子失败**
- verify 报漂移 / 敏感信息扫描报命中
- git status 显示 staged 但未 committed
- Obsidian 不会重试（钩子失败后 git 进程退出）
- 解决：见钩子失败处理流程

### 6.3 钩子失败处理流程

```bash
# 1. 看失败原因
git status
git diff --cached | head -30

# 2. 分类处理
#    a) verify 漂移：
#       - 跑 make verify-obsidian-templates 看具体漂移项
#       - 修复后重 commit
#    b) 敏感信息命中：
#       - 找到具体行（钩子会打印行号）
#       - 决定：删除敏感内容 / 用占位符 / 移到 vault 之外
#       - 重 commit

# 3. 紧急跳过
git commit --no-verify   # 仅本地项目可接受
```

### 6.4 跨设备冲突（已规避）

决策 3（不跨设备）+ 决策 6（仅本地）= 无远端 = 不会发生 cross-device merge 冲突。

## 7. 验证方式

```bash
# 1. git init 完成
ls -d /home/jiuben/StockVault/.git
ls -d /home/jiuben/ObsidianVaults/tdx-data-feed/.git

# 2. core.hooksPath 已挂载
git -C /home/jiuben/StockVault config core.hooksPath
git -C /home/jiuben/ObsidianVaults/tdx-data-feed config core.hooksPath
# 都应输出 /home/jiuben/tdx-data-feed/scripts/hooks

# 3. pre-commit 钩子 +x
ls -l /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit
# -rwxr-xr-x 1 ... pre-commit

# 4. 首次 commit 完成
git -C /home/jiuben/StockVault log --oneline -3
git -C /home/jiuben/ObsidianVaults/tdx-data-feed log --oneline -3

# 5. on-save 自动 commit 验证
#    改一个笔记，等 30s
#    git -C <vault> log --oneline -1
#    应有 vault: 2026-... jiuben@... 新 commit

# 6. pre-commit 钩子拦截验证
#    故意制造漂移（修改部署侧模板）
#    跑 make precommit-verify 应报失败
#    跑 git commit 应被 pre-commit 拒绝

# 7. 敏感信息拦截验证（Step 6 实施后）
#    故意在笔记里加一行 sk-abcdefghijklmnopqrst
#    git add && git commit 应被 pre-commit 拒绝
```

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 审批弹窗超时（chmod/git init） | Phase 1.4 实施被卡 | 通过 `git_init_vaults.sh` 一键脚本，用户手动执行 |
| Templater race | commit 内容不完整 | 30s debounce；verify 拦截（若 pre-commit 钩子启用）|
| pre-commit 钩子误报 | 误拦截正常 commit | 白名单机制（`git commit --no-verify`）；正则优化 |
| .gitignore 不生效 | 大文件/嵌入缓存入 git | Phase 1.4 实施前先跑 `git check-ignore` 验证 |
| 钩子被绕过（--no-verify）| 漂移状态进 commit 历史 | 仅本地项目可接受；定期跑 `make verify-obsidian-templates` 兜底 |
| 误 git init 到错误目录 | 误跟踪无关目录 | `git_init_vaults.sh` 内置 `[[ ! -d .git ]]` 检查 |
| 双 vault commit author 冲突 | 提交信息混淆 | 两 vault 都用 `vault-bot <vault@local>` |
| 误 git init 后再 init 嵌套仓库 | 路径冲突 | `git_init_vaults.sh` 检查 vault 根而非当前目录 |

## 9. 工作量估计

| 步骤 | 估计 |
|---|---|
| 1. 备份快照（用户） | 0.1h |
| 2. 写 git_init_vaults.sh | 0.3h |
| 3. 写 on-save 触发/提交信息/冲突处理文档 | 0.3h |
| 4. 升级 pre-commit 钩子（敏感信息）| 0.5h |
| 5. 用户跑 git_init_vaults.sh + 首次 commit | 0.2h |
| 6. 端到端验证（on-save + pre-commit + 敏感信息）| 0.5h |
| **合计** | **1.9h** |
