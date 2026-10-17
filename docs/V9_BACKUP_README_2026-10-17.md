# v9 训练目录备份说明

> ⚠️ **备份目的**：v9 训练**失败（mode collapse）**，训练目录被 tdx-clean-logs 移到 Trash。**备份到此处防止 Trash 自动清理**导致训练数据丢失，**用于根因分析 + 未来 v10 训练对比**。

## 📊 备份内容

| 项目 | 来源 | 大小 |
|---|---|---|
| `qwen3-14b-tdx-v9/checkpoint-200/` | Trash | 751 MB |
| `qwen3-14b-tdx-v9/checkpoint-1200/` | Trash | 751 MB |
| `qwen3-14b-tdx-v9/checkpoint-1400/` | Trash | 751 MB |
| `qwen3-14b-tdx-v9/trainer_log.jsonl` | Trash | 24 KB |
| **总计** | | **2.2 GB** |

## 🚨 v9 失败核心事实

- **训练到 1400 step** 后被 L1/L2/L3 sanity 阈值自动停止
- **ckpt-1400 评估**（2026-10-17 20:59）：
  - 方向准确率 = **0.0202**（2.02%，比随机 33% 差 16 倍）
  - Spearman = 0.0000
  - **100% 预测 flat**（mode collapse 确认）
- **训练目录** 2026-10-17 21:11 被 `tdx-clean-logs.service` 移到 Trash

## 🔍 根因（详见 `docs/V9_MODE_COLLAPSE_ROOT_CAUSE_2026-10-17.md`）

1. **enable_thinking 漏关**（v8 关键修复被 v9 漏掉）
2. **lora_rank 翻倍**（8 → 16）+ **alpha 翻倍**（16 → 32）
3. **cutoff_len 减半**（1280 → 512）

## 🛠 v10 训练修复方向

- `enable_thinking: false`
- `lora_rank: 8, lora_alpha: 16`
- `cutoff_len: 1280`
- 加 focal loss（γ=2.0，防 mode collapse）
- 早停检测：ckpt-100 后 direction_acc < 0.20 立即停训

## 📂 备份位置

- **项目内**：`/home/jiuben/tdx-data-feed/models/_archive/qwen3-14b-tdx-v9-failed-2026-10-17/`
- **不在 git**（.gitignore 排除 `models/_archive`）
- **不被 tdx-clean-logs 清理**（不在 train/output/）
- **不被 systemd 干预**（用户目录，无 timer）

## 🔄 恢复方式（如需复现 v9 失败）

```bash
# 把备份目录链接到 train/output
ln -s /home/jiuben/tdx-data-feed/models/_archive/qwen3-14b-tdx-v9-failed-2026-10-17/qwen3-14b-tdx-v9 \
      /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v9

# 然后可重跑 sanity 评估（已修路径 bug）
venv/bin/python training/eval/run_v4_eval.py \
    --ckpt /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v9/checkpoint-1400 \
    --base /home/jiuben/models/Qwen3-14B \
    --quantize-4bit
```

## 📅 备份时间

- **备份触发**：2026-10-17 22:20（用户质疑 v9 训练状态后，立即响应）
- **备份方式**：`cp -r`（非 mv，保留 Trash 原文件）
- **备份时长**：~2 min
- **下次清理建议**：v10 训练成功 + 通过 sanity 后可清理
