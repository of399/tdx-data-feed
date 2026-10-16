# Obsidian Dataview 查询 Snippet 集（Phase 2.4）

> 5+ 常用查询，复制进 Dataview 代码块即可用。依赖 `docs/tag_taxonomy.md` 的标签约定。

## 1. 持仓 / 标的清单

```dataview
LIST FROM #标的 SORT file.name
```

## 2. 数学复习到期（next_review ≤ 今天）

```dataview
TABLE domain AS 领域, level AS 层级, next_review AS 复习日
FROM #math-notes
WHERE next_review <= date(today)
SORT next_review ASC
```

## 3. 行业分布（按板块聚合标的）

```dataview
TABLE length(rows) AS 标的数量
FROM #标的
GROUP BY 板块
SORT 标的数量 DESC
```

## 4. 待写队列（skeleton 未填充）

```dataview
LIST FROM #math-notes
WHERE status = "skeleton"
SORT file.name
```

## 5. 跨库互链（指向项目库的笔记）

```dataview
TABLE related_project_vault AS 项目库链接
FROM ""
WHERE related_project_vault
SORT file.name
```

## 6. 回测方案汇总

```dataview
TABLE status AS 状态
FROM #回测
SORT file.name
```

## 7. 知识库最近更新

```dataview
TABLE 创建日期 AS 创建, 状态
FROM #知识库
SORT 创建日期 DESC
LIMIT 30
```

---
使用说明：代码块语言选 `dataview`；查询基于 frontmatter 字段与标签，字段缺失会导致漏查（先跑 `verify_obsidian_metadata.py` 保证完整性）。
