---
title: "<% tp.file.title %>"
type: 书索引
domain: <% (tp.file.folder().replace(/^\d+_/, '').replace('开放问题', '跨领域')) || '跨领域' %>
level: 研究生
status: 学习中
depends: []
tdx_link:
tags:
  - math-notes
  - 书索引
next_review: 2099-12-31
created: <% tp.date.now("YYYY-MM-DD") %>
---

> 书索引是文献枢纽，**默认不含** `flashcards/<领域>`（不进 Spaced Repetition 卡组）；如需把某本书也当复习卡，手动加 `flashcards/<领域>` 标签即可。

## 本章覆盖笔记
- [[测度]]
- [[勒贝格积分]]
- [[收敛定理]]
- [[拉东-尼科迪姆定理]]

> 有新卡时在此追加链接；下面的统计会自动更新。

## 掌握度（自动统计，勿手填）

```dataview
TABLE WITHOUT ID
  file.link AS 笔记,
  status AS 状态,
  file.mtime AS 最近修改
FROM #math-notes
WHERE !contains(file.folder, "06-模板") AND contains(file.outlinks, this.file.link) OR contains(this.file.outlinks, file.link)
SORT status ASC
```

```dataview
TABLE length(rows) AS 数量
FROM #math-notes
WHERE !contains(file.folder, "06-模板") AND contains(file.outlinks, this.file.link)
GROUP BY status
```

## 章节与项目价值（可选）
> 对照 `docs/math_upgrade_plan_20261015.md`：本章对本项目 P0/P1 是否有帮助？没有就写"长期储备"，避免投入错配。

---
> 字段约定：`type ∈ {定义,定理,例子,反例,证明,开放问题,书索引}`。
