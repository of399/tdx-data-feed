# Obsidian 知识库改造方案（tdx-data-feed）

> 制定日期：2026-10-11
> 目标：把当前"单向复制 + 人工记忆 00–08 目录"的 Obsidian 知识库，升级为"标签驱动检索 + 自动索引 + 研究闭环 + 事件触发同步"的结构，同时**仍以 `scripts/sync-to-obsidian.sh` 为单一事实源**，不把 vault 当源码仓库。

---

## 0. 现状核对（基于实测）

| 项 | 实测结果 | 结论 |
|---|---|---|
| vault 路径 | `/home/jiuben/ObsidianVaults/tdx-data-feed` | 外部目录，脚本单向写 |
| vault `.obsidian/` | 仅 `app.json` + `appearance.json`，**无 `community-plugins.json`** | Dataview/Templater 等**均未安装** → 方向 1 是起点 |
| 同步方式 | `sync-to-obsidian.sh` 复制 9 类文档到 `00-知识库入口`~`08-报告` | 硬编码目录，靠人工记忆 |
| 报告同步 | 只保留最新一份 F5/F6，旧报告丢失 | 需 `archive/` 保留历史 |
| 触发方式 | 脚本自带 `--watch` 60s 轮询 | 浪费资源，改为 systemd `.path` |
| systemd | 已有 `tdxfeed-sync.{path,service}`、`plan-e-journal.{service,timer}` | 可作范本/复用 |
| vault 陈旧目录 | `06-运维与监控`、`07-同步工具`、`99-Changelog` 脚本未填充 | 建议清理或纳入脚本 |

---

## 1. 优先级总览

| 优先级 | 方向 | 收益 | 成本 | 落地载体 |
|---|---|---|---|---|
| **P0 必做** | ① Dataview 自动索引/MOC | 消除硬编码目录，按 tag/同步时间/源查漂移 | 装插件 + 脚本生成 MOC | `sync-to-obsidian.sh` `generate_moc()` |
| **P1 中** | ④ 同步脚本增强 | MOC 自动生成 + 报告历史 + 事件触发 | 改脚本 + 新增 2 个 systemd 单元 | `sync-to-obsidian.sh`、`systemd/obsidian-sync.*` |
| **P1 中** | ② Templater 统一 frontmatter | 字段一致、可扩展（doc_type/related_strategy） | 改脚本 + 模板文件 | `sync-to-obsidian.sh`、`etc/obsidian/templates/` |
| **P1 中** | ③ Periodic Notes + plan_e_journal | 研究日志与实盘/模拟信号双向链接 | 装插件 + 模板 | `etc/obsidian/templates/Daily-Note.md` |
| **P2 可选** | ⑤ obsidian-git 库内版本化 | vault 本地变更历史 | 装插件 + 配置（禁 push） | `etc/obsidian/obsidian-git.json` |
| **P3 评估后** | ⑥ 双向同步 | Obsidian 写笔记回写仓库 | 改造较大 | 待确认需求 |

---

## 2. 方向① Dataview（P0 必做）

**目标**：用标签查询替代人工记忆的 00–08 目录，支持"按同步时间查最新""按源文件查漂移"。

**做法**
1. 在 Obsidian 安装 Dataview 社区插件（见 §8 步骤）。
2. 同步脚本每次运行自动生成 `00-知识库入口/索引-MOC.md`，内含三段 Dataview 查询：
   - 按 `synced_at` 倒序列出全部同步文档；
   - 按 `source` 列出 `source_hash`，用于对比源文件当前 sha256 判断漂移；
   - 按 `doc_type` 分类。
3. 所有同步文档已带 `#tdx-data-feed` 标签，MOC 自身用 `#moc` 排除自身。

**查询示例**（写入 MOC 笔记，Obsidian 内实时渲染）：
```dataview
TABLE source AS 源文件, synced_at AS 同步时间, source_hash AS 哈希, line_count AS 行数
FROM #tdx-data-feed AND -#moc
SORT synced_at DESC
```

**验证**：同步后打开 MOC 笔记，应能列出全部文档并可点击跳转。
**风险**：Dataview 为只读查询，不影响源文件；若不装插件，MOC 笔记会显示原始查询代码（无害）。

---

## 3. 方向② Templater 统一 frontmatter（P1）

**目标**：字段一致且可扩展。

**做法**
- 脚本生成的 frontmatter 已扩展为统一 schema（见 `sync-to-obsidian.sh` `copy_doc`）：
  ```yaml
  title / source / source_hash / synced_at / line_count
  doc_type        # 新增：入口/概览/策略/数据…
  related_strategy # 新增：如 plan_e
  tags: [tdx-data-feed, documentation]
  ```
- 人工在 Obsidian 内新建的笔记使用 `etc/obsidian/templates/Synced-Doc.md` 模板，保证手动笔记与自动同步文档字段一致。
- `templater.json` 已配置 `templates_folder: "templates"`，并在文件创建时自动套用。

**验证**：任意同步文档 frontmatter 含 `doc_type`；手动新建笔记自动带相同字段。
**风险**：frontmatter 格式变更不影响已有文档渲染。

---

## 4. 方向③ Periodic Notes + plan_e_journal（P1）

**目标**：把每日实盘/模拟信号与研究笔记形成闭环。

**做法**
- 复用现有 `plan-e-journal.service`（`--mode daily` 写 `data/plan_e/live/journal.csv` 与 `sim`）——它已在每个工作日 18:10 运行。
- 安装 Periodic Notes，配置每日笔记落到 `10-研究日志/daily/`，模板 `etc/obsidian/templates/Daily-Note.md`。
- 每日笔记通过 `[[journal-YYYY-MM-DD]]` 双向链接到当天的 journal 记录，研究者可在笔记里写观察/假设/下一步，并与信号记录互链。

**验证**：在 Obsidian 用 Periodic Notes 创建当日笔记，能自动带日期与链接占位。
**风险**：仅新增目录 `10-研究日志/`，不改同步脚本既有行为。

---

## 5. 方向④ 同步脚本增强（P1）

**已落地**（见 `scripts/sync-to-obsidian.sh`）：
1. `generate_moc()`：每次同步生成 `00-知识库入口/索引-MOC.md`（配合 Dataview）。
2. `archive_report()`：在 `08-报告/archive/` 下按内容 sha256 去重归档每次的 F5/F6 报告，**保留回测演进历史**。
3. 新增 `systemd/obsidian-sync.path` + `systemd/obsidian-sync.service`，用 `PathModified=` 监听源文档变更（docs/、README、DELIVERY、CHANGELOG、Makefile、training/eval、data/plan_e），**替代 60s 轮询**，更省资源且更及时。

**验证**：
```bash
bash scripts/sync-to-obsidian.sh --dry-run   # 语法/路径检查
systemctl --user enable --now obsidian-sync.path
```
**风险**：`PathModified` 在部分文件系统对"仅内容修改"触发不如新增/删除灵敏；如需兜底，可保留一个低频 timer。已在服务里 `Type=oneshot`。

---

## 6. 方向⑤ obsidian-git（P2 可选）

**目标**：vault 本地变更历史（vault 是生成副本，不应当源码仓库）。

**做法**
- 安装 obsidian-git，`etc/obsidian/obsidian-git.json` 已设 `autoPush:false`、`disablePush:true`、`commitOnClose:true` —— **只本地提交，不推送、不替代源码仓库**。
- 在 vault 内 `git init`（不加 remote）。

**验证**：关闭 Obsidian 时自动生成一条本地 commit。
**风险**：若误开 push 且加了 remote，可能把 vault 推到远端——配置已禁用，仍需人工确认未配置 remote。

---

## 7. 方向⑥ 双向同步（P3 评估后）

**决策点**：是否真的需要在 Obsidian 里写研究笔记并**回写仓库**？
- 若否：保持现状（单向 + ①~⑤ 增强）即可，不实施。
- 若是：需引入"Obsidian 笔记 → 仓库 docs/"的回写通道（如脚本反向扫描 `10-研究日志/` 或 obsidian-git +  hook），改造较大，建议单独立项。

---

## 8. 落地步骤清单（按顺序）

1. **装插件**：Obsidian → 设置 → 社区插件，安装 `Dataview`、`Templater`、`Periodic Notes`；可选 `obsidian-git`。
   - 或把 `etc/obsidian/community-plugins.json` 复制到 `vault/.obsidian/` 后重启 Obsidian（插件二进制仍须经 UI 首次安装）。
2. **套用设置**：把 `etc/obsidian/{dataview,templater,periodic-notes,obsidian-git}.json` 复制到 `vault/.obsidian/`。
3. **放模板**：把 `etc/obsidian/templates/` 复制到 `vault/templates/`。
4. **清理陈旧目录**（可选）：删除 vault 下未被脚本填充的 `06-运维与监控`、`07-同步工具`、`99-Changelog`，或把它们的内容纳入脚本。
5. **启用事件触发**：
   ```bash
   systemctl --user enable --now obsidian-sync.path
   ```
6. **（可选）obsidian-git**：在 vault 内 `git init`，确认无 remote。
7. **验证**：运行 `bash scripts/sync-to-obsidian.sh --dry-run`，打开 vault 确认 MOC 渲染、归档目录生成。

---

## 9. 回滚

- 脚本增强均为**增量追加**，回滚只需 `git revert` 脚本与 systemd 单元；vault 内 MOC/archive 为生成物，删除即可。
- 插件/设置：移除 `community-plugins.json` 中对应项并卸载插件。
- systemd： `systemctl --user disable --now obsidian-sync.path`，保留 60s `--watch` 作为兜底。

---

## 附：本次已生成 / 修改的文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `scripts/sync-to-obsidian.sh` | 修改 | 新增 `generate_moc()`、`archive_report()`；frontmatter 增加 `doc_type`/`related_strategy` |
| `systemd/obsidian-sync.path` | 新增 | 监听源文档变更触发同步（替代轮询） |
| `systemd/obsidian-sync.service` | 新增 | 调用同步脚本（oneshot） |
| `etc/obsidian/README.md` | 新增 | 应用指南 |
| `etc/obsidian/community-plugins.json` | 新增 | 插件启用清单 |
| `etc/obsidian/{dataview,templater,periodic-notes,obsidian-git}.json` | 新增 | 各插件设置（含禁 push） |
| `etc/obsidian/templates/Synced-Doc.md` | 新增 | 手动笔记 frontmatter 模板 |
| `etc/obsidian/templates/Daily-Note.md` | 新增 | 每日研究日志模板（方向③） |
