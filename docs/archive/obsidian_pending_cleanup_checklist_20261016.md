# Obsidian 知识库待办清理操作清单（2026-10-16）

> 本清单由开发助手整理，供在 Obsidian 知识库（StockVault）中执行。
> **执行纪律（必读）**：① 每项任务的「变更清单」须先经用户确认，再执行；② 不删除用户数据——删除/合并前必须 `diff` 确认冗余；③ 改动统一走 `python3` 脚本（避审批超时），幂等、可回退；④ 每项完成后跑两个校验器复验。

## 📊 核心指标

| 维度 | 数值 |
|---|---|
| 待办任务总数 | 5 |
| 优先级层级 | P0 × 2 / P1 × 2 / P2 × 1 |
| 直接涉及文件 | ~14（标的 4 · 模板 6 · 收件箱 1 · 项目库 sync 1 + 引用方若干）|
| 当前校验状态 | 元数据 META_EXIT=1（贵州茅台旧 stub 报 filename）· 链接硬断链 0（18 工作空间伪断链）|

## 🎯 优先级总览

| 优先级 | 任务 | 类型 | 影响文件 |
|---|---|---|---|
| **P0-1** | 贵州茅台重命名收尾（删旧 stub + 修 3 链接）| 命名一致性 | 3 |
| **P0-2** | 000002 合并去重（万科A/Ａ）| 数据去重 | 3 |
| **P1-1** | 模板示例链接非 wikilink 化 | 校验信号净化 | 6 |
| **P1-2** | 收件箱草稿归类（2 链接）| 草稿治理 | 2+ |
| **P2** | 接入 sync 钩子 | 防回归 | 1（脚本）|

执行顺序建议：**P0-1 → P0-2 → P1-1 → P1-2 → P2**（P0 为数据完整性基础，P1 净化校验信号，P2 为防护层）。

---

## P0-1 贵州茅台重命名收尾

**现状**：`600519-贵州茅台.md`（9.05KB，内容主体）已存在，但旧 `贵州茅台.md`（863B stub）残留。`[[贵州茅台]]` 仍解析到旧 stub，校验器因此仍报 filename 错误。
**涉及文件**：
- `/home/jiuben/StockVault/01-标的/600519-贵州茅台.md`（保留，内容主体）
- `/home/jiuben/StockVault/01-标的/贵州茅台.md`（待删 stub，863B）
- 引用方 3 处：`README.md`、`01-标的/600519-贵州茅台.md`（自链）、`01-标的/00-MOC.md`
**影响范围**：标的模块命名一致性；3 处 wikilink 可解析性。
**变更清单（待确认）**：
1. `diff 贵州茅台.md 600519-贵州茅台.md`，确认 863B ⊂ 9.05KB（冗余）
2. 删除旧 `贵州茅台.md` stub
3. 将 3 处 `[[贵州茅台]]` → `[[600519-贵州茅台]]`（其中 `600519-贵州茅台.md` 内的自链改为去除或指向自身 basename）
**验收标准**：仅存 `600519-贵州茅台.md`；`[[600519-贵州茅台]]` 可解析；元数据校验 filename 错误归零。

## P0-2 000002 合并去重

**现状**：`000002-万科A.md`（376B，半角名，stub）与 `000002-万科Ａ.md`（8.77KB，全角名，内容主体）并存。仅 `01-标的/00-MOC.md` 引用 `[[000002-万科A]]`（半角）。
**涉及文件**：
- `/home/jiuben/StockVault/01-标的/000002-万科A.md`（规范名，半角）
- `/home/jiuben/StockVault/01-标的/000002-万科Ａ.md`（待合并后删，全角）
- `/home/jiuben/StockVault/01-标的/00-MOC.md`（引用方）
**影响范围**：标的重复；1 处 wikilink。
**变更清单（待确认）**：
1. `diff` 两文件，确认 8.77KB ⊇ 376B
2. 将 8.77KB 内容写入 `000002-万科A.md`（半角规范名）
3. 删除全角 `000002-万科Ａ.md`
4. 链接 `[[000002-万科A]]` 保持不变（已指向规范名）
**验收标准**：仅存 `000002-万科A.md`；无重复标的；元数据校验 `duplicate_stock_codes` 归零；`[[000002-万科A]]` 可解析。

## P1-1 模板示例链接非 wikilink 化

**现状**：`06-模板/math/` 下 6 个模板含示例 wikilink（`定义A`/`XXX的反例`/`猜想A`/`所证明的定理`/`相关定义` 等占位符），产生 18 个「工作空间断链」（模板示例，非真实笔记）。
**涉及文件**（6 个）：
- `/home/jiuben/StockVault/06-模板/math/T-证明.md`
- `/home/jiuben/StockVault/06-模板/math/T-开放问题.md`
- `/home/jiuben/StockVault/06-模板/math/T-定义.md`
- `/home/jiuben/StockVault/06-模板/math/T-反例.md`
- `/home/jiuben/StockVault/06-模板/math/T-例子.md`
- `/home/jiuben/StockVault/06-模板/math/T-定理.md`
**影响范围**：链接校验信号纯度（消除 18 伪断链）。
**变更清单（待确认）**：逐文件将占位符示例链接 `[[xxx]]` 改为**反引号行内代码** `` `xxx` ``（或 `<span>xxx</span>`），使其不被解析为链接。建议先 `grep -rn '\[\[' 06-模板/math/` 导出完整待改行清单再批处理。
**验收标准**：`verify_obsidian_links.py` 工作空间断链由 18 → 0；模板在 Obsidian 中渲染正常、示例语法仍可读。

## P1-2 收件箱草稿归类

**现状**：`00-收件箱/v4-数据库视图说明.md` 引用两处：
- `[[v4-技术面分析.base]]` —— **实际存在**（1.9KB，合法的 Obsidian Bases 视图文件）；校验器不识别 `.base` 后缀才误报，链接本身有效。
- `[[vault_evolution_report]]` —— **不存在**（真断链），指向一份未创建的演化报告。
**涉及文件**：
- `/home/jiuben/StockVault/00-收件箱/v4-数据库视图说明.md`（引用方）
- `/home/jiuben/StockVault/v4-技术面分析.base`（已存在，1.9KB）
- `vault_evolution_report`（待用户决策：建笔记 or 删链接）
**影响范围**：2 条草稿链接的去向与归类。
**变更清单（待确认）**：
1. `v4-技术面分析.base`：确认其为 Bases 视图，链接有效；归类到合适目录（或保留原位）；可选：扩展校验器 `resolve_target` 识别 `.base` 以消除误报。
2. `vault_evolution_report`：用户确认 → ① 创建该报告笔记并归类到报告目录，或 ② 删除草稿中的无效链接。
**验收标准**：收件箱草稿链接全部有效或已清理；无真断链（`hard_broken` 维持 0，工作空间断链仅剩用户明确保留项）。

## P2 接入 sync 钩子（防回归）

**现状**：项目库 `/home/jiuben/ObsidianVaults/tdx-data-feed` 由 `scripts/sync-to-obsidian.sh`（9.56KB）单向从 repo 生成副本。`related_stockvault` 字段与 `00-跨库索引.md` 会被下次 sync 覆盖。
**涉及文件**：`/home/jiuben/tdx-data-feed/scripts/sync-to-obsidian.sh`
**影响范围**：跨 vault 互链（`link_cross_vault.py` 产出）的持久性。
**变更清单（待确认）**：
1. 在 `sync-to-obsidian.sh` 末尾追加 **post-sync 步骤**：运行 `python3 scripts/link_cross_vault.py`（幂等重建互链），确保 sync 后互链自动恢复。
2. （备选）sync 时排除 `00-跨库索引.md` 与含 `related_stockvault` 字段的笔记，避免覆盖。
**验收标准**：执行一次完整 `sync-to-obsidian.sh` 后，项目库 `related_stockvault` 字段与 `00-跨库索引.md` 仍存在且正确。

---

## ⚠️ 注意事项

1. **所有删除/合并均为破坏性操作**，必须 `diff` 确认冗余并获得用户确认后方可执行——尤其 P0-1、P0-2 涉及用户笔记。
2. P0-1 中 `[[贵州茅台]]` → `[[600519-贵州茅台]]` 需同步全部 3 处引用，遗漏会导致新断链。
3. P1-1 批处理模板时，需区分「占位符示例链接」与「真实 `depends` 链接」，勿误伤真实依赖声明。
4. P1-2 的 `vault_evolution_report` 真断链需先问用户意图，勿擅自建空笔记或删链接。
5. P2 改动 sync 脚本属基础设施变更，建议先在测试分支验证再合入主流程。

## 🔧 复验命令（每项完成后执行）

```bash
python3 scripts/verify_obsidian_metadata.py --vault /home/jiuben/StockVault
python3 scripts/verify_obsidian_links.py --vault /home/jiuben/StockVault
```

---
生成时间：2026-10-16 18:00:00
生成者：开发助手（操作清单整理，未执行任何改动）
数据日期：2026-10-16
