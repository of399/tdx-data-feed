# v9 Mode Collapse 根因分析报告 (2026-10-17)

> ⚠️ **修正之前记忆**：之前 MEMORY.md 记录的根因（"学习率 1e-4 过高"）**有误**——v9 实际学习率是 **5e-5**（与 v8 相同）。**新根因**：lora_rank 翻倍 + enable_thinking 漏关 + cutoff_len 过短。

## 📊 v9 vs v8 关键差异对照（实测配置）

| 配置项 | v8（成功）| v9（失败）| 影响 |
|---|---|---|---|
| **lora_rank** | 8 | **16** | rank 翻倍 → 容量翻倍 → 易过拟合 prompt 格式 |
| **lora_alpha** | 16 | **32** | alpha 翻倍 → **训练强度 2 倍** |
| **lora_dropout** | 0.05 | 0.05 | 相同 |
| **learning_rate** | 5.0e-5 | 5.0e-5 | **相同**（之前记忆有误）|
| **warmup_ratio** | 0.03 | 0.05 | v9 略长 |
| **cutoff_len** | 1280 | **512** | **v9 截断 2.5x 过激** |
| **enable_thinking** | **false** ⚠️ | **未设**（默认开）⚠️ | **v8 关键修复被 v9 漏掉** |
| **dataset** | tdx_v8_train | **tdx_v4fwd_train** | 不同数据源 |
| **max_samples** | — | 50748 | v4fwd 重切分后 |
| **quantization_bit** | 4 | 4 | 相同 |
| **packing** | (未提) | true | v9 启用 packing |

## 🔍 关键根因（按重要性排序）

### 🔴 根因 1：**enable_thinking 未设**（v8 关键修复被漏掉）

v8 配置注释里明确写：
> `# enable_thinking: false  # 关闭 thinking，修正训练/推理不一致（v8 首训几乎全预测"跌"）`

v9 配置文件**完全没有这一行** → llamafactory 使用默认（**开启 thinking**）→ 训练时模型用 thinking 模式输出"涨/跌/震"，但推理时**不**用 thinking → **训练-推理不一致** → 模型倾向输出"最稳"的"震荡"（flat）。

**为什么"震荡"最稳？**
- thinking 模式下，模型倾向"全面分析"后给"中立"答案
- "震荡"在 A 股 20 日窗口最常见（**实际数据也显示 flat 占 33%**）
- 模型学会"任何情况都答震荡"= 33% 准确率

### 🔴 根因 2：**lora_rank/alpha 翻倍**（8/16 → 16/32）

| 维度 | v8 | v9 | 影响 |
|---|---|---|---|
| rank | 8 | 16 | 容量 2x |
| alpha | 16 | 32 | 强度 2x |
| alpha/rank 比 | 2.0 | 2.0 | **相同**（正常）|
| **绝对容量** | 8 | **16** | **v9 容量是 v8 的 2 倍** |

**为什么 rank 大会过拟合？**
- 21 维 Polars 特征 → LLM 输入 token 序列（cutoff_len=512）
- 任务：3 分类"涨/跌/震"
- rank=8：~4M 参数可学
- rank=16：~8M 参数可学 → **多余容量易记住"prompt 格式"而非"信号→label"**
- 模型学会"任何 prompt 都答震荡"（最常见 label）

### 🟡 根因 3：**cutoff_len 减半**（1280 → 512）

v4fwd 输入包含 20 日 K线 + 21 维指标 → token 数可能 > 512。

**实测 v8**：cutoff_len=1280，"实测 max=1361 token"（v8 注释里写）

**v9 用 512**：
- 大部分样本**被截断** → 模型只看到前 512 token（不到一半信息）
- 模型学不到完整 20 日模式 → **倾向给"中立"答案（flat）**
- **input 截断 = label 信息丢失** → 模型降级为"猜最常见 label"

## 📉 Loss 曲线异常（关键证据）

```
step  20: loss 11.65  ← 异常高
step  40: loss  9.19  ← 快速下降
step  60: loss  3.04  ← 继续下降
step  80: loss  0.29  ← 急剧下降 40 倍
step 100: loss  0.25  ← 继续下降
step 1420-1600: loss 0.13-0.15  ← 低位震荡（mode collapse 锁定）
```

**异常分析**：
- **100 步内 loss 降 40 倍**（11.65 → 0.25）= 极度过拟合
- **0.13-0.15 持续 1300+ 步** = 模式坍缩锁定（无法回弹）
- 0.13-0.15 ≈ exp(-0.14) = 0.87 = 模型给**单一答案 87% 置信度**

**正常 3 分类 loss 范围**：
- 随机：-log(1/3) ≈ 1.10
- 收敛后：0.4-0.8（有学习）
- mode collapse：< 0.20（单一答案高置信）

**v9 实际 loss=0.13** = **典型 mode collapse**（与 100% flat 预测一致）

## 🎯 根因总结

| # | 根因 | 重要性 | 修复方法 |
|---|---|---|---|
| 1 | enable_thinking 未设 | 🔴 最重要 | 加 `enable_thinking: false` |
| 2 | lora_rank 翻倍 8→16 | 🔴 关键 | 改回 `lora_rank: 8, alpha: 16` |
| 3 | cutoff_len 减半 1280→512 | 🟡 重要 | 改回 `cutoff_len: 1280` |
| 4 | packing: true（v8 未用）| 🟡 中等 | 考虑改 false 或验证效果 |
| 5 | focal loss 未实施 | 🟡 中等 | 加 focal loss（γ=2 + class_weight）|

## 🛠 v10 训练修复路线图

### 必须修复（缺一不可）

1. **enable_thinking: false** ← **v8 关键修复，不能漏**
2. **lora_rank: 8, lora_alpha: 16** ← 回到 v8 容量
3. **cutoff_len: 1280** ← 回到 v8 长度
4. **focal loss**（γ=2.0）← mode collapse 预防（**不是为 class_weight**——v4fwd 已平衡）

### 建议修复

5. **dataset 改为 tdx_v8_train** ← 复用 v8 已知可用数据
6. **packing: false** ← 验证 packing 是否影响
7. **early stop**（ckpt-100 后 direction_acc < 0.20 立即停训）← 早期止损

### 不建议

- **学习率**（5e-5 与 v8 相同，无需改）
- **max_steps**（2000 够用，v8 用 1040 已收敛）
- **quantization**（4bit 正常）

## 📊 v4fwd 数据集 label 分布（重要实测）

```
train (50,748 行): up 33.33% | down 33.33% | flat 33.33%  ← 完美平衡
val    (3,762 行): up 33.33% | down 33.33% | flat 33.33%
test   (2,784 行): up 33.33% | down 33.33% | flat 33.33%
```

**重要**：v4fwd 数据**已完美平衡**（1:1:1），**不是**类别不平衡问题。**之前 MEMORY.md "class_weight 必实施"的根因假设有误**——**真正问题不是不平衡，是模型坍缩到单一答案**。

**修正**：focal loss 仍有价值（防 mode collapse），但**不是为了 class_weight**，**是为了 γ focusing**（让 hard examples 权重更高）。

## 📌 关键教训

1. **不要凭印象判断训练配置**——必须看实际 yaml
2. **enable_thinking 是 v8 关键修复**——v9 漏掉是 v9 失败的核心
3. **lora_rank 翻倍易过拟合**——21 维特征任务不需要大 rank
4. **cutoff_len 不能太小**——截断 = 信息丢失
5. **loss 急剧下降 40 倍 = mode collapse 早期信号**——应在 step 100 检测
6. **数据平衡≠防 mode collapse**——即使 1:1:1，模型仍可坍缩到单一答案
7. **训练盲训不可取**——必须早期 sanity check（ckpt-100, ckpt-200）

## 🔍 验证方法（v10 训练时必做）

### 早停检测

```bash
# ckpt-100 评估 direction_acc（核心指标）
venv/bin/python training/eval/run_v4_eval.py \
    --ckpt /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v10/checkpoint-100 \
    --base /home/jiuben/models/Qwen3-14B \
    --quantize-4bit --limit 200

# 若 direction_acc < 0.20 立即停训
```

### 损失曲线监控

```bash
# 每 50 步看 loss
tail -f train/output/qwen3-14b-tdx-v10/trainer_log.jsonl | \
  python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    print(f'step {d[\"current_steps\"]:4d} loss {d[\"loss\"]:.4f} lr {d[\"lr\"]:.2e}')
"
# loss 急剧下降 5x+ 触发警觉
```

## 🎯 一句话

> **v9 失败的真因** = **enable_thinking 漏关** + **lora_rank 翻倍** + **cutoff_len 减半**——这 3 项让模型**快速学会"全预测 flat"**（100 步 loss 降 40 倍），与 v8 注释"v8 首训几乎全预测跌"是**同一类问题**。**v10 训练必须把这 3 项改回 v8 配置**，**并加 focal loss 防 mode collapse**。

## 📦 文件位置

- **本次报告**：`docs/V9_MODE_COLLAPSE_ROOT_CAUSE_2026-10-17.md`
- **v9 训练配置**：`~/.local/share/Trash/files/tdx_v9_from_scratch.yaml`
- **v9 trainer_log**：`~/.local/share/Trash/files/qwen3-14b-tdx-v9/trainer_log.jsonl`
- **v8 训练配置（参考）**：`training/config/tdx_v8_resume.yaml`
- **v9 训练目录（备份中）**：`models/_archive/qwen3-14b-tdx-v9-failed-2026-10-17/`（R1 备份）
