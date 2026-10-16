# Obsidian Phase 1.2 详细计划：插件配置统一（2026-10-15）

> 配套：`docs/obsidian_upgrade_plan_20261015.md`（总计划 Phase 1.2）、`docs/obsidian_phase_1_1_plan_20261015.md`（Phase 1.1）、`docs/obsidian_phase_1_4_plan_20261015.md`（Phase 1.4，本阶段紧后）。

## 1. 背景与目标

**Phase 1.1 末态**：`make verify-obsidian-templates` 报 4 份配置 JSON 漂移：
- `community-plugins.json` 漂移（StockVault 8 个 vs 源 4 个）
- `templater.json` StockVault 部署缺失
- `periodic-notes.json` StockVault 部署缺失
- `obsidian-git.json` StockVault 部署缺失

**问题诊断**：源-部署比对模型不匹配。两 vault **功能定位不同**（StockVault = 个人综合 + 数学；Project = 量化项目），其插件启用清单、Templater folder_templates、periodic-notes 路径必然不同。强行共用单一源 = 错配。

**目标**：建立**共享 + 分 vault** 双层源结构，让 verify 0 漂移。

## 2. 源结构（升级前后对比）

### 升级前（Phase 1.1 末态）

```
etc/obsidian/
├── community-plugins.json        # 4 个（项目口径）
├── dataview.json
├── templater.json
├── periodic-notes.json
├── obsidian-git.json
├── math/                         # 7 模板 + MOC
└── templates/                    # 5 模板
```

### 升级后（Phase 1.2 目标）

```
etc/obsidian/
├── obsidian-git.json             # 共享（决策 2/3/6 → 两 vault 同）
├── dataview.json                 # 共享（查询设置同）
├── math/                         # 数学模板（不变）
├── templates/                    # 非数学模板（不变）
├── stockvault/                   # ★ 新建：StockVault 专属
│   ├── community-plugins.json    # 8 个（smart-connections/copilot/dataview/templater/recent-files/tag-wrangler/various-complements/SR）
│   ├── templater.json            # templates_folder=templates, folder_templates=[]
│   └── periodic-notes.json       # daily → 07-日记, template=日记模板.md
└── project/                      # ★ 新建：项目 vault 专属
    ├── community-plugins.json    # 4 个（dataview/templater/periodic-notes/obsidian-git）
    ├── templater.json            # templates_folder=templates, folder_templates=[]
    └── periodic-notes.json       # daily → 10-研究日志/daily, template=templates/Daily-Note.md
```

## 3. 各文件差异点的合并规则

| 维度 | 共享规则 | 分 vault 规则 |
|---|---|---|
| `community-plugins.json` | — | **按 vault 角色分**：StockVault 启用数学 + AI 工具栈（SR/smart-connections/copilot/各种 complements）；Project 仅启用核心 4 件套 |
| `dataview.json` | **共享**（查询偏好跨 vault 一致）| — |
| `templater.json` | — | **按 vault 分**：folder_templates 各自维护（math vault 的 domain→模板映射 vs project 的 sync-from-repo 模板）|
| `periodic-notes.json` | — | **按 vault 分**：daily 路径不同（07-日记 vs 10-研究日志/daily）、template 引用不同（06-模板/日记模板.md vs templates/Daily-Note.md）|
| `obsidian-git.json` | **共享**（决策 2/3/6 → 两 vault 同：on-save 30s debounce + disablePush + 无 push/pull）| — |

**判定原则**：
- 若两 vault 当前内容**已相同**（如 dataview.json）→ 共享在 `etc/obsidian/` 根
- 若两 vault 当前内容**不同**或**应不同** → 进 `<vault>/` 子目录
- 漂移是否"真"漂移：升级后 verify 通过 = 不再算漂移

## 4. 涉及的文件

### 源（仓库内）

| 文件 | 状态 |
|---|---|
| `etc/obsidian/community-plugins.json` | 删除（迁到 stockvault/、project/）|
| `etc/obsidian/templater.json` | 删除（迁到 stockvault/、project/）|
| `etc/obsidian/periodic-notes.json` | 删除（迁到 stockvault/、project/）|
| `etc/obsidian/obsidian-git.json` | 保留（共享）|
| `etc/obsidian/dataview.json` | 保留（共享）|
| `etc/obsidian/stockvault/community-plugins.json` | 新建（8 插件）|
| `etc/obsidian/stockvault/templater.json` | 新建 |
| `etc/obsidian/stockvault/periodic-notes.json` | 新建 |
| `etc/obsidian/project/community-plugins.json` | 新建（4 插件）|
| `etc/obsidian/project/templater.json` | 新建 |
| `etc/obsidian/project/periodic-notes.json` | 新建 |

### 部署（vault 内）

| 文件 | StockVault | Project |
|---|---|---|
| `.obsidian/community-plugins.json` | 内容更新（8 插件，与现 Vault 同）| 内容更新（4 插件，与现 Vault 同）|
| `.obsidian/templater.json` | 新建 | 已存在（不变）|
| `.obsidian/periodic-notes.json` | 新建 | 已存在（不变）|
| `.obsidian/obsidian-git.json` | 新建（v2，决策 2/3/6）| 内容更新（v1 → v2）|
| `.obsidian/dataview.json` | 已存在（不变）| 已存在（不变）|

### 脚本与 Makefile

| 文件 | 变更 |
|---|---|
| `scripts/verify-obsidian-templates.sh` | 升级 v2.1：支持 `--vault-kind stockvault\|project`，比对 共享 + per-vault |
| `scripts/install-obsidian-templates.sh` | 新建：部署 JSON 到 vault `.obsidian/`，备份 `.bak-<ts>` |
| `Makefile` | 新增 `install-obsidian-templates` 目标；`verify-obsidian-templates` 接受 `VAULT_KIND` env |

## 5. 实施步骤

### Step 1：建立分 vault 源（写文件）
- 写 `etc/obsidian/stockvault/{community-plugins,templater,periodic-notes}.json`
- 写 `etc/obsidian/project/{community-plugins,templater,periodic-notes}.json`
- 内容基于**当前 vault 实际状态**（Phase 1.1 已收集 inventory）

### Step 2：删除 etc/obsidian/ 根的 per-vault 文件
- `community-plugins.json`、`templater.json`、`periodic-notes.json`（共享的 `obsidian-git.json`、`dataview.json` 保留）

### Step 3：升级 verify 脚本（v2.0 → v2.1）
- 新增 `--vault-kind {stockvault,project}` 必选
- 合并源路径：`SRC_SHARED=$REPO_ROOT/etc/obsidian`、`SRC_VAULT=$REPO_ROOT/etc/obsidian/$KIND`
- 5 份 JSON 各自的源路径映射：
  - community-plugins → SRC_VAULT
  - dataview → SRC_SHARED
  - templater → SRC_VAULT
  - periodic-notes → SRC_VAULT
  - obsidian-git → SRC_SHARED

### Step 4：升级 Makefile
- `verify-obsidian-templates` 接受 `VAULT_KIND` env（默认按 vault 路径启发式推断：路径含 `StockVault` → stockvault，否则 project）
- 新增 `install-obsidian-templates` 目标

### Step 5：写 install 脚本
- 备份 vault `.obsidian/*.json` 为 `.obsidian/*.json.bak-<ts>`
- 从源复制到 vault `.obsidian/`
- 幂等（重复执行内容不变）

### Step 6：跑 verify 报全一致

## 6. 回滚方案

### 6.1 自动回滚（install 触发）
- `install-obsidian-templates.sh` 每次覆盖前备份为 `.bak-<时间戳>`
- 回滚单文件：`mv .obsidian/community-plugins.json.bak-20261015-130358 .obsidian/community-plugins.json`

### 6.2 全 vault 回滚
Phase 1.2 实施前**预先**对两 vault 做快照：

```bash
# 实施前快照（手动执行）
TS=$(date +%Y%m%d-%H%M%S)
cp -r /home/jiuben/StockVault/.obsidian /home/jiuben/StockVault/.obsidian.pre-1.2.${TS}
cp -r /home/jiuben/ObsidianVaults/tdx-data-feed/.obsidian \
      /home/jiuben/ObsidianVaults/tdx-data-feed/.obsidian.pre-1.2.${TS}
```

回滚：
```bash
# 整 vault 回滚
rm -rf /home/jiuben/StockVault/.obsidian
mv /home/jiuben/StockVault/.obsidian.pre-1.2.<ts> /home/jiuben/StockVault/.obsidian
```

### 6.3 verify 一致性回滚
- `make verify-obsidian-templates` 失败 → 不要 install；先 diff 看差异
- 单一文件回滚：cp `etc/obsidian/<kind>/<file>.json` `.obsidian/<file>.json`

### 6.4 已知风险与缓解
- **templater folder_templates 不匹配**：phase 1.4 之前无 git，folder_templates 是 Obsidian 应用层状态；install 仅覆盖 JSON 不动 folder_templates
- **periodic-notes 路径错误**：phase 1.2 install 后，第一次创建 daily note 时按新路径走；若旧路径已有笔记，需手动 mv
- **AI 插件启用变化**：StockVault 的 community-plugins 保持当前 8 个不变（无功能变化）；Phase 1.2 不会启用/禁用任何插件

## 7. 验证方式

```bash
# 1. 源结构就位
ls etc/obsidian/stockvault/  # 3 个 JSON
ls etc/obsidian/project/     # 3 个 JSON
ls etc/obsidian/*.json        # 仅 obsidian-git.json + dataview.json

# 2. 共享源未被改写
diff etc/obsidian/dataview.json /home/jiuben/StockVault/.obsidian/dataview.json
diff etc/obsidian/dataview.json /home/jiuben/ObsidianVaults/tdx-data-feed/.obsidian/dataview.json
# 两个 diff 都应无输出

# 3. 跑 verify
make verify-obsidian-templates VAULT=/home/jiuben/StockVault VAULT_KIND=stockvault
make verify-obsidian-templates VAULT=/home/jiuben/ObsidianVaults/tdx-data-feed VAULT_KIND=project
# 两个 verify 都应 exit 0，13 模板 + 5 JSON 全 [一致]

# 4. install 幂等
bash scripts/install-obsidian-templates.sh --vault /home/jiuben/StockVault --kind stockvault
make verify-obsidian-templates VAULT=/home/jiuben/StockVault VAULT_KIND=stockvault
# 仍应 exit 0
```

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 删除 etc/obsidian/ 根的 per-vault JSON 失误 | 源数据丢失 | 写前已通过 inventory 确认；删除前 cp 到 `/tmp/` 暂存 |
| install 误覆盖用户个性化配置 | 笔记体验变化 | 备份 `.bak-<ts>`；Phase 1.2 严格按"当前 vault 状态"= 源，无功能变化 |
| 两 vault 共享 JSON 不一致 | 行为差异 | dataview.json 当前已同；obsidian-git.json Phase 1.4 才统一 v2 |
| 共享源被无意改写 | 跨 vault 行为变化 | Makefile 注释明确共享规则；verify 双向比对 |
| Phase 1.4 git 化前 .bak 堆积 | 部署侧膨胀 | Phase 1.4 启用 `.gitignore` 后自动排除 |

## 9. 工作量估计

| 步骤 | 估计 |
|---|---|
| 1. 写分 vault 源 | 0.3h |
| 2. 删除根 per-vault 文件 | 0.1h |
| 3. 升级 verify v2.1 | 0.5h |
| 4. 升级 Makefile | 0.2h |
| 5. 写 install 脚本 | 0.5h |
| 6. install + verify 端到端 | 0.5h |
| **合计** | **2.1h** |
