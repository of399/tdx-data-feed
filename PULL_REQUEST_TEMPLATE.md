<!--
GitHub Pull Request 模板
当用户在 GitHub 创建 PR 时，会自动填充到 PR 描述框。
-->

## 📋 变更说明

<!-- 简要描述本次 PR 的目的和变更内容 -->

## 🔗 关联 Issue

<!-- 关联的 Issue 编号（如 Closes #123），多个用逗号分隔 -->

Closes #

## 🏷️ 变更类型

<!-- 删除不适用的 -->

- [ ] 🚀 新功能 (feature)
- [ ] 🐛 Bug 修复 (fix)
- [ ] ♻️ 重构 (refactor)
- [ ] 📚 文档 (docs)
- [ ] 🧪 测试 (test)
- [ ] ⚡ 性能优化 (perf)
- [ ] 🔧 构建 / 工具 (chore)
- [ ] 📦 依赖更新 (deps)

## 📊 影响范围

<!-- 影响哪些模块？是否涉及实盘风险？ -->

- [ ] 数据采集层（tdxfeed / download_*）
- [ ] 量化分析层（find_* / plan_*）
- [ ] 模型训练层（train_*）
- [ ] 模型部署层（deploy_*）
- [ ] 应用层（predict / plan_e_journal）
- [ ] 自动化层（systemd timer）
- [ ] 监控层（canary）
- [ ] 文档
- [ ] **实盘风险**（如涉及，请详细说明）

## 📸 截图 / 输出

<!-- 如有 -->

## 📝 主要变更点

<!-- 列出关键文件 + 改了什么 -->

- `path/to/file.py`:
- `path/to/another.py`:

## 🧪 测试

<!-- 测试情况 -->

- [ ] 单元测试已添加
- [ ] 集成测试已添加
- [ ] 手动测试通过
  - [ ] 测试命令：`...`
  - [ ] 输出：`...`
- [ ] 已有测试无回归

## ✅ 检查清单

<!-- 提交前必查 -->

- [ ] 代码通过 `make lint`
- [ ] 代码通过 `make format`
- [ ] 测试通过 `make test`
- [ ] 健康检查通过 `make canary`
- [ ] 文档已更新（如需要）
- [ ] CHANGELOG.md 已更新
- [ ] commit message 遵循 Conventional Commits
- [ ] 与目标分支无冲突（或已手动解决）
- [ ] 单 PR ≤ 500 行（不含 lockfile）

## 📚 文档更新

<!-- 哪些文档已更新 -->

- [ ] README.md
- [ ] CONTRIBUTING.md
- [ ] SECURITY.md
- [ ] docs/ARCHITECTURE.md（如有架构变更）
- [ ] docs/API.md（如新增 / 修改 API）
- [ ] docs/PLAN_E*.md（如策略变更）
- [ ] Makefile（如新增命令）
- [ ] CHANGELOG.md

## 🚨 风险评估

<!-- 如有潜在风险 -->

### 数据风险
<!-- 是否影响生产数据 -->

### 实盘风险
<!-- 是否影响正在交易的策略 -->

### 性能风险
<!-- 是否增加显存 / CPU / 存储 -->

## 🔍 Reviewer Checklist

<!-- 给 reviewer 看的 -->

- [ ] 代码可读性高
- [ ] 无重复代码（DRY）
- [ ] 函数单一职责
- [ ] 命名清晰规范
- [ ] docstring 完整
- [ ] 错误处理合理
- [ ] 无硬编码密钥
- [ ] 无 SQL 注入 / 路径遍历风险

## 📎 相关链接

<!-- 相关 Issue / PR / 文档 -->

- Issue:
- PR:
- 文档:
- 讨论:

---

## 🤖 自动检查

<!-- CI 会自动跑 -->

- ✅ `lint.yml`：ruff check
- ✅ `quick-sop.yml`：pytest
- ✅ `sync-docs.yml`：文档同步（如有变更）

---

**注意事项**：

1. PR 描述必须填写完整，未填写的可能被打回
2. 单 PR 不超过 500 行（不含 lockfile）
3. 必须有至少 1 个 Reviewer 批准才能合并
4. CI 全绿后才能合并
5. 重大变更需在 ARCHITECTURE.md 新增 ADR

---

> 维护者：jiuben
> 模板版本：1.0.0
> 最后更新：2026-09-27