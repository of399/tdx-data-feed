# Obsidian 配置应用指南（tdx-data-feed）

本目录是 `ObsidianVaults/tdx-data-feed` 的**配置与模板源**，便于版本化管理 KB 设置。
实际 vault 在 `/home/jiuben/ObsidianVaults/tdx-data-feed`，不在本仓库内。

## 应用步骤

1. **安装插件**（Obsidian → 设置 → 社区插件 → 浏览）：
   - Dataview（必做）
   - Templater（方向②）
   - Periodic Notes（方向③）
   - obsidian-git（方向⑤，可选）
   或把本目录的 `community-plugins.json` 复制到 `vault/.obsidian/` 后重启 Obsidian
   （插件二进制仍须经 UI 首次安装，JSON 仅控制"启用"）。

2. **套用设置**：把以下文件复制到 `vault/.obsidian/`（同名覆盖/合并）：
   - `dataview.json`
   - `templater.json`
   - `periodic-notes.json`
   - `obsidian-git.json`（可选）

3. **放模板**：把 `templates/` 整个目录复制到 `vault/templates/`。

4. **（可选）obsidian-git**：在 vault 内 `git init`，**不要**添加 remote；
   配置已设 `autoPush:false` + `disablePush:true`，仅本地版本化。

5. **启用事件触发同步**（在仓库根目录执行）：
   ```bash
   systemctl --user enable --now obsidian-sync.path
   ```

## 注意
- vault 是 `scripts/sync-to-obsidian.sh` 的**生成副本**，单一事实源仍是该脚本与源码仓库。
- 不要将 vault 作为源码仓库推送；obsidian-git 仅用于本地变更历史。
- `06-运维与监控`、`07-同步工具`、`99-Changelog` 等目录若未被脚本填充，建议清理或纳入脚本。
- **插件本体必须经 Obsidian UI 安装一次**：`.obsidian/*.json` 只控制"启用"，复制配置文件不等于安装插件
  （实测 2026-10-15：项目 vault 的 `.obsidian/` 无 `plugins/` 目录，四个插件均未安装）。

## 另见：数学笔记库（`math/`，独立 vault）

`etc/obsidian/math/` 是**手写数学知识库**的模板源，默认装到独立 vault
`~/ObsidianVaults/math-study`（与项目库分离，避免生成物/手写物互相污染）：

```bash
bash scripts/install-math-templates.sh --init-config   # 建目录骨架 + 7 份 T-*.md + 总览 MOC + 插件配置
bash scripts/install-math-templates.sh --vault <path>  # 指定 vault（指向项目库时脚本会打印隔离警告）
```

- 字段枚举、卡片语法（`::` 而非 `Q:`/`A:`）、`depends` 链接写法见 `etc/obsidian/math/README.md`。
- 间隔重复用 **Spaced Repetition** 插件（`spaced-repetition`），不是 Flashcards。
- 评审依据：`docs/obsidian_math_kb_plan_review_20261015.md`。

### 数学笔记库 · 安装同步纪律

- **单一事实源**：`etc/obsidian/math/{templates,MOC}` 是源；`StockVault/06-模板/math/*` 与 `StockVault/05-知识库/数学/00_索引/总览.md`（及可选 `math-study`）是生成副本。
- **标准动作**：改源 → 部署 → 校对。
  ```bash
  bash scripts/install-math-templates.sh --vault /home/jiuben/StockVault --prefix "05-知识库/数学" --tpl-dir "06-模板/math"
  bash scripts/verify-math-templates.sh  --vault /home/jiuben/StockVault --prefix "05-知识库/数学" --tpl-dir "06-模板/math"
  # 或 make verify-math
  ```
- **禁忌**：勿直接改 vault 内 `06-模板/math` 的模板文件（改源不改副本）；MOC 只改 `etc/obsidian/math/MOC/总览.md`。
- **`tdx_link` 字段**：每张数学卡回指仓库内实现，如 `scripts/plan_e_backtest.py#deflated_sharpe_ratio`；可空，MOC「八、质检」列出未回指的概念卡。
- **`书索引` 取舍**：书索引是文献枢纽，**默认不含** `flashcards/<领域>`（不进 Spaced Repetition 卡组）；MOC「七、质检」已排除 `type=书索引`。如需把某本书当复习卡，手动加 `flashcards/<领域>` 标签。
