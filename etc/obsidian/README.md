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
