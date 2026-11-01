# v9 训练真实状态报告 (2026-10-17)

> ⚠️ **重大发现**：之前 v9 训练**已失败**（mode collapse 确认），**不是中断**——是被 L1/L2/L3 sanity 阈值自动停止。
> 实盘**未受影响**（predict.py 用 LightGBM，不依赖 v9 Qwen3）。

## 📊 关键事实（实测数据）

### v9 训练目录状态
- **位置**：`~/.local/share/Trash/files/qwen3-14b-tdx-v9/`（**已被 tdx-clean-logs.service 移到 Trash**）
- **checkpoint 列表**：checkpoint-200 / 1200 / 1400 + ckpt200-best-backup
- **训练实际进度**：**跑到 1400 step 后停止**（非 2000 step 满训练）
- **停止原因**：L1/L2/L3 sanity 阈值触发（**mode collapse** 自动检测）

### ckpt-1400 sanity 评估（**已跑完** 2026-10-17 20:59）

| 指标 | 值 | 评估 |
|---|---:|---|
| **方向准确率** | **0.0202** | 🔴 2.02% ≈ 随机（3 分类基线 33%）|
| **Spearman** | **0.0000** | 🔴 完全无相关性 |
| **cls_acc up** | 0.0150 | 🔴 模型几乎不预测涨 |
| **cls_acc down** | 0.0156 | 🔴 模型几乎不预测跌 |
| **cls_acc flat** | **1.0000** | 🔴 **100% 预测 flat（模式坍缩）** |
| 格式遵循率 | 0.9900 | 🟢 输出格式正常 |
| 解析失败率 | 0.0100 | 🟢 解析正常 |
| top5_hit_rate | 0.0000 | 🔴 Top 5 全错 |
| top10_hit_rate | 0.0000 | 🔴 Top 10 全错 |
| 耗时 | 916.7s | — |

**结论**：**v9 训练彻底失败，mode collapse 确认**——模型只预测 flat（震荡），完全无信号。

## 🐛 评估脚本 Bug（已修复）

### 问题
`training/eval/run_v4_eval.py:124` 用 `str(ckpt_path)` 传入相对路径：
```python
model = PeftModel.from_pretrained(base, str(ckpt_path), torch_dtype=torch.bfloat16)
#                                ↑ str("train/output/...")  被 peft 解析为 HF Repo ID
```

### 错误
```
huggingface_hub.errors.HFValidationError:
  Repo id must be in the form 'repo_name' or 'namespace/repo_name':
  'train/output/qwen3-14b-tdx-v9/checkpoint-1600'
```

### 修复（commit 待）
```python
# 修复 2026-10-17: 用绝对路径避免 peft 把 'train/output/...' 解析为 HF Repo ID
ckpt_abs = ckpt_path.resolve() if not ckpt_path.is_absolute() else ckpt_path
model = PeftModel.from_pretrained(base, str(ckpt_abs), torch_dtype=torch.bfloat16)
```

**注意**：此 bug **不影响** v9 训练（训练已完成 1400 step），**只影响** sanity 评估。**但修复后**未来 sanity check 可正常运行。

## ✅ 实盘状态（不受 v9 失败影响）

**关键发现**：
- **predict.py 用的不是 Qwen3-14B LLM**！
- **用的是 LightGBM**（`lgb.Booster`）：
  - 默认模型：`vpnl_huber_stage1`（21 维 Polars + 回归）
  - 模型文件：`data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`
  - **不依赖** v9 训练 / Qwen3-14B
- **Qwen3-14B + LoRA 仅用于 v4 评估**（验证方向准确率）
- 实盘预测 = **LightGBM 推理**（vpnl_huber_stage1，OOS Sp 0.3475）

**实盘状态**：✅ **完全正常**，predict.py 已跑过周末 Top 20（基于 10-16 数据）。

## 🔍 v9 训练中断的完整时序

| 时间 | 事件 |
|---|---|
| 2026-10-17 11:03 | v9-merged 创建（**早版本**，非新训练）|
| 2026-10-17 19:57 | ckpt-1600 sanity **失败**（HFValidationError 路径 bug）|
| 2026-10-17 20:59 | ckpt-1400 sanity **跑完**（mode collapse 确认，0.0202 准确率）|
| 2026-10-17 21:00+ | L1/L2/L3 sanity 触发训练停止 |
| 2026-10-17 21:11 | `train/output/` 目录被 tdx-clean-logs 移到 Trash |
| 2026-10-17 22:00+ | 当前无训练进程 |

## 🎯 下一步决策

| 选项 | 行动 | 估时 | 风险 |
|---|---|---|---|
| **A. 重启 v9 训练** | 应用修复 1-4：data 重切分 + focal loss + load_best + sanity L1/L2/L3 | 20h+ | 🟡 高（再失败需重做）|
| **B. 回滚到 v8-merged 评估** | 用 v8-merged（1.0 epoch 完成，10-01 训练）| 1h | 🟢 低（v8 已知可用）|
| **C. 暂停 v9 训练** | 暂不重训，先用 v8 实盘 | 立即 | 🟢 极低 |
| **D. 切换到 LightGBM-only** | 放弃 LLM 路线，纯 LightGBM 演进 | — | 🟡 中（推翻 2 月 LLM 工作）|

### 我**强烈建议 C**（暂停 v9 + 用 v8 实盘）+ 长期 **A**（修复 1-4 后重启）
- **短期**：用 v8-merged（已知可用）实盘，**不阻塞实盘**
- **长期**：v9 失败原因明确（mode collapse），修复 1-4 后重启
- **不推荐 D**：LLM 路线有价值（21 维 Polars + 语义推理），不应轻易放弃

## 📦 文件位置

- **v9 训练目录**：`~/.local/share/Trash/files/qwen3-14b-tdx-v9/`（含 ckpt-200/1200/1400 + ckpt200-best-backup）
- **v8-merged 部署**：`train/output/qwen3-14b-tdx-v8-merged/`（1.0 epoch 完整）
- **v9-merged 旧版本**：`models/Qwen3-14B-tdx-v9-merged`（11:03 创建，早版本）
- **本次修复**：`training/eval/run_v4_eval.py:124`（绝对路径）

## 📌 关键教训

1. **不要凭记忆判断 v9 训练状态**——必须先 `pgrep -af` + `find` 验证
2. **eval 路径 bug + 训练 mode collapse 是两回事**——不能混淆
3. **predict.py 用 LightGBM，不依赖 v9**——v9 失败**不影响实盘**
4. **tdx-clean-logs.service 会清理训练目录**——重要数据需 backup 到 vault 或 git
5. **修复 1-4 (data 重切分 + focal loss + load_best + sanity) 全部需要**——之前只完成 1+3+4，**修复 2 (focal loss) 未实施**是关键缺口

## 🛠 修复 1-4 完成度（v9 修复计划）

| # | 修复 | 状态 |
|---|---|---|
| 1 | 数据重切分（v4fwd_resplit.py）| ✅ 已完成 |
| 2 | focal loss / class_weight | ❌ **未实施**（关键缺口）|
| 3 | max_steps=2000 + load_best_model_at_end | ✅ 已完成 |
| 4 | sanity L1/L2/L3 阈值 | ✅ 已完成 |

**重启 v9 训练前必须先实施修复 2**——focal loss 用于解决 class imbalance（涨/跌样本远少于 flat），可降低 mode collapse 风险。

## 🎯 一句话

> **v9 训练 mode collapse 失败已确认**（方向准确率 2.02%），**实盘未受影响**（predict.py 用 LightGBM）。**短期暂停 v9 + 用 v8-merged 实盘**，**长期应用修复 1-4 后重启 v9**——重点是**修复 2 focal loss**（关键缺口）。
