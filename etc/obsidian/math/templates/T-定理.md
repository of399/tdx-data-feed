---
title: "<% tp.file.title %>"
type: 定理
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 研究生
status: 待证明
depends: ["[[定义A]]", "[[定义B]]"]
tdx_link:
tags:
  - math-notes
  - 定理
  - <% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> `flashcards/<领域>` 已由模板自动写入（按所在领域目录推导），建卡即入组；不需要成卡的笔记删掉该标签即可。

## 条件
- 条件1
- 条件2

## 结论
> 结论陈述（含公式）。

## 证明思路
- 关键技巧：……
- 完整步骤另开一卡：[[XXX的证明]]（用 `T-证明` 模板创建）

## 与我已知网络的关系
- 依赖：见 `depends`。
- **下游（自动反查）**：

```dataview
LIST
FROM #math-notes
WHERE !contains(file.folder, "06-模板") AND contains(depends, this.file.link)
```

## 反例 / 边界
> 哪一条条件是必要的？去掉会怎样？（链到 [[XXX的反例]]）

## 复习卡

<% tp.file.title %> 的条件与结论？
?
条件：……；结论：……。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`；`status ∈ {待整理,待证明,学习中,已掌握,开放问题}`。
> `depends` 必须写成带引号的 wiki 链接 `"[[概念]]"`，否则 Dataview 当字符串、图谱断链。
> 复习调度交给 Spaced Repetition（FSRS）；不用插件才手填 `next_review`（勿留空）。
