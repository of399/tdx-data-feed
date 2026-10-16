# Obsidian Git 工作流（2026-10-15）

> 配套：`docs/obsidian_phase_1_4_plan_20261015.md`（实施）、`docs/obsidian_phase_1_4_rollback_20261015.md`（回滚）、`.codebuddy/skills/tdx-obsidian-version-control/SKILL.md`（git 化 skill）。

## 1. on-save 自动 commit 触发机制

### 1.1 触发链

```
Obsidian 编辑笔记
   ↓ 保存（手动 Ctrl+S 或自动）
Templater / 用户写入
   ↓
obsidian-git 插件（autoSaveInterval = 30s）
   ↓ 每 30s 扫描一次 modified
git add -A
   ↓
pre-commit 钩子（core.hooksPath 指向 scripts/hooks/）
   ↓
检查 A: verify-obsidian-templates (源-部署零漂移)
检查 B: 敏感信息扫描（git diff --cached 新增行）
   ↓
git commit -m "vault: {{date}} {{hostname}}"
   ↓
.git/objects/ 新增 commit
```

### 1.2 关键时序

| 节点 | 耗时 | 备注 |
|---|---|---|
| 用户保存 | < 100ms | Obsidian 写盘 |
| 30s debounce | 30s | 防 Templater race |
| git add | < 1s | 增量 stage |
| pre-commit verify | 0.5-2s | bash 子进程 |
| pre-commit 敏感扫描 | < 100ms | grep -E |
| git commit | < 500ms | 本地写 object |
| **总耗时** | **~32s** | 用户编辑停 30s 后再 2s |

### 1.3 关键配置（`etc/obsidian/obsidian-git.json` v2）

```json
{
  "commitMessage": "vault: {{date}} {{hostname}}",
  "autoCommit": false,
  "autoPush": false,
  "autoPull": false,
  "disablePush": true,
  "commitOnClose": false,
  "syncMethod": "merge",
  "autoSaveInterval": 30
}
```

| 字段 | 值 | 理由 |
|---|---|---|
| `commitMessage` | `vault: {{date}} {{hostname}}` | 统一前缀 + 时间 + 主机名 |
| `autoCommit` | false | 关闭旧版 autoCommit，让 autoSaveInterval 单独控制 |
| `autoPush` | false | 决策 6（仅本地）|
| `autoPull` | false | 决策 3（不跨设备）|
| `disablePush` | true | 决策 6（防误推）|
| `commitOnClose` | false | 决策 2（用 on-save 而非 on-close）|
| `syncMethod` | merge | 远端 pull/merge 时策略（决策 3+6 后仅本地，无实际意义，保留默认值）|
| `autoSaveInterval` | 30 | 决策 2（30s debounce 防 Templater race）|

## 2. 提交信息生成规则

### 2.1 obsidian-git 默认模板

```json
"commitMessage": "vault: {{date}} {{hostname}}"
```

| 变量 | 展开 | 示例 |
|---|---|---|
| `{{date}}` | `YYYY-MM-DD-HHMM` | `2026-10-15-2130` |
| `{{hostname}}` | `user@host` | `jiuben@jiuben-PC` |

### 2.2 实际 commit message 示例

```
vault: 2026-10-15-2130 jiuben@jiuben-PC
```

（多文件一次 commit；`listChangedFilesInMessageBody=false` 不展开文件清单）

### 2.3 手动 commit（推荐用法）

对结构性改动，建议用语义化 commit message：

```bash
git commit -m "vault: 修复 06-模板/日记模板.md 时间格式

- 改 YYYY-MM-DD 为 YYYY-MM-DD ddd
- 同步 install-obsidian-templates.sh 模板
"
```

### 2.4 提交信息规范

| ✅ 推荐 | ❌ 避免 |
|---|---|
| `vault: 2026-10-15-2130 jiuben@jiuben-PC` | `fix` |
| `vault: 新增 tdx_link 字段到 5 模板` | `update` |
| `vault: rollback 上一 commit` | `add api key sk-xxxx`（含密钥）|
| | `持仓 600519 × 1000 @ 1800`（含真实数据）|

### 2.5 禁止的 commit 信息（pre-commit 钩子拦截）

- 包含 AWS key、OpenAI/Anthropic key、GitHub PAT、GitLab PAT、Slack token、JWT
- 包含明文密码（`password=xxx` / `secret=xxx` / `token=xxx`）
- 真实持仓（`代码 × 数量 × 成本`）

## 3. 冲突处理

### 3.1 syncMethod = merge

obsidian-git 配 `syncMethod: "merge"`，自动 merge 策略。本项目决策 3+6（不跨设备 + 仅本地）= 无远端 = 实际无 merge 冲突。`syncMethod` 保留为默认值以备未来启用。

### 3.2 冲突场景与处理

#### 场景 A：同文件 Obsidian 修改 + 外部工具修改

**触发**：用户在 Obsidian 改笔记的同时，`vim`/`VSCode` 也改了同一文件。

**症状**：
- on-save 30s 后 obsidian-git 触发 commit 成功
- 外部工具保存时 git status 显示冲突

**处理**：
```bash
git status                              # 找冲突文件
cat <冲突文件>                          # 看到 <<<<<<< HEAD / ======= / >>>>>>> 标记
# 手动合并
git add <冲突文件>
git commit -m "vault: resolve conflict in <file>"
```

#### 场景 B：on-save 30s 期间 Templater 写入未完成

**触发**：复杂 Templater 模板渲染耗时 > 30s（如包含多次 API 调用）。

**症状**：
- obsidian-git 在写入未完成时扫描，commit 了不完整内容
- 后续 Templater 完成写入后产生 uncommitted diff

**缓解**：
- 把 `autoSaveInterval` 调大到 60s 或 120s
- 关闭 Templater 自动化渲染

#### 场景 C：pre-commit 钩子失败

**触发**：
- verify 漂移（commit 前模板/配置改了）
- 敏感信息命中

**症状**：
- `git status` 显示 staged 但未 committed
- Obsidian 不会重试

**处理**：
```bash
# 1. 分类
git status
git diff --cached | head -30

# 2. verify 漂移
make verify-obsidian-templates
# 按 [漂移]/[缺失] 修复后重 git commit
# 或：make install-obsidian-templates 把部署同步到源

# 3. 敏感信息命中
# 钩子会打印具体行和模式
# 删除/占位符/移到 vault 之外 后重 commit

# 4. 紧急跳过
git commit --no-verify
```

### 3.3 钩子失败的元规则

| 失败类型 | 失败率 | 元规则 |
|---|---|---|
| verify 漂移 | < 5% | 必须修，不允许 --no-verify |
| 敏感信息命中 | < 1% | 改占位符或 vault 之外存储 |
| 误报（OAuth test key 等）| < 1% | 改进正则或 --no-verify（事后 review）|

**红线**：
- 真实持仓/客户数据 = 永久禁用 --no-verify
- 漂移未修 = 永久禁用 --no-verify
- 仅本地项目 = 临时 --no-verify 可接受（事后 review）

## 4. 紧急操作速查

```bash
# 查看最近 10 次 commit
git log --oneline -10

# 看某次 commit 改了什么
git show <commit-sha>

# 撤销最近一次 commit（保留修改）
git reset --soft HEAD~1

# 撤销最近一次 commit（丢弃修改）
git reset --hard HEAD~1

# 跳过钩子
git commit --no-verify -m "..."

# 临时禁用 on-save 自动 commit
# 在 Obsidian 中: Settings → Community plugins → 关闭 obsidian-git
# 或改 .obsidian/obsidian-git.json: autoSaveInterval=0

# 完全回滚（移除 git 化）
rm -rf /home/jiuben/StockVault/.git
rm -rf /home/jiuben/ObsidianVaults/tdx-data-feed/.git

# 物理备份（建议每周）
rsync -avh --delete /home/jiuben/StockVault/ /mnt/backup/StockVault/
rsync -avh --delete /home/jiuben/ObsidianVaults/tdx-data-feed/ /mnt/backup/ObsidianVaults-tdx-data-feed/
```

## 5. 监控与告警

### 5.1 每周 cron 检查

```bash
# 周一 09:00 检查 commit 健康
# 加入 systemd user timer
cat > /home/jiuben/.local/bin/vault-git-health.sh <<'EOF'
#!/usr/bin/env bash
for v in /home/jiuben/StockVault /home/jiuben/ObsidianVaults/tdx-data-feed; do
  cd "$v" || continue
  if [[ ! -d .git ]]; then
    echo "⚠ $v 无 .git" >&2
    continue
  fi
  # 最近 7 天 commit 数
  count=$(git log --since="7 days ago" --oneline | wc -l)
  echo "✓ $v: $count commits / 7 days"
  # 是否有未推送（仅本地项目可省）
  # git log @{u}.. 2>/dev/null || true
done
EOF
chmod +x /home/jiuben/.local/bin/vault-git-health.sh
```

### 5.2 异常处理

| 异常 | 检测 | 处理 |
|---|---|---|
| 7 天 0 commit | cron 报告 | 检查 obsidian-git 是否仍启用、autoSaveInterval 是否仍为 30 |
| commit 数 > 200/天 | cron 报告 | 检查是否有外部工具误触发写入 |
| .git 体积 > 500MB | `du -sh .obsidian/plugins/*` | 检查 .gitignore 是否生效；清理大文件 |
| pre-commit 失败率 > 30% | 手动统计 | 改进 verify 脚本或敏感正则 |

## 6. 决策溯源

| 决策 | 来自 | 影响 |
|---|---|---|
| 决策 2: obsidian-git on-save | `docs/obsidian_upgrade_plan_20261015.md` | `autoSaveInterval=30` |
| 决策 3: 不跨设备 | `docs/obsidian_upgrade_plan_20261015.md` | `autoPush/Pull=false` |
| 决策 6: git 仅本地 | `docs/obsidian_upgrade_plan_20261015.md` | `disablePush=true` + 不配 remote |
