# Obsidian Phase 1.1 详细计划：扩展源-部署零漂移验证范围（2026-10-15）

> 配套：`docs/obsidian_upgrade_plan_20261015.md`（总计划）、`.codebuddy/skills/tdx-obsidian-math-verify/SKILL.md`（v1，待升级为 v2）、`.codebuddy/skills/tdx-obsidian-version-control/SKILL.md`（commit 前置 verify）。

## 1. Phase 1.1 范围与目标

**范围**：把 8 维度差距分析中的 P0 第 1 项（模板与元数据规范）从"数学闭环"扩展到"全 vault 闭环"，同步落地本批次 6 项决策。

**目标**：
- 非数学 5 模板纳入 Obsidian 源管理（决策 1）
- 配置 JSON（5 份）纳入源-部署比对
- 验证脚本升级为 v2，全 vault 一次跑通
- 落地 obsidian-git on-save 策略（决策 2），并接入"commit 前 verify"钩子
- 落地不跨设备同步（决策 3）+ 仅本地 git（决策 6）配置
- AI 插件 copilot / smart-connections 审计基线（决策 4）
- archive 年度保留治理（决策 5）

**不在范围**：标的去重（Phase 2.1）、统一 MOC（Phase 2.2）、日记/收件箱跑通（Phase 2.3）、标签字典（Phase 2.4）、媒体目录（Phase 3.1）、搜索压测（Phase 3.2）。

---

## 2. 6 决策落地

| # | 决策 | 落地方式 | 影响范围 |
|---|---|---|---|
| 1 | 5 模板纳入源 | 复制到 `etc/obsidian/templates/`，verify 范围扩展 | Phase 1.1 直接 |
| 2 | obsidian-git on-save | `autoSaveInterval=30`（防 Templater race）+ pre-commit verify 钩子 | Phase 1.1 + 1.4 |
| 3 | 不跨设备同步 | 无远端、无 syncthing、无 iCloud；`autoPull=false`、`autoPush=false` | Phase 1.1 + 1.4 |
| 4 | AI 插件均纳入 | copilot / smart-connections 启用 + 审计基线（白名单/无密钥） | Phase 1.1 + 1.2 |
| 5 | archive 年度保留 | 每年 1/1 自动 zip 上一年报告到 `08-报告/archive/YYYY.zip` | Phase 1.1 + 1.3 |
| 6 | git 仅本地 | `disablePush=true`、不配置 remote、不开 syncthing | Phase 1.1 + 1.4 |

---

## 3. 实施步骤（详细）

### 步骤 1：建立非数学模板源

```bash
mkdir -p /home/jiuben/tdx-data-feed/etc/obsidian/templates

# 复制 5 个非数学模板
cp /home/jiuben/StockVault/06-模板/00-MOC.md           /home/jiuben/tdx-data-feed/etc/obsidian/templates/
cp /home/jiuben/StockVault/06-模板/个股研究模板.md     /home/jiuben/tdx-data-feed/etc/obsidian/templates/
cp /home/jiuben/StockVault/06-模板/回测实验模板.md     /home/jiuben/tdx-data-feed/etc/obsidian/templates/
cp /home/jiuben/StockVault/06-模板/日记模板.md         /home/jiuben/tdx-data-feed/etc/obsidian/templates/
cp /home/jiuben/StockVault/06-模板/v4技术面分析模板.md /home/jiuben/tdx-data-feed/etc/obsidian/templates/

# 写入 README 说明源-部署对应关系
cat > /home/jiuben/tdx-data-feed/etc/obsidian/templates/README.md <<'EOF'
# 非数学模板源（v1）

5 个模板的唯一事实源。部署位置为 `StockVault/06-模板/`，由本仓库 make install-obsidian-templates 同步。

| 源文件 | 部署路径 | 用途 |
|---|---|---|
| 00-MOC.md | 06-模板/00-MOC.md | 模板目录的 MOC |
| 个股研究模板.md | 06-模板/个股研究模板.md | 个股研究笔记 |
| 回测实验模板.md | 06-模板/回测实验模板.md | 回测实验笔记 |
| 日记模板.md | 06-模板/日记模板.md | 日常研究日志 |
| v4技术面分析模板.md | 06-模板/v4技术面分析模板.md | v4 技术面分析 |

修改流程：改源 → make install-obsidian-templates → make verify-obsidian-templates。
EOF
```

### 步骤 2：升级 verify 脚本为 v2

升级 `scripts/verify-math-templates.sh` → 改名为 **`scripts/verify-obsidian-templates.sh`**，新增 5 模板 + 5 份配置 JSON 比对：

```bash
# scripts/verify-obsidian-templates.sh（v2 关键逻辑）
SRC_NONMATH="$REPO_ROOT/etc/obsidian/templates"
SRC_JSON_DIR="$REPO_ROOT/etc/obsidian"
VAULT_OBS="${VAULT}/.obsidian"

# 6.1 验证非数学 5 模板
for f in "$SRC_NONMATH"/{00-MOC.md,个股研究模板.md,回测实验模板.md,日记模板.md,v4技术面分析模板.md}; do
  [[ -e "$f" ]] || continue
  check_one "$f" "$VAULT/06-模板/$(basename "$f")" "非数学模板 $(basename "$f")"
done

# 6.2 验证配置 JSON（5 份）
for j in community-plugins dataview templater periodic-notes obsidian-git; do
  src="$SRC_JSON_DIR/$j.json"
  dst="$VAULT_OBS/$j.json"
  [[ -f "$src" ]] || continue
  # JSON 规范化比对（jq -S 排序后 cmp）
  if cmp -s <(jq -S . "$src") <(jq -S . "$dst" 2>/dev/null) ; then
    echo "[一致] 配置 $j.json"
  else
    echo "[漂移] 配置 $j.json -> $dst"
    drift=1
  fi
done
```

依赖检查：`jq` 必须可用（已在 PATH 或 venv）。

### 步骤 3：升级 Makefile

```makefile
# Makefile 升级（保留旧 verify-math 向后兼容）
verify-obsidian-templates:  ## 校验全 vault 源 ↔ 部署零漂移（数学 + 非数学 + 配置 JSON）
	@bash $(SCRIPTS)/verify-obsidian-templates.sh --vault $${VAULT:-/home/jiuben/StockVault} --prefix $${PREFIX:-} --tpl-dir $${TPL_DIR:-06-模板} --nonmath-tpl-dir $${NONMATH_TPL_DIR:-06-模板}

verify-math:  ## 兼容旧名，等价 verify-obsidian-templates
	@$(MAKE) verify-obsidian-templates
```

### 步骤 4：obsidian-git on-save 集成（决策 2/3/6）

`etc/obsidian/obsidian-git.json`（v2 配置）：

```json
{
  "version": "2.0.0",
  "settings": {
    "commitMessage": "vault: {{date}} {{hostname}}",
    "autoCommit": false,
    "autoPush": false,
    "autoPull": false,
    "disablePush": true,
    "commitOnClose": false,
    "syncMethod": "merge",
    "refreshSourceControl": true,
    "differentInterval": 10,
    "autoSaveInterval": 30
  }
}
```

> `autoSaveInterval=30` 实现"on-save 风格"——30 秒 debounce 避免与 Templater race（决策 2）；`disablePush=true` + `autoPush=false` + 不配 remote 满足"仅本地"（决策 3/6）。

**commit 前 verify 钩子**（`scripts/pre-commit-verify.sh`，受 `core.hooksPath` 控制）：

```bash
#!/usr/bin/env bash
# 任何 git commit 前先跑 verify（Phase 1.1 落地）
set -e
cd /home/jiuben/tdx-data-feed
make verify-obsidian-templates >/dev/null 2>&1 || {
  echo "[pre-commit] verify-obsidian-templates 失败，拒绝 commit"
  echo "  跑 \`make verify-obsidian-templates\` 查看漂移项"
  exit 1
}
```

挂载：

```bash
git -C /home/jiuben/StockVault config core.hooksPath /home/jiuben/tdx-data-feed/scripts/hooks
cp /home/jiuben/tdx-data-feed/scripts/pre-commit-verify.sh /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit
chmod +x /home/jiuben/tdx-data-feed/scripts/hooks/pre-commit
```

> 注：commit 钩子受决策 6（仅本地）影响——本机提交能拦截，跨设备无法强制；本项目不跨设备故可接受。

### 步骤 5：AI 插件审计基线（决策 4）

两插件均纳入，但加约束。审计基线写 `docs/ai_plugin_audit_20261015.md`：

#### copilot 审计结论：**纳入**

- 启用：保留（`/home/jiuben/StockVault/.obsidian/community-plugins.json` 第 2 项）
- 数据存储：`StockVault/copilot/system-prompts/`（本地）
- 隐私约束：
  - 不在 system-prompts/ 写入任何含真实持仓、API key、token 的内容
  - 写一段 README 提示"勿放敏感信息"
- 嵌入模型：使用本地嵌入（默认），不外发
- 验收：`copilot/system-prompts/` 仅放 prompt 模板，无密钥

#### smart-connections 审计结论：**纳入**

- 启用：保留
- 数据存储：嵌入向量在 `.obsidian/plugins/smart-connections/`（受 `.gitignore` 排除）
- 隐私约束：
  - 嵌入计算可用本地（默认）或远端（按用户偏好）
  - 笔记内容走嵌入 = 视为内嵌 vault 内容；不视为外发（除非远端模型）
- 验收：vault `.gitignore` 排除 `.obsidian/smart-connections/`

#### 共同约束（白名单）

| 行为 | 允许 | 禁止 |
|---|---|---|
| 笔记内容本地嵌入 | ✓ | |
| 笔记内容外发 | | ✓（除非显式启用远端模型并文档化）|
| 写 system-prompts 模板 | ✓ | |
| 写含密钥的 prompt | | ✓ |
| 远端模型（OpenAI/Claude）调用 | 用户显式启用 | 默认可关 |

### 步骤 6：archive 年度治理（决策 5）

`scripts/archive_obsidian_annual.sh`：

```bash
#!/usr/bin/env bash
# archive_obsidian_annual.sh
# 每年 1/1 把上一年报告 zip 到 08-报告/archive/<YYYY>.zip
set -uo pipefail
REPORTS="/home/jiuben/ObsidianVaults/tdx-data-feed/08-报告"
ARCHIVE="$REPORTS/archive"
TS="$(date +%Y)"
PREV_YEAR=$((TS - 1))

mkdir -p "$ARCHIVE"
# 选去年生成的 .md（按 mtime 判定：上年 1/1 至 12/31 23:59）
find "$REPORTS" -maxdepth 1 -name "*.md" -newermt "$PREV_YEAR-01-01" ! -newermt "$PREV_YEAR-12-31 23:59:59" -print0 \
  | xargs -0 zip -j "$ARCHIVE/$PREV_YEAR.zip"
echo "✓ $PREV_YEAR 报告已归档: $ARCHIVE/$PREV_YEAR.zip"
```

接入 systemd user timer：

```ini
# systemd/archive-obsidian-annual.service
[Service]
Type=oneshot
ExecStart=/home/jiuben/tdx-data-feed/scripts/archive_obsidian_annual.sh

# systemd/archive-obsidian-annual.timer
[Timer]
OnCalendar=*-01-01T01:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

---

## 4. 配置文件总览（本阶段落盘）

| 配置 | 路径 | 受决策 | 内容 |
|---|---|---|---|
| 非数学模板源 | `etc/obsidian/templates/` | 1 | 5 模板 + README |
| verify 脚本 v2 | `scripts/verify-obsidian-templates.sh` | 1 | 数学 + 非数学 + JSON |
| Makefile | `Makefile` | 1 | verify-obsidian-templates + verify-math 兼容 |
| obsidian-git.json v2 | `etc/obsidian/obsidian-git.json` | 2/3/6 | on-save 30s debounce + disablePush + 无 push |
| pre-commit 钩子 | `scripts/hooks/pre-commit` | 1 | commit 前 verify |
| AI 插件审计文档 | `docs/ai_plugin_audit_20261015.md` | 4 | copilot + smart-connections 基线 |
| `.gitignore` 模板 | 两 vault 根 | 4/6 | 排除插件数据 + 嵌入缓存 |
| archive 脚本 | `scripts/archive_obsidian_annual.sh` | 5 | 1/1 自动 zip 上年 |
| archive systemd | `systemd/archive-obsidian-annual.{service,timer}` | 5 | 1/1 01:00 触发 |

---

## 5. 验收清单

| 验收项 | 判据 |
|---|---|
| 非数学 5 模板在源 | `ls etc/obsidian/templates/` 含 5 份 `.md` + README |
| verify v2 一次跑通 | `make verify-obsidian-templates` 退出码 0；覆盖 6 类模板 + 5 JSON |
| 旧 verify-math 仍兼容 | `make verify-math` 等价 verify-obsidian-templates |
| obsidian-git on-save 生效 | `autoSaveInterval=30`，改动后 30s 内自动 commit |
| commit 前 verify 拦截 | 故意改 deployment 制造漂移 → 尝试 commit → 被 pre-commit 拒绝 |
| 无跨设备配置 | `git remote -v` 空；`disablePush=true`；无 syncthing 进程 |
| AI 插件审计文档就位 | `docs/ai_plugin_audit_20261015.md` 存在；约束白名单 5 条 |
| AI 插件 `.gitignore` 生效 | `git check-ignore` 命中 `.obsidian/plugins/*/data.json`、`copilot/index.json` |
| archive 年度脚本就位 | `archive_obsidian_annual.sh` 可 dry-run；systemd timer 启用 |
| 全 vault `.git`（Phase 1.4 入口）| 暂不要求，但 `.gitignore` 已为 Phase 1.4 准备 |

---

## 6. 与其他阶段的衔接

- **Phase 1.2**（插件配置统一）→ 复用本阶段 `obsidian-git.json` v2 配置
- **Phase 1.3**（.bak 清理与 archive）→ archive 年度保留（决策 5）已并入本阶段
- **Phase 1.4**（两 vault git 化）→ pre-commit 钩子（步骤 4）已就位；`.gitignore` 模板就位；`obsidian-git.json` v2 配置可立即启用
- **Phase 2.x**（结构/链接）→ verify 范围已扩展，可顺手校验笔记 frontmatter
- **Phase 3.x**（媒体/搜索）→ verify 范围未来扩展到 attachments/ 目录

---

## 7. 风险与缓解

| 风险 | 来源 | 缓解 |
|---|---|---|
| Templater 写入与 autoSaveInterval race | 决策 2 | 30s debounce + pre-commit verify 兜底 |
| 配置 JSON `jq` 依赖 | 步骤 2 | verify 脚本启动检查 `command -v jq` |
| pre-commit 钩子被绕过 | 步骤 4 | 仅本地项目，绕过风险低；定期 `make verify-obsidian-templates` |
| AI 插件嵌入数据外发 | 决策 4 | `.gitignore` 排除嵌入缓存；审计文档白名单约束 |
| archive 误删本年报告 | 决策 5 | 脚本只处理 mtime 在 `[YYYY-01-01, YYYY-12-31]` 范围内的文件；dry-run 模式 |
| 5 模板复制时遗漏 metadata | 步骤 1 | 复制后 `ls -l` 比对大小；`make verify-obsidian-templates` 兜底 |

---

## 8. 工作量估计

| 步骤 | 估计 |
|---|---|
| 1. 建立非数学模板源 | 0.3h |
| 2. 升级 verify 脚本 | 1.0h（含 `jq` 依赖检查、JSON 规范化、错误处理）|
| 3. 升级 Makefile | 0.2h |
| 4. obsidian-git on-save + pre-commit | 0.5h |
| 5. AI 插件审计基线 | 0.5h |
| 6. archive 年度治理 | 0.5h |
| 端到端验收 | 0.5h |
| **合计** | **3.5h** |

---

## 9. 落地后 `tdx-obsidian-math-verify` skill 升级到 v2

完成后，`.codebuddy/skills/tdx-obsidian-math-verify/SKILL.md` 同步更新：
- frontmatter `description` 加入"非数学模板 + 配置 JSON"覆盖
- 正常执行标准从 7 项扩到 9 项（含 JSON 校验、pre-commit 钩子）
- 调用方式从 `make verify-math` 升级为 `make verify-obsidian-templates`，旧名保留为 alias

可与本计划同步实施。
