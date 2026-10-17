# v9 训练最终状态报告 + K1 决策执行 (2026-10-17 21:10)

> 接续 `INCIDENT_v9_ckpt1400_extended_training_collapse_2026-10-17.md`：ckpt-1400 5 指标 FAILED 后，用户决策 K1（回退 v7r/v7r1115）。本报告记录 **K1 执行确认 + 资产清单 + 长期反思**。

## ✅ K1 决策执行确认

**K1 含义**：回退到 v7r/v7r1115 继续实盘，不再追 v9。

**实际状态**——K1 **已"自然"实现**：

| 路径 | 状态 | 说明 |
|---|---|---|
| **主路径（方案 D 预测）** | ✅ **一直在 vpnl_huber_stage1** | `scripts/predict.py:102 DEFAULT_MODEL = "vpnl_huber_stage1"`（LightGBM 21维 Polars 阶段1因子迁移）|
| **Ollama LLM 信号生成** | ✅ v7r1115 健康 | `qwen3-14b-tdx-v7r1115:latest`（29GB，2 weeks ago）|
| **Ollama v9 部署** | ⚠️ 仍存在（29GB）| **但无生产代码引用**，可视为"历史资产" |
| **方案 D 模型文件** | ✅ 完整 | `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`（550KB, 10-10）|

**结论**：**线上从未切到 v9**——v9 是 LoRA 训练实验，方案 D 主路径是独立 LightGBM 路径。**无需任何代码切换**。

## 📊 v9 资产清单（59 GB 沉没成本）

| 项 | 路径 | 大小 | 状态 |
|---|---|---:|---|
| 训练输出 | `train/output/qwen3-14b-tdx-v9/` | **2.2 GB** | 8 个 ckpt + trainer_log.jsonl（保留供对比）|
| GGUF merged | `/home/jiuben/models/Qwen3-14B-tdx-v9-merged-f16.gguf` | **28 GB** | F16 量化（保留供未来重新实验）|
| Ollama 部署 | `qwen3-14b-tdx-v9:latest` | **29 GB** | 9 hours ago（健康但无引用）|
| v9 ckpt-200_presanity | `train/output/qwen3-14b-tdx-v9-ckpt200-best-backup/` | 751 MB | 永久备份（存放在 output_dir 外，防 save_total_limit 滚动）|
| **总计** | | **~59 GB** | 全部为"实验性资产"，**未投入生产** |

**清理建议**（**仅在用户明确要求时执行**）：
```bash
# 释放 59 GB 磁盘（破坏性操作，不可逆）
ollama rm qwen3-14b-tdx-v9          # 释放 29 GB
rm /home/jiuben/models/Qwen3-14B-tdx-v9-merged-f16.gguf  # 释放 28 GB
# 保留 train/output/qwen3-14b-tdx-v9/ 供未来对比（2.2 GB 价值高）
```

## 🛠 当前系统状态

| 进程 | 状态 | 备注 |
|---|---|---|
| v9 训练 | ❌ 已死 | OOM Killer 杀于 10-17 19:37:13 |
| v9_train_watchdog | ❌ 已死 | 同一时刻被杀 |
| plan_e 监控 | ❌ 未启动 | 周末市场关闭可能正常 |
| systemd timer | ⏰ 下周一 | `tdxfeed-sync.timer` 18:00，`tdx-clean-logs.timer` 3:00 |

**无 v9 训练相关 timer**——v9 训练**没有**定时重试机制（一次性训练，无回滚）。

## 🧠 长期反思（下次 LoRA 训练前必读）

### 失败根因

1. **学习率 1e-4 对 14B + LoRA rank16 过高**——一旦坍缩无回弹机制
2. **缺 focal loss**——"平"类天然占 ~30%，CE loss 容易让模型学会"全预测平"
3. **load_best_model_at_end 在 mode collapse 下选不到 best**——所有 checkpoint 都在坍缩
4. **训练期间 sanity 全部 RC_ERROR**（GPU OOM）——5 指标从未在 ckpt-400/600/800/.../1400 跑过，等于**盲训**

### 下次训练前必解决

| # | 问题 | 解决 |
|---|---|---|
| 1 | LR 过高 | **1e-4 → 5e-5**（14B + LoRA 推荐）|
| 2 | 缺 focal loss | **必实施**（γ=2.0 + class_weight 平衡）|
| 3 | 过拟合 prompt 格式 | LoRA rank **16 → 8** |
| 4 | 训练盲训 | **改 watchdog：sanity 跑前先释放 GPU 资源 / 用 4bit 路径（单卡 13GB 训练期也能跑）**|
| 5 | Ollama 25GB 杀训练 | **训练前 `systemctl stop ollama`**（评估对其他服务影响）|
| 6 | 数据验证缺失 | **训练时同步跑 sanity 5 指标**（不能只看 loss）|
| 7 | 早期未止损 | **ckpt-100 后若 direction_acc < 0.20 立即停训** |

### 时间线教训

- v9 训练 60h+ 实际投入（约 10-14 重启次数 + 13h+ 实际训练）
- ckpt-200 (4%) 触发首次事件 → 5 指标 FAILED → 立即停训才是 ROI 最高选择
- **延长训练是反直觉的"修复"**——但项目方在第一次事件后没止损，反而投入更多时间

## 🎯 决策记录

| 日期 | 决策 | 行动 |
|---|---|---|
| 2026-10-16 | INCIDENT v9 collapse | 修复 1-4 实施（数据/训练/阈值）|
| 2026-10-17 09:10 | ckpt-1400 落盘（70% 训练）| 训练继续到 2000 |
| 2026-10-17 19:37 | Kernel OOM 杀训练 | watchdog 也死 |
| 2026-10-17 21:00 | ckpt-1400 sanity 跑完 | 5 指标 FAILED（direction_acc 2%）|
| 2026-10-17 21:10 | **K1 决策** | **回退 v7r1115 + 不再追 v9** |
| 2026-10-17 21:15 | K1 确认执行 | 线上 stage1（从未切到 v9）+ Ollama v7r1115 健康 |

## 📦 相关文件

- `docs/INCIDENT_v9_collapse_2026-10-16.md`（**第一次事件报告**）
- `docs/INCIDENT_v9_ckpt1400_extended_training_collapse_2026-10-17.md`（**第二次事件报告**）
- `docs/v9_TRAINING_FINAL_STATUS_2026-10-17.md`（**本报告**）
- `training/eval/metrics_checkpoint-1400.json`（5 指标灾难性结果）
- `train/output/qwen3-14b-tdx-v9/`（2.2 GB 训练资产）
- `~/ObsidianVaults/tdx-data-feed/06-运维与监控/vault 升级评估待决策.md`（vault 备忘——v9 失败属于"复杂任务"决策）

## 🎯 一句话结论

**v9 训练彻底失败，K1 已自然执行**——线上保持 `vpnl_huber_stage1`（LightGBM 主路径），Ollama v7r1115 健康。**不应再投入时间在 v9**——下次 LoRA 训练前必须先解决 7 项根因（降 LR、focal loss、rank 8、4bit 推理、Ollama 治理等）。
