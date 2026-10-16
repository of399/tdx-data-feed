# 数学笔记库模板（math vault）

独立于 `tdx-data-feed` 项目库的手写知识库模板源。**单一事实源是本目录**，由
`scripts/install-math-templates.sh` 复制到 vault；vault 内改动请回抄到此，否则会被下次安装覆盖。

## 1. 落地步骤

```bash
# 推荐：独立新 vault（默认路径 ~/ObsidianVaults/math-study，不存在会自动创建）
bash scripts/install-math-templates.sh

# 或指定 vault（含共库模式，脚本会打印隔离警告）
bash scripts/install-math-templates.sh --vault /home/jiuben/ObsidianVaults/tdx-data-feed

# 只打印计划不写盘
bash scripts/install-math-templates.sh --dry-run

# 可选：把目录→模板映射写进 Templater 插件真正的 data.json（会先备份）
bash scripts/install-math-templates.sh --patch-templater
```

安装后在 Obsidian 里 **Open folder as vault** 选中该目录，再装插件。

### 1.1 装进已有编号体系的主库（StockVault 模式）

主库自带 `00-收件箱 … 07-日记`、`05-知识库/`、`06-模板/` 时，用 `--prefix` / `--tpl-dir` 对齐其编号体系，
避免根目录长出第二套编号：

```bash
bash scripts/install-math-templates.sh \
  --vault /home/jiuben/StockVault \
  --prefix "05-知识库/数学" \
  --tpl-dir "06-模板/math" \
  --init-config --patch-templater --copy-plugin-bins
```

- `--prefix`：数学笔记的根前缀（领域目录、MOC 都挂在其下；**模板目录不受前缀影响**）
- `--tpl-dir`：模板目录，同时决定 `templater.json` 的 `templates_folder`（取上一级目录）
- `--copy-plugin-bins`：把 math-study 里已下载好的插件本体复制到目标 vault，省去 UI 安装
- `--init-config`：合并 `community-plugins.json`（按 manifest id 去重，不覆盖已装插件）

> 实测：`sync-to-obsidian.sh` 的目标是 `~/ObsidianVaults/tdx-data-feed`，**不是** StockVault，
> 所以装进 StockVault 不会被同步脚本 `cp` 覆盖。

### 1.2 把 math-study 的种子/笔记搬进主库

`~/ObsidianVaults/math-study` 是 **git 仓库**（种子卡与模板的版本源），同步时**必须排除 `.git`**，
否则会在主库里留下一个嵌套仓库（已踩过一次：`.git` + `.gitignore` 被 rsync 搬进 `05-知识库/数学/`）：

```bash
rsync -a --ignore-existing \
  --exclude '.obsidian' --exclude 'templates' --exclude '.git' --exclude '.gitignore' \
  ~/ObsidianVaults/math-study/ ~/StockVault/05-知识库/数学/
```

## 1.3 Spaced Repetition 的实测默认值（v1.15.4，别凭印象猜）

安装后第一时间核对了插件自己的 `data.json`：

| 设置项 | 实测值 | 含义 |
|---|---|---|
| `flashcardTags` | `['#flashcards']` | 必须打该标签才成卡组 ✅ |
| `convertBoldTextToClozes` | `false` | **`**粗体**` 不会**被当挖空（放心用粗体排版） |
| `convertHighlightsToClozes` | `true` | **`==高亮==` 会被当挖空**，正文慎用 |
| `convertCurlyBracketsToClozes` | `false` | `{{}}` 不会 |
| `clozePatterns` | `['==[123;;]answer[;;hint]==']` | 仅高亮一族 |

推论：笔记正文里凡出现 `==高亮==`，都会被切出一张填空卡。而**注释里写出的字面分隔符**
（如在 HTML 注释里举例写明双冒号）也会被解析成一张垃圾卡——已从 `T-定义.md` 移除。

## 1.4 Templater 的设置到底写在哪（v2 与 v1 完全不同）

Templater 2.x 走 Obsidian 标准的 `loadData/saveData`，设置文件是
**`.obsidian/plugins/<插件目录名>/data.json`**（顶层扁平，`data_version: 2`）。
`.obsidian/templater.json` 是 v1 时代的旧位置（`{"version","settings"}` 包装），v2 **完全不读** ——
写进去等于没配，这是本库实测踩过的坑。

| 字段 | 取值 | 说明 |
|---|---|---|
| `templates_folder` | `06-模板` | 模板库根，决定「Insert Template」的候选范围 |
| `folder_templates` | `[{folder, template}]` | `folder` 是 vault 相对路径；`template` 先按 vault 相对路径找，找不到才拼 `templates_folder`，所以两种写法都行 |
| `trigger_on_file_creation_mode` | `none` / `folder` / `regex` | 只有 `folder` 才会套用目录模板 |

两个**脚本改不了**的点：

1. **总开关 `trigger_on_file_creation` 存在 localStorage**（key `templater-local-settings`），不在 vault 文件里
   → 必须手动去 设置 → Templater 打开「Trigger Templater on new file creation」。
2. **插件目录名必须等于 manifest id**。本库实测目录名是 `templater`、manifest id 是 `templater-obsidian`，
   不一致会让 Obsidian 拒绝加载（表现：开关灰掉、重启也不亮）。修复 = 目录改名为 id 同名 + 同步
   `community-plugins.json`。

顺序很关键：**先写配置文件 → 重启 Obsidian → 再在 UI 打开总开关**。
反过来的话，插件保存设置时会用内存里的旧值把 `folder_templates` 覆盖回空。

## 2. 必装插件

| 插件 | 用途 | 备注 |
|---|---|---|
| **Dataview** | 总览与卡内反查 | 无 `plugins/` 目录时 JSON 配置无效，必须经 UI 安装一次 |
| **Spaced Repetition** | 间隔重复（FSRS/SM-2） | 真名就叫这个，**没有官方 "Flashcards" 插件** |
| Templater（可选） | `{{标题}}` 自动填文件名 | 不装则 `<% ... %>` 原样显示，手工替换即可 |

## 3. 卡片语法（最容易踩的坑）

Spaced Repetition **只认**以下格式，`Q:` / `A:` **不识别且不报错**（静默 0 张卡）：

| 类型 | 写法 |
|---|---|
| 单行 | `问题::答案` |
| 单行反向 | `问题:::答案` |
| 多行 | 问题行 → **`?` 独占一行** → 答案行 |
| 多行反向 | 问题行 → **`??` 独占一行** → 答案行 |
| Cloze | 仅 `==挖空==`（实测 `**粗体**` 与 `{{}}` 默认关闭） |

另外：笔记必须带 **`#flashcards`** 标签（复数）才会被扫成卡组，子卡组写 `flashcards/实分析`；
只写 `## 复习卡` 标题无效（它仅作为插件显示的上下文）。

> **踩坑记录（2026-10-15）**：把答案写在问号同一行（`? 答案`）会**静默 0 张卡**。
> 插件源码 `MultiLineBasic` 分支的条件是 `currentTrimmed === multilineCardSeparator`，
> 即 `?` 那行 **trim 后必须严格等于 `?`**；同理 `??`。这一条最初写错，导致 6 张种子卡全部未成卡。

### 3.1 `flashcards` 标签如何自动生成（模板为何不会被扫成空卡）

模板**已经**自带 `flashcards/<领域>` 标签，但采用"藏前缀"写法，使模板文件本身不被 Spaced Repetition 当作卡组：

- `tags` 用 block 序列，第三项是无引号独立标量
  `<% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>`；
- 模板文件里这一项的字面值是 `<% 'flashcards/' + … %>`（**不以 `flashcards/` 开头**）；Spaced Repetition 的标签匹配是**前缀匹配**（`subTag.startsWith(tag + "/")`），所以模板**不会被扫成空卡**；
- 用模板建卡时 Templater 把它展开成 `flashcards/概率统计` 等（**以 `flashcards/` 开头**），**自动入组**，无需手工补；不需要成卡的笔记删掉该标签即可。
- 总览的所有查询都用 `WHERE !contains(file.folder, "06-模板")` 排除模板目录，避免模板被统计成笔记。

## 4. 字段约定（枚举）

```yaml
type:   定义 | 定理 | 例子 | 反例 | 证明 | 开放问题 | 书索引
status: 待整理 | 待证明 | 学习中 | 已掌握 | 开放问题
domain: 实分析 | 复分析 | 傅里叶分析 | 泛函分析 | 概率统计 | 优化理论 | 数值分析 | 跨领域
level:  本科 | 研究生 | 研究前沿
```

- **`depends` 必须写成带引号的 wiki 链接**：`depends: ["[[测度]]"]`。
  写 `depends: [测度]` 会被 Dataview 当普通字符串 → 图谱断链、反查失效。
- **`domain` 由目录自动推导，别再写死**：
  `domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>`
  `tp.file.folder()` 默认只返回**目录名最后一段**（`absolute=false`），所以 `05-知识库/数学/14_概率统计/`
  里的新卡会得到 `概率统计`。`90_开放问题` 落到 `跨领域`（枚举里没有"开放问题"这个 domain）。
  ⚠️ 历史坑：7 个模板原本都硬编码 `domain: 实分析`，而 `folder_templates` 把 8 个领域目录都映射到同一批模板
  → 在概率统计目录建卡会静默写成 `实分析`。已统一改为推导，**不要再改回字面值**。
- **`used_by` 已废弃**：手工双向维护必然漂移。下游用卡内 Dataview 反查：
  `LIST FROM #math-notes WHERE contains(depends, this.file.link)`
- **`next_review` 与插件调度二选一**：用插件就保持 `2099-12-31` 占位。
  **切勿留空**——空值是 YAML `null`，`WHERE next_review <= date(today)` 会静默漏项。
- 文件名 = 标题，禁止 `/ \ : * ? " < > |`。

## 5. 目录骨架（安装脚本自动创建）

```
00_索引/        总览 MOC
10_实分析/ 11_复分析/ 12_傅里叶分析/ 13_泛函分析/
14_概率统计/ 15_优化理论/ 16_数值分析/
90_开放问题/
templates/math/ 7 份 T-*.md
```

> **领域优先级提醒**：`docs/math_upgrade_plan_20261015.md` 实测——普林斯顿分析四卷对本仓库
> 对口率 **23.53%**、对 P0 项帮助 **0**；工科五卷 ~**72%**（第 5 卷概率统计命中 P0-1）。
> 若学习时间服务于项目，优先建 `14_概率统计 / 15_优化理论 / 16_数值分析`，
> 并给每张卡加可选 `tdx_link` 回指仓库脚本，避免知识库变成孤岛。

## 6. 与项目库（tdx-data-feed vault）的关系

- 项目库由 `scripts/sync-to-obsidian.sh` **单向 repo → vault** `cp` 覆盖写入
  `00-知识库入口/`、`08-报告/`；手写数学笔记**不要**放进这些目录。
- 若共库（`--vault` 指向项目 vault），两套查询靠标签隔离：
  项目文档 `#tdx-data-feed`，数学卡 `#math-notes`。
- 双向同步（方向⑥）目前仍搁置，共库时请保持"数学卡只读消费、不回写仓库"。

## 7. 同步策略（Source/Deploy Policy · 2026-10-16 定型）

数学笔记库存在**三个位置**，单向流向：

```
┌──────────────────────────────┐     唯一事实源（必须先改这里）
│ etc/obsidian/math/MOC/总览.md  │ ◀────── 任何修改请编辑此文件
└──────────────┬───────────────┘
               │  scripts/install-math-moc.sh
               ▼
       ┌───────────────┴───────────────┐
       ▼                               ▼
┌────────────────────────┐    ┌──────────────────────────────┐
│ math-study vault        │    │ StockVault/05-知识库/数学/    │
│ (独立 git 仓)            │    │ (嵌入主库的子库)               │
│ 路径: ~/ObsidianVaults/ │    │ 路径: ~/StockVault            │
│         math-study       │    │                              │
│ 模板目录: templates/math/│    │ 模板目录: 06-模板/math/       │
│ 角色: 全量源              │    │ 角色: 嵌入子库 (mirror)       │
└────────────────────────┘    └──────────────────────────────┘
```

| 位置 | 角色 | 同步脚本 | 路径 |
|---|---|---|---|
| **源（项目仓库）** | **唯一权威** | 编辑这里 | `etc/obsidian/math/MOC/总览.md` |
| **math-study vault** | 全量源（独立 git 仓） | `install-math-moc.sh`（默认双 vault）| `~/ObsidianVaults/math-study/00_索引/总览.md` |
| **StockVault vault** | 嵌入子库（mirror）| `install-math-moc.sh`（默认双 vault）| `~/StockVault/05-知识库/数学/00_索引/总览.md` |

**当前状态**（2026-10-16 体检）：
- math-study vault **几乎是空壳**（14 md = 7 模板 + 1 总览 + 6 笔记；10/11/12/13 领域空）
- StockVault 部署 vault **反而多 11 张笔记**（与"源→部署"方向相反，是历史存量）
- **目标方向**：math-study 充实后回流 → StockVault 仍是 mirror
- **当前特殊**：StockVault 是事实上最完整的"内容池"，math-study 需补全

**操作约束**：
- 任何对"知识库总览/综述/质检规则"的修改 → 改 `etc/obsidian/math/MOC/总览.md`，跑 `install-math-moc.sh` 双 vault 同步
- vault 内**只读消费**（Dataview 渲染），不要直接编辑 vault 内总览——会被下次同步覆盖（除非 `--force`）
- 模板/笔记可由各 vault 独立积累（不在 MOC 同步范围内），但**核心规则在源**
