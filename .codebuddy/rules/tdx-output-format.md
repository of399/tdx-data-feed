---
description: tdx-data-feed 输出报告格式规范。预测、回测、训练等所有 Markdown 报告的统一样式，包含核心指标 + Top N + 风险提示 + 下一步建议。
alwaysApply: true
enabled: true
updatedAt: 2026-09-27T10:35:00.000Z
---

<system_reminder>
Apply this output format to ALL Markdown reports in tdx-data-feed.

## 通用结构

所有 Markdown 报告必须包含：

```
# {报告标题} ({YYYY-MM-DD HH:MM})

## 📊 核心指标
（4-6 个关键数字 + 简短说明）

## 🎯 Top N 明细
（表格，N=5/10/30）

## 📈 统计分布
（图表 / 阈值分布 / 区间统计）

## ⚠️ 风险提示
（3-5 条风险 / 注意事项）

## 🔧 下一步建议
（可执行操作）

---
生成时间：YYYY-MM-DD HH:MM:SS
生成者：{Agent 或脚本名}
模型版本：v{N}
数据日期：YYYY-MM-DD
```

## 文件命名

| 类型 | 模式 | 示例 |
|---|---|---|
| 预测 | `predict_YYYYMMDD_HHMMSS.{csv,md}` | `predict_20260927_183015.md` |
| 回测 | `plan_e_backtest_v{N}_YYYYMMDD_HHMMSS.{csv,md}` | `plan_e_backtest_v2_20260927.md` |
| 对比 | `{strategy}_compare_YYYYMMDD_HHMMSS.md` | `plan_e_compare_20260927.md` |
| 训练 | `train_v{N}_YYYYMMDD_HHMMSS.md` | `train_v7_20260927.md` |
| 实盘 | `journal_YYYY-MM-DD.md` | `journal_2026-09-27.md` |
| 周报 | `weekly_YYYY-W{NN}.md` | `weekly_2026-W39.md` |

## 数字格式

- **百分比**：保留 2 位小数（`0.5556` → `55.56%`）
- **金额**：保留 2 位小数（不带千分位）
- **数量**：千分位分隔（`7,620`）
- **耗时**：`Xh Ym` 或 `Xm Ys`
- **模型 ID**：`qwen3-14b-tdx-v{N}`

## 表格风格

- **对齐**：数字右对齐（`| ---: |`），文本左对齐
- **强调**：胜率 / 关键指标用 `**粗体**`
- **趋势**：用↑↓→ 标识变化方向
- **不确定性**：用 ⚠️ 标注

## 风险提示模板（必须含）

1. 历史回测不代表未来收益
2. 模型基于 2020-2026 数据训练，2026 年新模式可能失效
3. 翻倍策略属于激进策略，建议小资金启动
4. 严格止损（-10%）防止黑天鹅
5. 小市值 / 低流动性个股滑点可能放大（北交所数据已下线）

## 反模式（不要做）

- ❌ 用 emoji 装饰图标以外的内容
- ❌ 输出结果不加日期时间戳
- ❌ 给出绝对收益承诺（"保证 100% 翻倍"）
- ❌ 省略风险提示
- ❌ 用绝对路径，用 `scripts/...` 相对路径
</system_reminder>
