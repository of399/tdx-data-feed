# Obsidian Phase 1.4 回滚方案（2026-10-15）

> 配套：`docs/obsidian_phase_1_4_plan_20261015.md`。

## 回滚总原则

Phase 1.4 是**纯加性**变更（git init + commit history），不删/改任何笔记/插件数据。回滚 = 移除 `.git/` 目录即可。

四层防御：
1. **Phase 1.4 实施前快照**（用户手动 `cp -r <vault> <vault>.pre-1.4.<ts>`）
2. **首次 commit 前可丢弃**（`rm -rf .git` 直接重做）
3. **commit 后可 reset**（`git reset --hard <commit>` 回到任一历史点）
4. **vault 自带 `.obsidian/workspace.json` 等不被跟踪**（受 `.gitignore` 保护）

## 回滚触发条件

- [x] git init 后插件/笔记功能异常
- [x] pre-commit 钩子误报率高（>30%）
- [x] 误 commit 大文件或敏感信息（用 BFG / filter-repo 清理后强制 push；本地项目无远端故可 reset）
- [x] 主观判断 git 化副作用不可接受

## 回滚操作

### 方案 A：完全回滚（移除 git 化）

```bash
# 1. 移除两 vault .git
rm -rf /home/jiuben/StockVault/.git
rm -rf /home/jiuben/ObsidianVaults/tdx-data-feed/.git

# 2. 取消 obsidian-git 配置（可选，配置无害）
#    Obsidian → Settings → Community plugins → 关闭 obsidian-git
#    或保留但 autoSaveInterval=0

# 3. 恢复备份（如有需要）
#    rm -rf /home/jiuben/StockVault
#    mv /home/jiuben/StockVault.pre-1.4.<ts> /home/jiuben/StockVault
```

### 方案 B：保留 git 但回滚某次 commit

```bash
# 查看历史
git -C /home/jiuben/StockVault log --oneline -10

# 软重置（保留工作区修改）
git -C /home/jiuben/StockVault reset --soft HEAD~1

# 硬重置（丢弃所有修改回到指定 commit）
git -C /home/jiuben/StockVault reset --hard <commit-sha>

# 撤销已 push（仅本地项目可省此步）
# git -C /home/jiuben/StockVault push --force
```

### 方案 C：保留 git 但禁用 on-save 自动 commit

```bash
# 改 .obsidian/obsidian-git.json
# autoSaveInterval: 30 → 0（关闭定时）
# commitOnClose: false（保持）
# 仍可在 Obsidian 用 ctrl+P → "Obsidian Git: Commit" 手动 commit
```

### 方案 D：禁用 pre-commit 钩子

```bash
# 方式 1：取消 core.hooksPath
git -C /home/jiuben/StockVault config --unset core.hooksPath

# 方式 2：钩子脚本改名（绕过但保留文件）
mv /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit \
   /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit.disabled

# 方式 3：临时跳过
git commit --no-verify
```

## 已知边角

### 1. Phase 1.4 不会改任何笔记内容
仅创建 `.git/`、首次 commit、配置 obsidian-git。笔记 `.md` 内容完全不变。

### 2. 笔记 .obsidian/workspace.json 不入 git
`.gitignore` 已排除 `workspace.json` 和 `workspace-*.json`。Obsidian 每次启动重写 workspace.json；不在版本控制。

### 3. plugin data 不入 git
`.obsidian/plugins/*/data.json` 和 `smart-connections/` 整体排除。嵌入缓存、UI 状态不入 git。

### 4. 备份 .bak 不入 git
`*.bak-*` 已排除。install 触发的备份不入版本控制。

### 5. 误 commit 大文件
若 .gitignore 不生效导致 `data/parquet/`、`sh/`、`sz/` 等入 git，提交会卡住或仓库膨胀。预防：
- Phase 1.4 实施前先 `git status --short | head -20` 确认
- 若 `data/` 出现，紧急 `git rm -r --cached data/` 后重新 commit

### 6. 跨设备不可同步
决策 3+6 = 不跨设备。仅本地 commit，无 push。vault 损坏 = 笔记丢失（git 历史仅本地，**无远端备份**）。
**强烈建议**：加一个外接硬盘/云盘定期 `cp -r <vault> <backup>`（与 git 解耦的物理备份）。

## 责任人

- 实施：项目所有者
- 验证：项目所有者
- 紧急回滚：项目所有者

## 物理备份建议（与 git 互补）

```bash
# 周一/周五 21:00 自动同步到外接硬盘（假设挂载在 /mnt/backup）
rsync -avh --delete \
  /home/jiuben/StockVault/ /mnt/backup/StockVault/
rsync -avh --delete \
  /home/jiuben/ObsidianVaults/tdx-data-feed/ /mnt/backup/ObsidianVaults-tdx-data-feed/
```

或简化为 systemd user timer 跑 `cp -r` 到第二个本地目录。
