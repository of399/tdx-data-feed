---
title: "<% tp.file.title %>"
type: 定义
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 研究生
status: 学习中
depends: ["[[相关定义]]"]
tdx_link:
tags:
  - math-notes
  - 定义
  - <% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> `flashcards/<领域>` 已由模板自动写入（按所在领域目录推导），建卡即入组；不需要成卡的笔记删掉该标签即可。

## 定义
> 在这里写严格定义（Obsidian 原生支持 $LaTeX$：如 $\int f\,d\mu$）。

## 直觉解释
用一句话说明"它是什么、解决什么痛点"。

## 与我已知网络的关系
- 是 [[XXX]] 的推广 / 特例 / 对偶：原因是……
- 上游：本卡 `depends` 里列出的概念。
- **下游（自动反查，禁止手工维护 `used_by`）**：

```dataview
LIST
FROM #math-notes
WHERE !contains(file.folder, "06-模板") AND contains(depends, this.file.link)
```

## 反例 / 边界
- 链接到反例卡：[[XXX的反例]]（用 `T-反例` 模板创建）

## 复习卡
<!-- 卡体语法只认两种：单行的「问题 + 英文双冒号 + 答案」；或多行的「问题行」+「问号独占一行」+「答案行」。
两种错误写法都是静默 0 张卡：一是用 Q: / A:，二是把答案跟在问号同一行（插件要求问号那行 trim 后严格等于问号）。
本行有意不写出真实分隔符，否则加上 flashcards 标签后本行自身会被解析成一张垃圾卡。 -->

<% tp.file.title %> 是什么？
?
一句话定义 + 它解决的痛点。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`；`status ∈ {待整理,待证明,学习中,已掌握,开放问题}`。
> `depends` 必须写成带引号的 wiki 链接 `"[[概念]]"`，否则 Dataview 当字符串、图谱断链。
> `next_review` 与 Spaced Repetition 插件调度**二选一**：用插件就保持 `2099-12-31` 占位（勿留空，空值=YAML null 会被到期查询漏掉）。
