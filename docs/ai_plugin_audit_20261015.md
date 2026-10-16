# AI 插件审计结论（2026-10-15）

> 配套：`docs/obsidian_phase_1_1_plan_20261015.md` 决策 4（AI 插件均纳入）、`docs/obsidian_upgrade_plan_20261015.md` Phase 1.2。

## 审计范围

StockVault 项目 vault 中启用的两个 AI 插件：

| 插件 | manifest id | 启用位置 |
|---|---|---|
| Copilot | `copilot` | `StockVault/.obsidian/community-plugins.json` 第 2 项 |
| Smart Connections | `smart-connections` | `StockVault/.obsidian/community-plugins.json` 第 1 项 |

---

## Copilot：审计结论 ✅ 纳入

### 数据流

- 提示词模板本地存储：`StockVault/copilot/system-prompts/`（项目 vault 之外）
- 默认嵌入：本地（无需远端 API key）
- 远端模型：用户显式启用 OpenAI / Anthropic 等时启用

### 隐私边界

- 笔记内容送入嵌入计算 = 视为内嵌 vault 内容，**默认不外发**
- 启用远端模型 = 笔记内容经 API 出去，**须用户显式同意**
- 审计规则：`system-prompts/` 中**禁止**写入：
  - 真实持仓信息（股票代码 + 数量 + 成本）
  - API key / token / 凭证
  - 客户/合作方敏感数据
  - 未公开财务数据

### 验收

- [x] `StockVault/copilot/system-prompts/` 仅含 prompt 模板
- [x] 启用插件清单中包含 `copilot`（已存在）
- [x] 远端模型默认关闭（用户未配置 API key）
- [x] 嵌入缓存入 `.gitignore`（`.obsidian/copilot/index.json`）

---

## Smart Connections：审计结论 ✅ 纳入

### 数据流

- 嵌入计算：本地或远端（按用户偏好）
- 嵌入存储：`.obsidian/plugins/smart-connections/`（含向量索引）
- 查询时：笔记标题 + 摘要用于语义匹配，**默认本地**

### 隐私边界

- 嵌入向量受 `.gitignore` 排除（`smart-connections/` 整体不提交 git）
- 嵌入 = 笔记内容的不可逆摘要，**不视为敏感数据外发**（本地计算时）
- 启用远端嵌入 = 向量送远端模型，**须用户显式同意**

### 验收

- [x] 启用插件清单中包含 `smart-connections`（已存在）
- [x] 嵌入目录入 `.gitignore`（`smart-connections/` 整体排除）
- [x] 远端嵌入默认关闭

---

## 共同约束白名单

| 行为 | 允许 | 禁止 |
|---|---|---|
| 笔记内容本地嵌入/索引 | ✓ | |
| 笔记内容外发到远端 | | ✓（除非显式启用远端模型并文档化）|
| 写 system-prompts 模板 | ✓ | |
| 写含密钥/持仓/客户数据的 prompt | | ✓ |
| 远端模型（OpenAI/Claude）调用 | 用户显式启用 | 默认可关 |
| 嵌入缓存入 git | | ✓（已在 .gitignore 排除）|

---

## 监控与变更

- 升级任一 AI 插件前：重跑本审计（diff .obsidian/community-plugins.json）
- 新增 system-prompts：人工 review 不得包含密钥/持仓
- 远端 API key 配置：建议放在 vault 之外的 `~/.config/obsidian-ai/`（不进 git）

---

## 审计签字

- 审计人：项目所有者
- 审计日期：2026-10-15
- 决策：均纳入，附白名单约束
- 下次重审：插件升级时 / 季度复审
