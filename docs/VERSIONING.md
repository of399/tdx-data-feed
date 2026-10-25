# 版本治理规则（VERSIONING）— 2026-10-18 制定

> 目的：让未来会话有章可循——版本号怎么开、什么时候开、退休怎么走。
> 来源：v9 mode collapse 复盘（`docs/V9_MODE_COLLAPSE_ROOT_CAUSE_2026-10-17.md`）+ 用户决策（2026-10-18）。

## 一、两条版本线（关键区分，勿混淆）

| 版本线 | 形态 | 作用 | 新版本频率 |
|---|---|---|---|
| **LLM 线** | Qwen3-14B + LoRA（`train/output/qwen3-14b-tdx-vX/`）| **仅评估**（评估脚本已归档 `training/eval/_archive/run_v4_eval.deprecated.py`，方向准确率验证），**不进实盘** | 低——配方实质变化才开 |
| **LightGBM 线** | `data/double_up/d_signal_5d_model_*.txt`（`scripts/predict.py` 的 `MODEL_REGISTRY`）| **实盘主力**（默认 `vpnl_huber_stage1`）| 较高——特征/目标升级即开 |

**铁律**：LLM 线的版本成败**不影响实盘**（实盘走 LightGBM）。评估 LLM 失败≠生产事故。

## 二、版本号规则

1. **命名**：`v{N}` 主版本；**修复版用 `v{N}r`**（resume/repair），不跳新号。
   - 修复属实质变化也不开新主版本；只有 `v{N}r` 再失败、需要推翻配方重设计时才开 `v{N+1}`。
2. **目录隔离**（沿用项目惯例）：每个版本独立 `output_dir`（`train/output/qwen3-14b-tdx-v{N}/`），绝不覆盖旧版本产物。
3. **配置文件**：`training/config/tdx_v{N}.yaml`（在上一版基础上注明每处改动的 diff 与原因）。

## 三、新版本触发器（4 个，缺一不开）

| 触发器 | 判定 | 开新号？ |
|---|---|---|
| **修复失败复盘** | vX 修复后（vXr）仍不达 sanity 阈值 | ✅ 开 vX+1（配方推翻重设计）|
| **数据/特征升级** | 特征维度/数据集实质变化（如 18 维→26 维→21 维精选）| ✅ |
| **基座换代** | Qwen3 → 新基座 | ✅（低频）|
| **目标函数变更** | 分类→回归、标签定义变化 | ✅ |
| 定期累积重训 | 每周/每月新数据 | ❌ **同版本续训**（vX-resume 模式）|

**禁止**：预研式开新版本（"反正以后要用先建一个"）——这是 2026-10-17 被用户纠正过的错误（"配置还没做好就做下一个"）。

## 四、两态约束（任何时刻）

```
活跃版本最多两个:
  [现役]    ── 唯一通过 sanity(ckpt-100 direction_acc ≥ 0.20)且完整训练达标的
  [实验中]  ── 唯一正在训练/修复的

其余 → 退休归档
```

## 五、退休归档流程

1. **触发**：新版本定稿为现役后；或版本确认死亡（如 v9 mode collapse）。
2. **动作**：
   ```bash
   # 训练目录移入归档（防 tdx-clean-logs 误清）
   git mv train/output/qwen3-14b-tdx-vX _archive/  # 若 git 跟踪
   # 或大文件: mv train/output/qwen3-14b-tdx-vX models/_archive/qwen3-14b-tdx-vX-failed-YYYY-MM-DD/
   # 归档 README 记录: 失败原因/根因/配置/评估数据
   ```
3. **保留物**：trainer_log.jsonl + 末次 checkpoint + 根因报告链接（参照 `models/_archive/qwen3-14b-tdx-v9-failed-2026-10-17/README.md` 模式）。
4. **LightGBM 线**同理：`MODEL_REGISTRY` 里非现役、非回滚项的模型文件 → 注释标记退休日期（不删文件，`predict.py --model` 仍可显式回滚）。

## 六、定稿流程（ vXr → 现役 ）

```
1. 配置变更清单写入 yaml 头注释（每处 diff + 原因 + 指向根因报告）
2. ckpt-100 sanity: direction_acc ≥ 0.20 → 继续训练; < 0.20 → 立即停训止损
3. 完整训练(2000 step) + load_best_model_at_end
4. 终评: run_v4_eval 全指标(direction_acc/spearman/cls_acc/top-k)
   ↑ 该脚本已归档 training/eval/_archive/run_v4_eval.deprecated.py（带已知缺陷），
     重开 LLM 线前先评审它或重建评估入口，不要直接执行
5. 达标 → 合并部署(models/Qwen3-14B-tdx-vXr-merged) + 旧现役退休归档
6. 报告 + commit + memory 三件套
```

## 七、当前版本状态（2026-10-22 更新）

| 线 | 版本 | 状态 |
|---|---|---|
| LLM | v8-merged | **现役**（10-01 完整训练，已知可用）|
| LLM | v9 | ❌ 死亡（mode collapse，已归档 `models/_archive/`）|
| LLM | v9r | ❌ 死亡（ckpt-1000 实测 33.3% = 全震荡基线；输出目录已删除，指标留档 `training/eval/metrics_v9r_*.json`）|
| LightGBM | vpnl_huber_stage1 | **现役**（OOS Sp 0.3475，实盘默认）|
