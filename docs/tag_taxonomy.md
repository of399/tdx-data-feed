# Obsidian 标签分类法（Tag Taxonomy）

> Phase 2.4 交付。统一于 `docs/obsidian_metadata_schema.md`。标签是跨模块检索与 Dataview 查询的 backbone。

## 命名空间（五大）

| 空间 | 适用模块 | 常见标签 |
|---|---|---|
| 项目 | 项目库 / 工程 | `project` `quant` `pipeline` `obsidian` |
| 数学 | 数学笔记 | `math-notes` `概率统计` `实分析` `优化理论` `数值分析` `线性代数` `随机过程` |
| 标的 | 个股笔记 | `标的` `A股` `北交所` `板块-xxx` |
| 框架 | 方法论 | `框架` `朝堂制` `v4` `系统学` |
| 回测 | 回测笔记 | `回测` `方案E` `方案D` |
| 数据 | 数据字典 | `数据` `行情` `复权` |

## 数学子空间（domain 标签）

与 `05-知识库/数学/<NN_领域>/` 目录对齐：

`概率统计` · `线性代数` · `优化理论` · `数值分析` · `实分析` · `随机过程`

每张数学笔记 `tags` 必含 `math-notes`，并追加其 domain 标签（如 `[math-notes, 概率统计]`）。

## 规则

1. 数学笔记 `tags` 必须含 `math-notes`（校验器强制）。
2. 标签小写、用连字符（`概率统计` 不用空格；`A股` 不写作 `a-share`）。
3. 同义标签统一：禁止 `A股` / `a股` / `A股股票` 混用。
4. 板块用 `板块-xxx` 前缀（如 `板块-银行`），便于 `GROUP BY`。
5. MOC / landing 页（`00-` 前缀、`*MOC*`、`总览`）豁免标签约束。

## 与校验器联动

- `verify_obsidian_metadata.py`：数学笔记缺 `math-notes` → 报错；`tags` 非列表/空 → 报错。
- `verify_obsidian_links.py`：依赖图基于 `depends: ["[[链接]]"]` 与 `used_by` 反查。
- 标签是 Dataview 查询（`docs/obsidian_dataview_snippets.md`）的过滤维度。
