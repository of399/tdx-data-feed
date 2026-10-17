# qwen3-14b-tdx v4 评估报告（20261017_204258）

## 配置
- Checkpoint: `train/output/qwen3-14b-tdx-v9/checkpoint-1400`
- Base model: `/home/jiuben/models/Qwen3-14B`
- 样本数: **200**

## 核心指标

| 指标 | 值 |
|---|---|
| 方向准确率 | **0.0202** |
| Spearman（pred_pct vs real_pct） | **0.0000** |
| 三分类 acc (up/down/flat) | **0.0150 / 0.0156 / 1.0000** |
| Top-5 命中率 | 0.0000 |
| Top-10 命中率 | 0.0000 |
| 回测夏普（年化） | **0.000** |
| 年化收益 | 0.000 |
| 最大回撤 | 0.000 |
| alpaca 格式遵循率 | 0.9900 |
| 解析失败率 | 0.0100 |

## 方案 B Sanity 5 指标（2026-10-16 watchdog 判定用）

判定公式：**acc ≥ 0.33 AND sp ≥ 0.33 AND cls_acc_up ≥ 0.33 AND cls_acc_down ≥ 0.33 AND cls_acc_flat ≥ 0.33**

| 指标 | 值 | 阈值 | 通过 |
|---|---:|---:|:---:|
| direction_accuracy | 0.0202 | 0.33 | ❌ |
| spearman | 0.0000 | 0.33 | ❌ |
| cls_acc_up | 0.0150 | 0.33 | ❌ |
| cls_acc_down | 0.0156 | 0.33 | ❌ |
| cls_acc_flat | 1.0000 | 0.33 | ✅ |

## 解读阈值（建议）

- 方向准确率 ≥ 0.55 → 模型具备基础方向判断能力
- Top-10 命中率 ≥ 0.30 → 候选池筛选有效
- 夏普比率 ≥ 1.0 → 模拟交易有正向 alpha
- 格式遵循率 ≥ 0.95 → 输出可直接下游消费

## 下一步

1. 若所有阈值通过 → 模型可上线，进 `tdxfeed.cli predict` 子命令
2. 若方向/夏普不达标 → 评估是否需要 DPO（§14.7）或重训（启用 packing + torch.compile）
3. 若格式遵循率不达标 → 抽 100 条人工检查 input/output 错位
4. Top-k/夏普低 → 数据层面：检查 test split 是否时序连续（§6.3 时序切分）
