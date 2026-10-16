# Obsidian 双库升级分阶段实施计划（2026-10-15）

> 配套文档：`docs/obsidian_audit_20261015.md`（差距分析）、`.codebuddy/skills/tdx-obsidian-math-verify/SKILL.md`（数学零漂移验证，v1）、`.codebuddy/skills/tdx-obsidian-version-control/SKILL.md`（git 化流程，v1 新建）。

## 目标

按 P0 → P1 → P2 优先级，把 8 维度差距（共 4×P0、2×P1、2×P2）排期推进，并通过 **skill 联动** 把"源 ↔ 部署零漂移"验证范围从数学库逐步扩展到非数学模板、配置 JSON、媒体与归档治理。

## 阶段总览

| 阶段 | 优先级 | 范围 | 目标 | 关键交付 | 依赖 |
|---|---|---|---|---|---|
| Phase 0 | 准备 | skill 联动基础 | 文档 + 新 skill 落盘 | 本计划、`tdx-obsidian-version-control` SKILL.md | 无 |
| Phase 1 | P0 | 模板+配置+版本控制 | 源-部署闭环扩展 + git 化 | `verify-obsidian-templates` v2（数学+非数学模板+配置 JSON）、两 vault `.git`、`.gitignore` | Phase 0 |
| Phase 2 | P1 | 结构 + 链接 | 数据完整 + 标签字典 | 标的去重脚本、统一 MOC、标签字典、跨库互链 | Phase 1 |
| Phase 3 | P2 | 媒体 + 搜索 | 性能 + 治理 | 独立 `attachments/`、查询模板化、跨库搜索 | Phase 2 |

---

## Phase 0：准备（已完成）

- [x] 8 维度差距盘点（见审计报告）
- [x] 创建 `tdx-obsidian-version-control` skill
- [x] 落盘本计划文档

---

## Phase 1：P0 核心保障（核心：源-部署闭环 + 版本控制）

### Phase 1.1 扩展零漂移验证范围（数学闭环 → 全 vault）

- 在 `etc/obsidian/` 新建 `templates/` 子目录，复制 5 个非数学模板（`00-MOC.md` / `个股研究模板.md` / `回测实验模板.md` / `日记模板.md` / `v4技术面分析模板.md`），建立它们的"单一事实源"
- 升级 `scripts/verify-math-templates.sh` → 改名为 **`scripts/verify-obsidian-templates.sh`**（语义更准），增加 5 项模板 + 5 份配置 JSON 比对
- `Makefile` `verify-math` 目标升级（或新增 `verify-obsidian-templates` 目标），`MATH_VAULT` env 复用
- 配置 JSON 校验：对比 `etc/obsidian/*.json` ↔ vault 内 `.obsidian/*.json`（community / dataview / templater / periodic-notes / obsidian-git），偏差即告警
- 验收：`make verify-obsidian-templates` 报全一致；6 类模板 + 5 份 JSON 全覆盖；升级路径无回退（v1 旧名仍兼容）

### Phase 1.2 插件配置统一

- 单一事实源策略：每个 vault 的"已启用插件 + 插件配置 JSON"以 `etc/obsidian/` 为基准
- `install-math-templates.sh --init-config` 升级为 idempotent 写入 + 触发 verify
- 修正 `periodic-notes.json` 路径：`daily.folder=07-日记`、`daily.template=日记模板.md`（与 StockVault 实际位置一致）
- AI 插件（`copilot` / `smart-connections`）审计：数据外发、嵌入模型、隐私边界，输出审计结论
- 验收：4 套配置碎片化收敛为 1 套基准 + 1 套 vault 增量

### Phase 1.3 `.bak` 清理与 archive 治理

- `.bak-<时间戳>` 文件保留最近 7 份或 7 天，写 `scripts/clean_obsidian_bak.sh`
- archive 治理：写 `scripts/archive_obsidian_reports.sh`，把 30 天前的报告 `zip` 到 `08-报告/archive/YYYY-Qn.zip`
- 接入 systemd user timer（每日凌晨 03:00）
- 验收：`.bak` 数量有上限；archive 不无限增长

### Phase 1.4 两 vault git 化（核心）

- 调用新 skill `tdx-obsidian-version-control` 完成以下动作：
  - `git init` 两个 vault
  - 应用标准 `.gitignore`（含 `.trash/`、`.DS_Store`、`*.bak-*`、`workspace*.json`、插件数据文件）
  - 启用 `obsidian-git`：`commitOnClose=true`、`disablePush=true`（默认仅本地）、`autoCommit=false`
  - 或改为外部 cron：每 30 min 扫描 modified > 5 min 则 commit
- 提交 message 模板：`vault: {{date}} {{path}}`
- 验收：两 vault 都有 `.git`、commit 历史正常、`.gitignore` 生效、`disablePush` 默认开启

---

## Phase 2：P1 数据完整 + 链接

### Phase 2.1 标的笔记去重

- 扫描 `01-标的/`，按 6 位代码分组，识别全/半角 `A`/`Ａ` 重复
- 写 `scripts/dedup_stock_notes.py`：决策保留半角，重命名全角
- 验收：5568 → 5565，0 重复

### Phase 2.2 统一顶层 MOC

- StockVault 根加 `00-MOC.md` 总入口（覆盖 01-标的 / 02-框架 / 03-数据 / 04-回测 / 05-知识库 / 06-模板）
- 项目库已有 `00-知识库入口/索引-MOC.md`，验证
- 验收：两 vault 都有单一顶层 MOC

### Phase 2.3 日记 / 收件箱实际跑通

- 选 daily 路径：StockVault 用 `07-日记`，项目库用 `10-研究日志/daily`（区分个人 / 项目）
- 配 periodic-notes + Templater 模板
- 收件箱：保留 `app.json newFileFolderPath=00-收件箱`，加每周 review 流程
- 验收：连续 7 天有日记

### Phase 2.4 标签字典 + 跨库互链

- 写 `docs/tag_taxonomy.md`：项目 / 数学 / 标的 / 框架 / 回测 / 数据 五大命名空间
- 在项目库加 `[[StockVault/05-知识库/数学/00_索引/总览]]` 等关键互链
- Dataview snippet 模板化：5+ 个常用查询（持仓 / 复习 / 行业 / 待办 / 跨库）
- 验收：标签字典文档、5 个查询 snippet、关键互链 ≥ 10 个

---

## Phase 3：P2 媒体 + 搜索

### Phase 3.1 独立 attachments 目录

- 改 `app.json attachmentFolderPath="attachments"`
- `.gitignore` 排除 `>1MB`（或用 git LFS）
- 标配附件清单模板（标的截图、模型 loss 曲线、回测净值曲线）

### Phase 3.2 搜索性能压测

- 5700 文件下内置搜索响应基线
- Dataview 关键查询 < 1s 验收
- 评估是否需要全文索引增强

### Phase 3.3 跨库搜索

- 外部脚本 `scripts/search_obsidian.sh`：ripgrep 双库 + tag 过滤
- 集成到 QuickAdd 或 shell alias

---

## skill 联动设计

### `tdx-obsidian-math-verify` 演化

| 版本 | 范围 | 触发条件 |
|---|---|---|
| v1（现有） | 数学 7 模板 + MOC | install-math-templates.sh 后 / 提交前 / 手动 |
| v2（Phase 1.1） | v1 + 非数学 5 模板 + 5 份配置 JSON | 同上 |
| v3（Phase 3） | v2 + attachments 目录结构 + archive 命名规范 | 同上 |

### 新建 `tdx-obsidian-version-control` skill

覆盖 Phase 1.4 全流程：
- 两 vault git init（含安全检查：非空目录、未初始化检测）
- `.gitignore` 模板（含 Obsidian / 插件 / 媒体 / 备份四类）
- commit 策略：手动 / on-close / cron 三选一
- `disablePush` 默认（防误推）
- 跨设备同步方案：git push / syncthing / iCloud
- 故障排查：commit 失败、merge 冲突、push 被禁、`.gitignore` 不生效

---

## 时间盒建议

| 阶段 | 工作量估计 | 建议时长 |
|---|---|---|
| Phase 0 | 1h（已完成） | 本周 |
| Phase 1 | 4–6h | 1–2 周 |
| Phase 2 | 3–4h | 1 周 |
| Phase 3 | 2–3h | 1 周 |
| **合计** | **10–14h** | **3–5 周** |

---

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| obsidian-git 与 Templater 写入 race | commit 内容不完整 | 改用 `commitOnClose` 或 cron 延后 |
| 跨设备同步延迟 | 临时方案不可用 | 预留 syncthing / iCloud 备选 |
| AI 插件数据外发 | 隐私泄露 | Phase 1.2 优先决策保留 / 限制 / 下线 |
| archive 治理误删 | 历史报告丢失 | 30 天观察期 + 独立 backup 目录 |
| git init 误操作 | vault 内已有内容覆盖 | skill 内置 dry-run + 备份 |
| 标签字典与既有笔记冲突 | 大量笔记需返工 | 渐进式，先新笔记，老笔记分批迁移 |

---

## 待决策项（用户已决策 2026-10-15）

> 详细落地见 `docs/obsidian_phase_1_1_plan_20261015.md`。

| # | 决策项 | 决策 | 落地阶段 |
|---|---|---|---|
| 1 | 非数学 5 模板纳入源？ | **纳入**（复制到 `etc/obsidian/templates/`）| Phase 1.1 |
| 2 | obsidian-git 策略？ | **on-save**（`autoSaveInterval=30` debounce 防 Templater race）| Phase 1.1 + 1.4 |
| 3 | 跨设备同步？ | **不开启**（无 syncthing / iCloud / 远端）| Phase 1.1 + 1.4 |
| 4 | AI 插件审计？ | **均纳入**（copilot / smart-connections 带白名单约束）| Phase 1.1 + 1.2 |
| 5 | archive 保留期？ | **年度**（每年 1/1 zip 上一年报告）| Phase 1.1 + 1.3 |
| 6 | git 远端？ | **仅本地**（`disablePush=true`，不配 remote）| Phase 1.1 + 1.4 |
