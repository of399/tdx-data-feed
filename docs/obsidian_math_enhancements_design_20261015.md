# Obsidian 数学笔记库 · 增强项设计说明 (2026-10-15)

> 范围：在已完成「flashcards 自动化 + 模板防扫空卡 + MOC 排除修正 + 质检查询 + 废卡清理」的基础上，补三项是增强项：
> 1. 安装同步纪律（源 ↔ 部署 防漂移）
> 2. `tdx_link` 字段（概念卡 ↔ 仓库实现 回指）
> 3. `T-书索引` 取舍（书索引是否进 Spaced Repetition 卡组）
>
> 单一事实源：`etc/obsidian/math/{templates,MOC}`；部署副本：`StockVault/06-模板/math/*` 与 `StockVault/05-知识库/数学/00_索引/总览.md`（及可选 `math-study`）。
> 标准动作：改源 → `install` 部署 → `verify` 校对。

---

## 一、安装同步纪律

### 1.1 目标与预期效果
- **目标**：杜绝「改了源模板却忘了部署」或「在 vault 里直接改了模板」导致的源/副本静默漂移；提供可重复的「改源 → 部署 → 校对」闭环。
- **预期效果**：任何不一致都能被校验脚本在秒级检出（`exit 1`）；该脚本可挂进 `make` 与日常检查；新人改动模板有纪律可循。

### 1.2 实施步骤
1. **新增 `scripts/verify-math-templates.sh`**（只读校验，不改盘）：
   - 参数与 `install-math-templates.sh` 一致：`--vault / --prefix / --tpl-dir`，默认指向 `math-study` + `templates/math`。
   - 对 `etc/obsidian/math/templates/T-*.md` 逐个 `cmp` 比对 `<vault>/<tpl-dir>/T-*.md`；
   - 对 `etc/obsidian/math/MOC/总览.md` 比对 `<vault>/<prefix>/00_索引/总览.md`；
   - 输出三类：`一致 / 缺失(部署侧没有) / 漂移(内容不同)`，全一致则 `exit 0`，否则 `exit 1`。
2. **`Makefile` 增加 `verify-math` target**：调用上述脚本，变量可覆盖（`MATH_VAULT` / `MATH_PREFIX` / `MATH_TPL_DIR`，默认 StockVault）。
3. **`etc/obsidian/README.md` 新增 §「安装同步纪律」**：声明单一事实源 + 标准动作序列 + 禁忌（勿直接改 vault 内 `06-模板/math`；MOC 只改源）。

### 1.3 关键约束
- 部署副本本身是含 `<% %>` 的**模板原文**（Templater 仅在「新建文件」时展开，不展开模板文件本身），故 `cmp` 字节级比对有效；源与部署必须逐字节相同（`install` 已用 `cmp` 跳过一致的）。
- `verify` 的路径约定必须与 `install` 完全一致，否则出现假阳性漂移 —— 通过复用同一组默认参数 + 相同 flag 实现。
- `StockVault` 不会被 `sync-to-obsidian.sh` 覆盖（该脚本目标为 `~/ObsidianVaults/tdx-data-feed`），故其 MOC/模板只由本 `install`/`verify` 管理，无第三方覆盖风险。注意 `05-知识库/00-MOC.md` 是 moc_agent 自动生成，与我们的 `00_索引/总览.md` 无关，`verify` 只比对后者。

### 1.4 数据结构 / 接口 / 流程变更
- 新增脚本 `scripts/verify-math-templates.sh`（纯 bash + `cmp`，不引入新依赖）。
- `Makefile` 新增 target；`README` 文档变更。**无 schema / 字段变更**。
- 流程职责分离：`install` 负责写盘，`verify` 只读校验，二者参数对称。

### 1.5 验证与验收
- 部署后运行 `verify`：输出「全部一致」，`exit 0`。
- 故意在 `StockVault/06-模板/math/T-定义.md` 加一个空格 → `verify` 报「漂移」并 `exit 1`；还原后恢复 `0`。
- `make verify-math` 可运行且结果与手动一致；`bash -n` 语法检查通过。

### 1.6 风险与应对
- **假漂移（路径不匹配）**：`verify` 复用 `install` 同一组默认参数与 flag；文档给出标准命令对；若用不同 `--prefix` 部署，须同步传给 `verify`。
- **用户在 Obsidian UI 直接编辑 `06-模板/math` 模板**：漂移被 `verify` 捕获；README 明确「改源不改副本」。
- **权限**：`verify` 只读，不改盘，安全。

---

## 二、`tdx_link` 字段

### 2.1 目标与预期效果
- **目标**：每张数学卡记录指向仓库内真实实现/脚本的回指链接，打通「数学概念 ↔ 项目代码」，呼应 `docs/math_upgrade_plan_20261015.md` 的 ROI 对齐（概率统计/优化/数值分析优先）。
- **预期效果**：从概念卡一键跳到对应实现（如 Deflated Sharpe Ratio 卡 → `scripts/plan_e_backtest.py:deflated_sharpe_ratio`）；MOC 可统计「已回指/未回指」覆盖率，驱动补齐。

### 2.2 实施步骤
1. **7 个模板 frontmatter 增加 `tdx_link:`**（空标量，紧跟 `depends:` 之后）。
2. **模板正文新增 `## 项目实现回指` 小节**（在 `---` 之后、字段约定之前），一句话说明填法：`"[[scripts/plan_e_backtest.py#deflated_sharpe_ratio]]"` 或仓库相对路径，可空。
3. **（建议）MOC 新增「八、质检：缺 `tdx_link` 的笔记」**：
   ```dataview
   LIST file.link AS 笔记, domain AS 领域
   FROM #math-notes
   WHERE !contains(file.folder, "06-模板")
     AND !contains(["书索引"], type)
     AND !tdx_link
   SORT file.folder, file.name
   ```
   排除书索引（参考目录，回指意义弱）。
4. **部署**：重跑 `install` 把模板与 MOC 推到 `StockVault`；`verify` 校对。

### 2.3 关键约束
- **YAML 标量纪律**：用**无引号独立标量** `tdx_link:`（空）；勿写 `"<% ... %>"`（Templater 引号坑，会导致不展开/整项被丢）。本项不注入 `<% %>`，但仍须保持空标量写法。
- `depends` 必须带引号 wiki 链接才被 Dataview 当链接；`tdx_link` 同理，填 wiki 链接时写 `"[[...]]"`。
- 现有 6 张种子卡**已含 `tdx_link`**，新增模板不影响已存在笔记；仅新建卡自动获得空字段。
- 空 `tdx_link` 在 YAML 解析为 `null`；Dataview `!tdx_link` 对 `null`/缺失均判真 → 质检正确。

### 2.4 数据结构 / 接口 / 流程变更
- 模板 frontmatter 新增字段 `tdx_link`（可空，默认空）。契约：`tdx_link` ∈ {空 | wiki 链接 | 路径字符串}。
- MOC 新增一段 Dataview（可选但建议）。
- 部署流程不变（`install` / `verify`）。

### 2.5 验证与验收
- 文件系统建 0 字节文件于 `14_概率统计/` 触发 Templater → 生成笔记 frontmatter 含 `tdx_link:` 且为空；`head -20` 确认无 `<% %>` 残留。
- 7 个模板 `python3 -c` PyYAML 解析全过。
- 若实现 MOC 质检：空 `tdx_link` 的卡出现在「缺 tdx_link」列表；填值后消失。
- 部署后 `verify` 全一致。

### 2.6 风险与应对
- **空值被误当「已填」**：质检用 `!tdx_link`（`null`/空均判真），不会漏。
- **用户填裸路径而非链接**：仅影响人类可读性，无结构破坏；文档建议 wiki 链接写法。
- 与 flashcards 自动标签无关，互不干扰。

---

## 三、`T-书索引` 取舍

### 3.1 目标与背景
- **目标**：明确「书索引」类笔记是否进入 Spaced Repetition 卡组，消除模板当前「默认带 `flashcards` 标签」与「书索引 = 参考枢纽而非概念卡」之间的语义冲突。
- **现状**：`T-书索引.md` 第 3 个 tag 为 `<% 'flashcards/' + ... %>`，即新建书索引**默认入 SR 卡组**。但书索引正文无 `?` 分隔的 Q/A，SR 扫不出卡（0 张），只留下一个空 deck 路径；且它本应是「本章覆盖笔记 / 掌握度」枢纽，而非待背诵卡。

### 3.2 两个选项
- **选项 A（推荐，默认排除）**：书索引不当作复习卡。模板移除 `flashcards` 自动标签，正文保留为枢纽；MOC 质检排除 `type=书索引`，避免被「缺 flashcards」误报。
- **选项 B（保留 flashcards）**：书索引仍自动入组；如需成卡，在模板补一段 Q/A（如「本章核心结论？」）。代价：书索引语义被扭曲为概念卡，且多数书索引不会填 Q/A → 空 deck 堆积。

### 3.3 推荐与理由（选项 A）
书索引是「文献/章节枢纽」，与 定义/定理/例子 等概念卡性质不同；让它默认进卡组既无实际卡片，又污染 `flashcards/` 命名空间与质检逻辑。改为「默认排除、按需手动加标签」更干净，且符合模板原有注释「不需要成卡的笔记删掉该标签即可」的精神（把默认翻转）。

### 3.4 实施步骤（按选项 A）
1. `T-书索引.md` frontmatter：删除第 3 个 tag（`<% 'flashcards/'... %>`），保留 `tags:` 两行 `math-notes` / `书索引`。
2. 同步改部署副本（`install` 推）。
3. 两处 MOC（源 + `StockVault`）的「七、质检：缺 `flashcards` 标签」查询加 `AND type != "书索引"`（Dataview 支持 `type != "书索引"` 或 `!contains(["书索引"], type)`）。
4. 更新模板正文第 16 行注释：书索引默认不含 `flashcards`，如需成卡手动加 `flashcards/<领域>`。
5. `etc/obsidian/README.md` 在 flashcards 自动化段落补一句：书索引默认排除。

### 3.5 关键约束
- `type` 字段已在合法枚举（`书索引` 是体检查询列出的合法 type 之一），排除只影响 flashcards 质检与 SR 入组，不影响 MOC 枢纽/计数/体检。
- MOC「三、枢纽概念」「四、领域计数」仍含书索引（按 folder 过滤），符合「枢纽」定位，无需改。
- 已存在的书索引笔记（若有）仍带旧 `flashcards` 标签 → 不会因模板改动自动消失；如需统一手动移除。当前 6 张种子卡无书索引，故无存量需清理。
- 与 #2 的 `tdx_link` 互不冲突（书索引仍可带 `tdx_link` 回指来源文献/代码）。

### 3.6 数据结构 / 接口 / 流程变更
- 模板 `T-书索引.md` 的 `tags` 由 3 项变 2 项（去掉动态 `flashcards` 项）。
- MOC 质检查询 `WHERE` 子句增 `type != "书索引"`。
- **无新字段、无 schema 变更**；与 #2 互不冲突。

### 3.7 验证与验收
- 在 `14_概率统计/` 用 `T-书索引` 建卡 → frontmatter `tags` 仅 `math-notes` + `书索引`，无 `flashcards/`；SR 卡组树不出现该书索引空 deck（或 deck 计数不变）。
- MOC 七段：该书索引**不**出现在「缺 flashcards」列表（因 `type=书索引` 被排除）；其它真实缺标笔记仍出现。
- 手工给某书索引加 `flashcards/概率统计` → 它立即从七段「缺标」列表消失（`type` 排除只拦默认缺标，手动加标不被拦），且 SR 把它当卡（若有 `?` 卡体）。
- `verify` 全一致。

### 3.8 风险与应对
- **用户其实希望书索引也复习** → 改回选项 B 仅需把第 3 个 tag 加回（一行），并去掉 MOC 的 `type != "书索引"`（一处）。改动控制在最小，可一键回滚。
- **存量书索引笔记标签与模板不一致** → 当前无存量（种子卡无书索引），新卡随模板；如有旧卡，手动清理一次。
- **QA 查询 `type != "书索引"` 误伤其它 type** → 仅精确排除书索引，不动其它 6 类。

---

## 四、统一部署与校对命令（三项共用）

```bash
# 部署（源 → StockVault）
bash scripts/install-math-templates.sh \
  --vault /home/jiuben/StockVault \
  --prefix "05-知识库/数学" \
  --tpl-dir "06-模板/math"

# 校对（只读，exit 0=一致 / 1=漂移）
bash scripts/verify-math-templates.sh \
  --vault /home/jiuben/StockVault \
  --prefix "05-知识库/数学" \
  --tpl-dir "06-模板/math"
```

---

## 五、涉及文件清单（预计）

| 类别 | 文件 | 变更 |
|---|---|---|
| 新增脚本 | `scripts/verify-math-templates.sh` | 新建（只读校验） |
| 构建 | `Makefile` | 增 `verify-math` target |
| 源模板 ×7 | `etc/obsidian/math/templates/T-*.md` | 加 `tdx_link:`；`T-书索引` 去 flashcards 标签 + 注释 |
| 源 MOC | `etc/obsidian/math/MOC/总览.md` | 七段加 `type != "书索引"`；新增八段（tdx_link 质检） |
| 部署副本 | `StockVault/06-模板/math/*`、`StockVault/05-知识库/数学/00_索引/总览.md` | 由 `install` 同步 |
| 文档 | `etc/obsidian/README.md` | 新增安装同步纪律 + tdx_link + 书索引排除说明 |

---

## 六、下一步建议
1. 确认 #3 采用**选项 A（书索引默认排除）**（或改为选项 B）。
2. 确认后我按第四节命令实施：改源 → `install` 部署 → 新建 `verify` 脚本并 `verify` 校对 → 用 0 字节文件触发 Templater 抽样验证新卡 frontmatter。
3. 验收口径：三项 `verify` 全一致、`tdx_link` 出现在新卡、书索引不进 SR 卡组且不被七段误报。

---
生成时间：2026-10-15
生成者：设计文档（待实施确认）
模型版本：v{N}
数据日期：2026-10-15
