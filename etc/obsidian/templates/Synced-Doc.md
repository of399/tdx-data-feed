---
title: <% tp.file.title %>
created: <% tp.date.now("YYYY-MM-DD") %>
tags:
  - tdx-data-feed
doc_type: note
related_strategy:
source:
---

<% tp.file.cursor(1) %>

> 手动笔记模板：字段与 `scripts/sync-to-obsidian.sh` 自动同步文档保持一致。
> - `doc_type`：note / 策略 / 数据 / 入口 …
> - `related_strategy`：如 plan_e
> - `source`：若引用仓库内某文件，填其仓库相对路径
