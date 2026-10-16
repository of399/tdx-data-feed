---
title: "<% tp.file.title %>"
type: 反例
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 研究生
status: 已掌握
depends: ["[[被反驳的命题]]"]
tdx_link:
tags:
  - math-notes
  - 反例
  - <% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> `flashcards/<领域>` 已由模板自动写入（按所在领域目录推导），建卡即入组；不需要成卡的笔记删掉该标签即可。

## 它反驳什么
> 被反驳的命题 / 直觉：……

## 构造
> 写出对象的具体构造（含关键参数）。

## 为什么条件失效
> 逐条对照原命题条件，指出哪一条不满足、以及失效的机制。

## 边界
> 改动一点（如加强某个条件）后命题是否恢复成立？

## 复习卡

<% tp.file.title %> 反驳了什么、如何构造？
?
反驳：……；构造：……；失效条件：……。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`。
> 定义卡与定理卡通过 `[[XXX的反例]]` 链接到本卡。
