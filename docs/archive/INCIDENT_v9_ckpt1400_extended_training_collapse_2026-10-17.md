# v9 训练彻底失败事件报告（2026-10-17 21:00）

> 接续 `INCIDENT_v9_collapse_2026-10-16.md`：原报告分析 ckpt-200 (4%) mode collapse，本报告分析 **ckpt-1400 (70%)**——修复 1-3 全部失效，坍缩更深。

## 🚨 关键发现：v9 训练**彻底失败**

| 指标 | ckpt-200 (4%) | **ckpt-1400 (70%)** | 变化 | L2 阈值 | 判定 |
|---|---:|---:|---|---:|:---:|
| direction_accuracy | 0.3015 | **0.0202** | **-93%** | 0.50 | ❌❌ |
| cls_acc_up | 0.4403 | **0.0150** | **-97%** | 0.35 | ❌❌ |
| cls_acc_down | 0.0000 | 0.0156 | +1.5pp | 0.35 | ❌ |
| cls_acc_flat | 1.0000 | 1.0000 | — | 0.35 | ✅ (全预测平) |
| spearman | 0.0000 | 0.0000 | — | 0.05 | ❌ |
| top5_hit_rate | — | **0.0000** | — | — | ❌ |
| top10_hit_rate | — | **0.0000** | — | — | ❌ |
| format_compliance | — | 0.9900 | — | ≥0.95 | ✅ |

**灾难性结果**：
- **direction_accuracy = 0.0202（2%）**——3 分类随机 33%，**比随机差 16 倍**
- **cls_acc_up = 0.015（1.5%）**——几乎全错
- **top5/10 命中率 = 0.0**——排序能力全无
- **格式遵循率 99%**——输出格式 OK，但**内容全错**

**典型 mode collapse 终态**：
- 训练 loss 0.13（看似收敛）
- 模型**完全坍缩成"全预测平"模式**
- 4/5 指标不达标（仅 cls_acc_flat 通过是虚假的——是坍缩症状）

## 📉 修复 1-3 全部失效

| 修复 | 实施 | 是否阻止坍缩 |
|---|---|:---:|
| **修复 1 数据重切分** | ✅ v4fwd_resplit.py 已写、已重切分 | ❌ 仍坍缩 |
| **修复 2 focal loss** | ❌ **未实施**（因 OOM 杀训练前没机会）| — |
| **修复 3 延长训练到 2000 + load_best** | ✅ max_steps: 2000 + load_best_model_at_end: true | ❌ 坍缩**更深**（30% → 2%）|
| **修复 4 sanity L1/L2/L3 阈值** | ✅ 已实装 | ⚠️ 阈值从未生效（全部 RC_ERROR） |

**关键教训**：**延长训练让坍缩更深，不是修复**——这是反直觉的发现。

## 🔍 根因重新分析

### 已观察到的（INC-2026-10-16）

1. ✅ 数据分布漂移（train/val/test 3 集标签分布不一致）
2. ✅ step 60→80 loss 骤降 35x 典型 mode collapse 早期症状
3. ✅ 3 分类 CE 损失对少类不友好
4. ✅ 时序漂移（train 2016-2024 vs test 2026）

### 新观察（ckpt-1400 验证后）

5. ⚠️ **训练 loss 0.13 表面收敛**——但 5 指标全崩（loss 不代表泛化能力）
6. ⚠️ **format_compliance = 99%**——模型**学会格式但丢内容**——典型过拟合到 prompt 模板
7. ⚠️ **load_best_model_at_end 在 mode collapse 下选不到 best**——所有 checkpoint 都在坍缩，load_best 只能选"最不坍缩的"（仍是 2% direction_acc）

### 真实根因（推断）

**最大嫌疑**：**学习率 1e-4 对 14B + LoRA rank16 过高**——在坍缩一旦触发后无回弹机制。
- 修复 1 数据平衡：数据没大变化（仅 3 集分布更一致）
- 修复 3 延长训练：让模型**更确定**地坍缩到"全预测平"模式
- **修复 2 focal loss** 是关键缺失——若实施可能压住"平"类对 loss 的贡献

**其他可能**：
- 14B 模型 + 3 分类任务规模不匹配（参数远多于需要）
- LoRA rank16 不足以表达 3 分类决策边界
- 数据量 64k 训练 70% 后已完全学完，剩余 30% 是死记硬背

## 📊 训练历史

| 时刻 | 事件 |
|---|---|
| 2026-10-14 18:52 | 训练开始（max_steps=2000）|
| 10-14~10-16 | 多次重启（SIGTERM/OOM），消耗 ~13h 实际训练时间 |
| 10-16 23:42 | ckpt-200 落地，5 指标 FAILED（**核心触发原报告**）|
| 10-17 02:50 | ckpt-600 落地（sanity RC_ERROR，因训练同时占 GPU）|
| 10-17 04:24 | ckpt-800 落地 |
| 10-17 05:58 | ckpt-1000 落地 |
| 10-17 07:34 | ckpt-1200 落地 |
| 10-17 09:10 | ckpt-1400 落地（**最终可用 checkpoint**）|
| 10-17 19:37:13 | **Kernel OOM Killer 杀所有用户进程**（Ollama 25GB 占内存 + 训练 + 内存压力）|
| 10-17 21:00 | **ckpt-1400 sanity 跑完——5 指标 FAILED，训练彻底失败** |

## 🎯 决策与行动

### 立即（已执行）

- ✅ ckpt-1400 sanity 跑完（4bit 单卡路径，15 min 完成）
- ✅ metrics_checkpoint-1400.json 落盘
- ✅ 5 指标确认 FAILED

### 后续决策点

| 选项 | 行动 | 风险 |
|---|---|---|
| **K1（推荐）** | **回退到 v7r/v7r1115**（Ollama 已部署，eval_loss 0.1914）| 0 风险 |
| K2 | 重新设计 v9 训练（降 LR 10x + focal loss + 更小 rank）| 8-10h 训练 + 不确定能修复 |
| K3 | 暂停 v9，专注 v7r 实盘跟踪 | 0 风险 |
| K4 | 重做数据（更严格时序分层 + 时序对齐 test 集）| 大改动 + 重训 |

**K1 是当前最稳选择**——v9 投入已 60h+，ckpt-1400 不可用，**不应再投入更多时间**。
- v7r1115 部署 2 周前，Ollama 仍在线
- v7r-q8 部署 9 天前，9 days modified
- 这两个都是**已验证可工作**的模型

### 长期反思（下次训练前必须解决）

1. **学习率**：1e-4 → 5e-5 或更低（14B + LoRA 推荐 1e-5~5e-5）
2. **focal loss** 必实施（γ=2.0 + class_weight 平衡）
3. **更小 LoRA rank**：rank 16 → rank 8（防过拟合到 prompt 格式）
4. **数据验证**：训练时同步看 sanity 5 指标（不能只看 loss）
5. **早期止损**：ckpt-100 后若 direction_acc < 0.20 ⇒ 立即停训
6. **Ollama 治理**：训练前 `systemctl stop ollama` 释放 25GB CPU mem（避免 OOM 杀）

## 📦 相关文件

- `training/eval/metrics_checkpoint-1400.json`（5 指标全崩）
- `training/eval/report_checkpoint-1400.md`（报告生成 OK）
- `train/output/qwen3-14b-tdx-v9/checkpoint-1400/`（adapter 14 文件齐全，257 MB）
- `docs/INCIDENT_v9_collapse_2026-10-16.md`（**前事件报告**——本报告的接续）
- `data/plan_e/plan_e_backtest_v1_20261016_225003.md`（v1 全量回测 DSR=0.180/NW=0.853 不显著——**项目整体量化策略当前是"无显著 alpha"**）
- `ollama list`：`qwen3-14b-tdx-v7r1115:latest`（2 weeks ago）、`qwen3-14b-tdx-v7r-q8:latest`（9 days ago）—— **回退目标**

## 🎯 一句话结论

**v9 训练彻底失败**——修复 1-3 全部无效，模式坍缩在 70% 训练后达终态（2% direction accuracy）。**应回退到 v7r/v7r1115**（已部署可工作），**不应再投入更多时间在 v9**。
