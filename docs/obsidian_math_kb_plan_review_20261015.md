# Obsidian 数学笔记库方案评审（2026-10-15 12:10）

> 评审对象：用户提出的「95_模板 + 00_索引 + 10_实分析 + 90_开放问题」四模板笔记体系（定义/定理/例子/开放问题/书索引 + Dataview 总览 + Flashcards 复习卡）。
> 评审基线：实测 `/home/jiuben/ObsidianVaults/tdx-data-feed` 现状 + `docs/OBSIDIAN_KB_RENOVATION.md` + `docs/OBSIDIAN_BIDIRECTIONAL_SYNC_EVAL.md` + `docs/math_upgrade_plan_20261015.md` + Spaced Repetition 官方 README。
> **格式声明**：本文为方案评审（技术评估），适用模板子集（核心指标 / 明细 / 风险 / 下一步）；Top N 明细改为「问题清单」，统计分布改为「严重度分布」。

## 📊 核心指标

| 指标 | 数值 | 说明 |
| ---: | ---: | --- |
| 方案可用度（照抄即用） | **40%** | 结构优秀，但有 2 处致命阻塞、4 处会静默失效 |
| **P0 必改项** | **5** | 插件未装、插件名错、卡片格式不被识别、与同步脚本冲突、领域优先级错配 |
| P1 应改项 | 8 | 双向链接维护、空值字段、枚举约束、Templater 路径等 |
| P2 可选增强 | 5 | 死链检测、掌握度自动统计、备份等 |
| 现有 vault 已装插件 | **0 / 4** | `.obsidian/` 只有 JSON 配置，**无 `plugins/` 目录** |
| 现有 vault 目录体系 | `00-`…`08-` + `99-` | **连字符**；新方案用**下划线** `95_模板`，两套并存 |
| 同步脚本接管目录 | `00-知识库入口`、`08-报告` | 脚本 `cp` 覆盖写入，手写内容放进去会被吃掉 |
| 普林斯顿分析四卷对口率 | **23.53%**（对 P0 帮助 0） | 新方案 domain 全为 `实分析`，与项目 ROI 排序不一致 |

**一句话结论**：模板的**知识结构**（定义/定理/例子/反例/复习卡）是一流的，问题全在**工程落地层**——插件一个都没装、卡片格式写错、以及这套数学库塞进 `tdx-data-feed` 这个"脚本生成型 vault"会互相污染。**建议独立开 vault**，而不是改造现有库。

---

## 🎯 问题清单（按严重度降序）

### P0-1 插件根本没装（方案完全跑不起来）

实测：`.obsidian/` 下只有 `app.json / appearance.json / community-plugins.json / dataview.json / templater.json / periodic-notes.json / obsidian-git.json`，**没有 `plugins/` 目录**。

`etc/obsidian/README.md` 已写明：JSON 只控制"启用"，**插件二进制必须经 UI 首次安装**。所以 `00_索引/总览.md` 现在打开只会显示原始 ```dataview 代码块。

> 修复：Obsidian → 设置 → 第三方插件 → 关闭安全模式 → 浏览，安装 `Dataview` 与 `Spaced Repetition`（见 P0-2）。

### P0-2 插件名错误：没有叫 "Flashcards" 的官方插件

间隔重复插件真名是 **Spaced Repetition**（`st3v3nmw/obsidian-spaced-repetition`）。社区里另有一个 `Flashcards`（LLM 生成卡片的第三方工具），装错则不产出复习队列。

### P0-3 复习卡格式 `Q:` / `A:` **不被识别**（最坑：静默失效）

Spaced Repetition 官方支持的格式只有这些：

| 类型 | 语法 |
| --- | --- |
| 单行 | `Question::Answer` |
| 单行反向 | `Question:::Answer` |
| 多行 | 问题行 → `? 答案行` |
| 多行反向 | 问题行 → `?? 答案行` |
| Cloze | `==挖空==` / `**加粗**` / `{{大括号}}` |

`Q: …` `A: …` **不在列表内**，插件不报错、直接忽略 → 你以为进了队列，实际 0 张卡。

另外**必须给笔记打 `#flashcards` 标签（复数）**才会被扫成卡组，子卡组用 `#flashcards/实分析`；仅写 `## 复习卡` 标题无效（该标题只作为插件显示的上下文 `笔记名 > 复习卡`）。

修正后的复习卡块（推荐单行式，最稳）：

```markdown
## 复习卡
<!-- 本笔记需带 #flashcards/实分析 标签才会进入复习队列 -->

勒贝格积分相对黎曼积分的根本区别？:: 按值域分层、以测度加权，可积函数类更大（如狄利克雷函数）。
```

答案较长时用多行式（问题行以 `?` 结尾，答案行以 `? ` 开头）：

```markdown
## 复习卡

{{标题}} 的条件与结论？
? 条件：……；结论：……。
```

### P0-4 与现有同步体系冲突（生成物 vs 手写物）

`scripts/sync-to-obsidian.sh` 是 **单向 repo → vault**，`cp` 覆盖写 `00-知识库入口/`、`08-报告/` 及其 `archive/`；`docs/OBSIDIAN_BIDIRECTIONAL_SYNC_EVAL.md` 已把"排除列表"列为**仅人工约定（部分缺失）**。数学笔记是 authored 内容，一旦放进这些目录，或将来启用双向同步（方向⑥），就会被覆盖/回写污染。

> 建议：**新建独立 vault `~/ObsidianVaults/math-study`**。理由：① 领域与 tdx 完全无关；② 避免 `00-`/`08-` 生成物与手写笔记混排；③ 省掉双向同步的隔离与冲突裁决成本（方向⑥可继续搁置）。
> 若坚持共库：必须① 手写区固定在 `90_手写/` 之上且**不落在 `00-`~`08-`**；② 给数学卡统一 tag `#math-notes`；③ 在同步脚本里硬编码排除列表。

### P0-5 领域优先级与项目 ROI 错配

`docs/math_upgrade_plan_20261015.md` 实测结论：普林斯顿分析四卷对本仓库对口率 **23.53%**、对 P0 项帮助 **0**；工科五卷 ~**72%**，其中**第 5 卷概率统计直接命中 P0-1（统计推断校正）**。而新方案 domain 全为 `实分析`。

> 建议：domain 枚举按项目价值排序先建 `概率统计 / 线性代数 / 优化理论 / 数值分析`，`实分析 / 复分析 / 泛函分析` 作为长线；并给每张卡加可选字段 `tdx_link`（指向本仓库的脚本或概念，如 `[[scripts/factor_significance.py]]`），让学习笔记与量化工程闭环，避免变成第二个孤岛。

---

### P1 清单（结构与查询层）

| # | 问题 | 现状 | 建议 |
| ---: | --- | --- | --- |
| 6 | **`used_by` 手工双向维护必漂移** | 定义卡写 `used_by: [勒贝格积分]`，被依赖方改名/新增就失同步 | 只保留 `depends`；`used_by` 改为 Dataview 反查：`LIST FROM #math-notes WHERE contains(depends, this.file.link)` |
| 7 | **`depends` 是纯文本不是链接** | `depends: [σ代数]` | 写成**带引号的 wiki 链接** `depends: ["[[σ代数]]"]`，否则 Dataview 当字符串处理，图谱/入链全断 |
| 8 | **`review:` 空值 = YAML null** | `review: ` 解析为 null | `WHERE review <= date(today)` 会静默漏掉 null 项。要么删字段，要么占位 `review: 2099-12-31` |
| 9 | **两套复习调度打架** | 手写 `review` 日期 + 插件 FSRS/SM-2 | 二选一：交给插件就删 `review`；要手写就用 Dataview 到期查询，别并存 |
| 10 | **type/status/level 无枚举约束** | 自由文本 | 固定枚举 `type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`、`status ∈ {待证明,学习中,已掌握,开放问题}`；加一条体检查询列出越界值 |
| 11 | **缺 `反例` / `证明` 模板** | 定义卡链 `[[XXX的反例]]`、定理卡链 `[[XXX的证明]]`，但无对应模板 | 补两个模板（或把反例并入"例子"卡的"边界例/反例"段，二选一，别双写） |
| 12 | **目录驱动 → 应改标签驱动** | `FROM "10_实分析" OR "11_复分析"` | 改为 `FROM #math-notes`（与现有 MOC 的 `#tdx-data-feed` 一致）：新增领域不用改查询；并把 `domain` 作为分组字段而非路径依据 |
| 13 | **Templater 找不到 `95_模板/`** | `templates_folder: "templates"`、`enable_folder_templates: true` | 模板放 `templates/math/T-定义.md`（沿用现有 `templates/`），或改 templater.json 的 `templates_folder`；另 `trigger_on_file_creation: true` 会对所有新笔记套 `Synced-Doc`，建议改用 `folder_templates` 按目录指定（当前为空） |

### P2 清单（可选增强）

| # | 建议 |
| ---: | --- |
| 14 | **死链检测**：`LIST depends FROM #math-notes` 人工核对，或开 `dataview.json` 的 `enableJs: true` 用 dataviewjs 自动比对未建卡依赖 |
| 15 | **书索引掌握度改自动**："已掌握 / 待证明 / 开放问题" 手填 → `TABLE status FROM #math-notes WHERE 书索引相关 GROUP BY status` |
| 16 | **备份**：手写笔记是资产，vault 不在 git。建议 vault 内 `git init`（`obsidian-git.json` 已设 `disablePush:true`、无 remote）或独立私有仓库 |
| 17 | **文件名规范**：`[[σ代数]]` 可用，但禁止 `/ \ : * ? " < > |`；建议加"文件名 = 标题"约定 |
| 18 | **模板纳入版本控制**：放 `etc/obsidian/templates/math/`，由同步脚本（或新增 `--install-templates`）复制到 vault，与现有 `Synced-Doc.md` / `Daily-Note.md` 同约定 |

---

## 📈 严重度分布

| 严重度 | 数量 | 后果 |
| ---: | ---: | --- |
| P0 阻塞（不修则方案不可用） | 5 | 索引不渲染 / 复习队列为空 / 手写笔记被覆盖 / 投入错领域 |
| P1 结构（能用但会漂移） | 8 | 链接图断裂、枚举漂移、排序漏项 |
| P2 增强 | 5 | 效率与可维护性 |

**修正后可直接使用的索引查询**（替换原 `00_索引/总览.md` 四段）：

```dataview
TABLE domain AS 领域, level AS 层级, status AS 状态, review AS 下次复习
FROM #math-notes
WHERE status = "待证明" OR status = "学习中"
SORT file.mtime DESC
```

```dataview
LIST
FROM #math-notes AND #开放问题
```

```dataview
TABLE WITHOUT ID file.link AS 概念, length(file.inlinks) AS 被引次数, domain AS 领域
FROM #math-notes
SORT length(file.inlinks) DESC
```

> 注：`file.inlinks` 统计的是**所有**入链（含 MOC 与正文引用），不等于"被 depends 依赖数"。若要精确统计 `depends` 维度，需在定义卡内放一段内联反查（见 P1-6）或开 `enableJs: true` 用 dataviewjs 解析 `contains(depends, ...)`。

```dataview
TABLE length(rows) AS 笔记数
FROM #math-notes
WHERE domain
GROUP BY domain
```

---

## ⚠️ 风险提示

1. **插件未安装前所有 Dataview/SR 功能均为空转**，且 Spaced Repetition **不报错**，容易误以为"已进队列"。
2. **vault 是生成副本**（单一事实源仍为仓库 + `sync-to-obsidian.sh`）；手写数学笔记与之混库，一旦启用方向⑥双向同步，存在被覆盖/回写污染的风险，排除列表目前仅人工约定。
3. **`cp` 非原子写**（评估文档已列为"部分缺失"）：若共库且脚本正在写 `08-报告/`，同时 Obsidian 打开该目录可能读到半截文件。
4. **学习投入与项目收益弱相关**：普四卷对本仓库 P0 项帮助为 0，纯实分析路线短期不产生量化收益，需明确"这是个人长期投入"还是"服务于项目"。
5. **无备份**：vault 未 `git init`，手写笔记丢失不可恢复。

---

## 🔧 下一步建议（按执行顺序）

1. ~~**开独立 vault** `~/ObsidianVaults/math-study`~~ ✅ 已执行（2026-10-15 12:42，脚本自动创建）
2. ~~**装插件**~~ ✅ 已用 `scripts/install-obsidian-plugins.sh` 从 GitHub Releases 下载插件本体
   （dataview / obsidian-spaced-repetition / templater-obsidian；项目 vault 另补三项）。
   **仍需人工**：Obsidian → 设置 → 第三方插件 → 关闭安全模式 → 启用。
3. ~~**改模板**~~ ✅ 已执行：卡片改 `问题? / ? 答案`（另在注释中给出 `Q::A`）；`tags` 含 `flashcards/<领域>`；
   `depends` 改为 `["[[链接]]"]`；`used_by` 删除改为卡内 Dataview 反查；`review` → `next_review`（默认 `2099-12-31`，不留空）。
4. ~~**补枚举与模板**~~ ✅ 已执行：`T-定义 / T-定理 / T-例子 / T-反例 / T-证明 / T-开放问题 / T-书索引` 共 7 份，
   落在 `etc/obsidian/math/templates/`（纳入版本控制）。
5. ~~**对齐项目 ROI**~~ ✅ 已执行：骨架目录按 ROI 排序新增 `14_概率统计 / 15_优化理论 / 16_数值分析`，
   开放问题模板加 `## 与我项目的关系（可选）`（`tdx_link`）。
6. ~~**备份**~~ ✅ 已 `git init`（无 remote），`.gitignore` 排除插件二进制与 `*.bak-*`，已 3 次提交。
7. ~~**建第一批卡**~~ ✅ 已建 6 张种子卡（见下表），均带 `tdx_link` 回指仓库实现。
8. **用模板建后续卡时**：记得在 `tags` 补 `flashcards/<领域>`（模板故意不带，否则模板会被做成空卡）。

### 落地状态（2026-10-15 12:42）

| 项 | 状态 | 位置 |
|---|---|---|
| 7 份修正版模板 | ✅ | `etc/obsidian/math/templates/T-*.md`（2026-10-15 13:18 去掉 `flashcards` 标签） |
| 修正版总览 MOC（6 段查询，含体检/孤儿） | ✅ | `etc/obsidian/math/MOC/总览.md`（各段均排除 `templates/`） |
| 字段枚举与卡片语法约定 | ✅ | `etc/obsidian/math/README.md` |
| 模板安装脚本 | ✅ | `scripts/install-math-templates.sh`（幂等、覆盖前备份） |
| **插件安装脚本** | ✅ 新增 | `scripts/install-obsidian-plugins.sh`（断点续传 + 指数退避 + size 校验） |
| 新 vault 初始化 | ✅ | `~/ObsidianVaults/math-study`（目录骨架 + 模板 + MOC + `.obsidian` 配置） |
| **插件本体安装** | ✅ | math-study：dataview 0.5.70 / obsidian-spaced-repetition 1.15.4 / templater-obsidian 2.26.0；项目 vault 另补 dataview / templater-obsidian / periodic-notes |
| **6 张种子卡** | ✅ | `14_概率统计/`×4、`15_优化理论/`×1、`16_数值分析/`×1，均带 `tdx_link` |
| **vault git 备份** | ✅ | `~/ObsidianVaults/math-study`（无 remote，3 次提交） |
| 仅需人工 | ⬜ | Obsidian → 第三方插件 → **关闭安全模式**并启用插件；用模板建卡后手补 `flashcards/<领域>` |

```bash
# 复现 / 装到其他 vault
bash scripts/install-math-templates.sh --init-config                       # 默认 math-study
bash scripts/install-math-templates.sh --vault /path/to/vault --dry-run    # 先看计划
```

---
生成时间：2026-10-15 12:10:00
生成者：tdx-data-feed 助手（Obsidian 数学库方案评审）
数据日期：2026-10-15（vault 与插件状态实测于 2026-10-15 12:05）
