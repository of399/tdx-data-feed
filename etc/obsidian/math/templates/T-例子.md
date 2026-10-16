---
title: "<% tp.file.title %>"
type: 例子
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 本科
status: 已掌握
depends: ["[[相关定义]]"]
tdx_link:
tags:
  - math-notes
  - 例子
  - <% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> `flashcards/<领域>` 已由模板自动写入（按所在领域目录推导），建卡即入组；不需要成卡的笔记删掉该标签即可。

## 正例
> 一个满足该概念的具体对象，写出构造。

## 边界例 / 反例
> 逼近条件边界的情形，说明为何仍成立或不成立；不成立时另开 [[XXX的反例]]。

## 说明了什么
- 它验证了 [[相关定义]] 的哪一个条件是不可省的？

## 复习卡

举一个 <% tp.file.title %> 的例子？
?
构造：……；它说明：……。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`；`status ∈ {待整理,待证明,学习中,已掌握,开放问题}`。
> `depends` 必须写成带引号的 wiki 链接 `"[[概念]]"`。
