# v9 训练模式坍缩事件报告

**事件 ID**：INC-2026-10-16-v9-collapse
**发生时间**：2026-10-16 20:41（ckpt-200 sanity 完成）
**报告时间**：2026-10-16 21:05
**影响范围**：v9 训练（`train/output/qwen3-14b-tdx-v9/`）
**严重度**：🟡 中（训练可继续；产物未部署；非破坏）

---

## 1. 现象记录

### 1.1 sanity 5 指标（ckpt-200）

| 指标 | 值 | 阈值 | 通过 |
|---|---:|---:|:---:|
| direction_accuracy | 0.3015 | 0.33 | ❌ |
| spearman | 0.0000 | 0.33 | ❌ |
| cls_acc_up | 0.4403 | 0.33 | ✅ |
| cls_acc_down | **0.0000** | 0.33 | ❌ |
| cls_acc_flat | **1.0000** | 0.33 | ✅ |

**判定**：3/5 不达标 → sanity FAILED。

### 1.2 loss 趋势（trainer_log.jsonl）

```
step  20: loss=11.67   (随机 init)
step  40: loss=8.76
step  60: loss=2.42
step  80: loss=0.25    ← 骤降 35x（典型 mode collapse 早期症状）
step 100: loss=0.22
step 200: loss=0.20
step 240: loss=0.18    ← 训练停止前最后一行
```

### 1.3 5 指标内部矛盾

- `direction_accuracy = 0.30`（接近 3 分类随机 33%）
- `cls_acc_flat = 1.0`（**所有平样本预测正确**）
- `cls_acc_down = 0.0`（**所有跌样本预测错误**）
- `cls_acc_up = 0.44`（**部分涨样本预测正确**）

**最可能解释**：模型预测分布呈"绝大多数预测为平 + 少量预测为涨 + 从不预测为跌"模式——典型 mode collapse 特征。

### 1.4 训练过程

- 多次 watchdog 触发 → 训练重启频繁（trainer_log.jsonl 多行乱序）
- 实际 wallclock 1.5h，但仅跑到 step 280+（因重启损失 ~70% 进度）
- ckpt-200 于 18:28 落地，ckpt-200_presanity 于 18:56 落地

---

## 2. 根因分析

### 🚨 根因 1（高置信）：数据时序极端不平衡

```
训练数据 2009-2015:  12 条 (0.02%)
训练数据 2016+:    64,044 条 (99.98%)
```

**64k 训练样本几乎全部来自 2016 年之后**——模型学到的市场结构、价格行为、波动特征均集中于 2016-2024 年。

**3 集分布漂移**：

| 集 | up | down | flat | 推论 |
|---|---:|---:|---:|---|
| train (2016-2024 主力) | 33.8% | 39.8% | 26.4% | 略偏跌（震荡市特征）|
| val | 15.7% | **56.0%** | 28.4% | 严重偏跌（val 时段可能熊市）|
| test (2026-08+) | **47.7%** | 20.3% | 32.0% | 严重偏涨（2026 牛市）|

**结论**：train/val/test **三个集的标签分布**显著不同——这是 mode collapse 的**首要温床**（模型学到"震荡"占 26.4% 的先验，到 test 涨 47.7% 时无法泛化，坍缩为"震荡"默认输出）。

### 🚨 根因 2（高置信）：训练步数过少 + 早停触发坍缩

- 当前在 **step 280/6006 = 4.6%** 时已 sanity
- loss 从 step 60 → 80 骤降 35x（典型"学到了捷径"模式）
- 模型在**前 4% 训练就收敛到 0.18**——可能学习到"震荡"作为默认输出的快捷方式

### ⚠️ 根因 3（中置信）：3 分类 CE 损失对少类不友好

- 3 类分布差异不大（33.8/39.8/26.4%），但 val 集 56% 跌 强化了模型的"跌"先验
- 训练时无 class_weight / focal loss 调节
- 当 test 集分布剧烈变化（涨 47.7%）时，模型"懒于"重新学习，跌类彻底学不会

### ⚠️ 根因 4（中置信）：训练 / 评估 prompt 模板一致但特征分布漂移

- instruction 模板**完全一致**（"你是专业的A股量化分析师..."）
- output 格式**完全一致**（"方向：涨/跌/震荡"）
- 但 input 行情数据**时间段不同**（train 早期 2009 周边，test 2026）
- 模型"看到近期 20 个交易日" 时的模式识别能力**未在训练中充分学习**

---

## 3. 修复建议

### 修复 1（必要 · 改数据）：重做 train/val/test 时序分层切分

**当前**：train 取 2009-2024 + val 取某段 + test 取 2026——分布漂移严重。

**建议**：
- 重新切分：保证 3 集**标签分布** + **时序分布**都接近
- 例如：train = 2016-2023 滚动 + val = 2024 滚动 + test = 2025-2026
- 或用 **stratified split** 保证 3 集 up/down/flat 比例均接近 33/33/33

**实施**：
```bash
# 重写 v4fwd 数据切分脚本(本会话不实施,留待下次数据治理)
# 1. 加载 raw v4fwd 数据 → 按时间排序
# 2. 按时序 7:2:1 切分(train:val:test)
# 3. 在每集内做 stratified sample 保证 3 类分布一致
# 4. 重新生成 train/val/test.jsonl + 更新 dataset_info.json
```

### 修复 2（强烈建议 · 改损失）：focal loss / class_weight 平衡

**当前**：llamafactory 默认 CE loss，无 class_weight 调节。

**建议**：
- 在 llamafactory 配置中加 `loss_type: focal_loss`（如果支持）
- 或在数据预处理中**重采样**让 3 类严格 1:1:1
- 或用 `weighted_sampler`（llamafactory 0.9.5 是否支持待查）

**实施**：
```yaml
# tdx_v9_from_scratch.yaml
loss_type: focal_loss    # 如果 llamafactory 0.9.5 支持
focal_alpha: [0.33, 0.33, 0.34]  # 3 类权重
focal_gamma: 2.0
```

### 修复 3（建议 · 改训练）：延长训练 + 加大 batch + 调整 LR

**当前**：max_steps=600（原 6006 epoch=3），effective_batch=16（DDP 2 卡），lr=1e-4 cosine。

**建议**：
- max_steps 改为 2000（覆盖更多 epoch）
- 有效 batch_size 提到 32（gradient_accumulation=16 + per_device=1 不变，eval_steps 调整）
- LR 降到 5e-5（避免早 step 模式坍缩）
- warmup_ratio 提到 0.05（5% 步数 warmup）
- 加 `load_best_model_at_end: true` + `metric_for_best_model: eval_loss`（让 val 选最优）

### 修复 4（可选 · 加固）：sanity 阈值分级

**当前**：5 指标均要 ≥ 0.33 才 PASS——过于严格（实际 3 分类随机 33%）。

**建议**：
- **L1 sanity**（ckpt-200）：核心方向判别 acc ≥ 0.40 + cls_acc 各类 ≥ 0.25（宽容）
- **L2 sanity**（ckpt-400/600）：acc ≥ 0.50 + cls_acc 各类 ≥ 0.35（标准）
- **L3 final**（训练结束）：OOS Sp ≥ 0.10 + 5 指标全过（严格）

---

## 4. 已实施的修改（事件响应）

### 4.1 `training/config/tdx_v9_from_scratch.yaml`

**修改**：加 `max_steps: 600`（替代 3 epoch 6006 step）。

**原因**：当前训练在 4.6% 进度已 collapse，再训到 6006 浪费时间。**600 step 跑 3 次 sanity（200/400/600）能在 ~3-4h 内完成验证**，决策更快。

```yaml
# L49-53 改动
num_train_epochs: 3.0
max_steps: 600                                   # 方案 B 2026-10-16 救火：ckpt-200 sanity 5 指标 FAILED
                                                   # 缩短训练到 600 step（原 6006 = 3 epoch 需 32-50h）
                                                   # 目标 3 次 sanity（200/400/600）验证坍缩是否持续
                                                   # 与 num_train_epochs 互斥：llamafactory 0.9.5 优先 max_steps
learning_rate: 1.0e-4
lr_scheduler_type: cosine
```

### 4.2 `training/eval/run_v4_eval.py`（之前的方案 B 改动）

**修改**：8bit 推理路径用 `device_map="auto"` + `max_memory={0:'15GB', 1:'15GB'}` 双卡拆分。

**原因**：14B 8bit = 14GB 拆 2 卡 = 7GB/张 + buffer 8GB/张 < 16GB/张。

### 4.3 重启训练

- 杀原训练 PID 675445/675446（wallclock 1.5h 但 step 仅 280+）
- 重启带 `max_steps=600` 训练（PID 676822）
- 启 watchdog 保活（PID 676779）

---

## 5. 监控计划

| 阶段 | 触发 | 监控指标 | 行动 |
|---|---|---|---|
| **T0+1h** | ckpt-400 落地 | 5 指标 (re-eval) | 若仍坍缩 → 立即停训 + 排查 |
| **T0+2h** | ckpt-600 落地 | 5 指标 (re-eval) | 训练结束 |
| **T0+3h** | 训练自然结束 | OOS Sp + 4 模型对比 | 决策 v9 部署 / 回退 |

**自动 sanity 由 watchdog 触发**（项目"修正"逻辑：sanity 失败仅记录不杀训练；ckpt-400/600 落地自动跑）。

---

## 6. 决策点

- **若 ckpt-400 sanity 通过**（5 指标达标）：继续训练到 600，OOS 评估后决策部署
- **若 ckpt-400 sanity 仍 FAILED**：立即停训，**优先实施修复 1（重做数据切分）** + 修复 2（focal loss），再决定是否重训
- **若 ckpt-600 sanity 通过但 OOS Sp 差**：模型可能过拟合训练 prompt，需更多数据 + 修复 4（sanity 分级）

---

## 7. 附录

### 7.1 关键数据文件

| 文件 | 行数 | up% | down% | flat% |
|---|---:|---:|---:|---:|
| `data/v4fwd/train.jsonl` | 64,056 | 33.8 | 39.8 | 26.4 |
| `data/v4fwd/val.jsonl` | 8,007 | 15.7 | 56.0 | 28.4 |
| `data/v4fwd/test.jsonl` | 4,576 | 47.7 | 20.3 | 32.0 |

### 7.2 关键时间线

| 时间 | 事件 |
|---|---|
| 10-12 23:05 | 项目"修正"：watchdog 改"仅记录不杀" |
| 10-16 18:28 | ckpt-200 落地 |
| 10-16 18:56 | ckpt-200_presanity 落地 |
| 10-16 20:10 | run_v4_eval.py 加 5 指标（方案 B）|
| 10-16 20:41 | sanity 跑完，5 指标 FAILED |
| 10-16 20:55 | 8bit 双卡推理成功（14B 拆 2 卡 7GB/张）|
| 10-16 21:00 | yaml 加 max_steps=600 |
| 10-16 21:05 | 训练重启，PID 676822 |

### 7.3 待办

- [ ] 等 ckpt-400 自动 sanity（~1h 后）
- [ ] 等 ckpt-600 自动 sanity（~2h 后）
- [ ] 训练结束后 OOS Sp 评估
- [ ] 决策：v9 部署 / 回退 / 重训
- [ ] 修复 1（数据切分）— 留待下次数据治理
- [ ] 修复 2（focal loss）— 评估 llamafactory 0.9.5 支持度
- [ ] 修复 4（sanity 分级）— 写 L1/L2/L3 配置
