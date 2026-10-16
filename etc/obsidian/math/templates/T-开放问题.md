---
title: "<% tp.file.title %>"
type: 开放问题
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 研究前沿
status: 开放问题
depends: ["[[相关概念]]"]
tdx_link:
tags:
  - math-notes
  - 开放问题
  - <% 'flashcards/' + (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域') || '跨领域') %>
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> `flashcards/<领域>` 已由模板自动写入（按所在领域目录推导），建卡即入组；不需要成卡的笔记删掉该标签即可。

## 问题陈述
> 严格写出未决问题。

## 当前研究状态
- 已知部分结果：……
- 数值 / 理论进展：……
- 更新于：<% tp.date.now("YYYY-MM-DD") %>

## 关联猜想与领域
- 关联 [[猜想A]]、[[领域B]]

## 与我项目的关系（可选）
> `tdx_link`：对应本仓库哪个脚本 / 概念（如 `scripts/factor_significance.py`）。没有就删掉本段。

## 复习卡

<% tp.file.title %> 目前进展如何？
?
数值进展：……；理论进展：……；仍缺：……。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`；`status` 对开放问题恒为 `开放问题`。
