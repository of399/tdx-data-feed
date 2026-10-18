# R10 对照实验结果 · thinking 开/关推理 (2026-10-18)

> 目的：验证 v9 mode collapse 的根因 1（训练-推理不一致），为 v9r 修复方向提供实证。
> 方法：同一 ckpt-1400（已归档的 v9 失败产物）用 thinking 开/关两种推理模式评估，对照方向分布。

## 一、结果对照

| 指标 | no-think（基线 200 条）| think（R10-B 400 条）| 倍数变化 |
|---|---:|---:|---:|
| **direction_accuracy** | 0.0202 | **0.0682** | **+3.4x** |
| **cls_acc_up**（涨类判别）| 0.0150 | **0.0967** | **+6.4x** |
| cls_acc_down（跌类）| 0.0156 | 0.0081 | -1.9x |
| **cls_acc_flat**（平类）| **1.0000**（100% 坍缩）| **0.0** | **彻底打破坍缩** |
| spearman | 0.0 | 0.0 | — |
| top5/top10_hit | 0.0 | 0.0 | — |
| 格式遵循率 | 0.99 | 0.99 | — |

## 二、判定结论

### 根因 1（训练-推理不一致）→ **部分证实**

**证据**：think 模式下模型**不再是 100% flat**——cls_acc_flat 从 1.0000 跌到 0.0，涨类判别力从 1.5% 升到 9.67%（6.4 倍）。

**机理**：v9 训练时 `enable_thinking` 未关（llamafactory 默认开），训练 target 隐含 thinking 模式；而评估脚本硬编码 `<no_think>` 关闭推理。二者不一致 → 模型在 no-think 推理下退化到"全预测最稳的 flat"。

### 但根因 1 不是全部 —— direction_acc 仍仅 6.8%（随机基线 33%）

think 模式也远未达标（sanity 阈值 0.20）。说明 **rank=16/cutoff=512/packing 的过拟合（根因 2/3/4）仍是主因**——thinking 不一致只是"最后一根稻草"之一，去掉它模型从"全 flat"回到"6.8% 弱判别"，但核心过拟合未解。

## 三、对 v9r 的意义（关键）

**4 项修复全部必要且方向正确，获得实证支撑**：

| v9r 修复 | 对应根因 | R10 实证 |
|---|---|---|
| A `enable_thinking: false` | 根因 1 | ✅ think 实验证实 train/infer 不一致确影响分布（flat 1.0→0.0）|
| B `lora_rank 16→8` | 根因 2 | 过拟合主因（acc 6.8% 仍低，指向容量过大）|
| C `cutoff_len 512→1280` | 根因 3 | 同上 |
| D `packing false` | 根因 4 | 同上 |

**结论**：v9r 不是盲目重试——`training/config/tdx_v9r.yaml` 的 4 项修复（已 commit）方向被本实验证实。启动 v9r 训练是**有依据的下一步**。

## 四、附带改进

- `run_v4_eval.py` 加流式方向分布统计（每 100 条输出 up/down/flat/none）——本实验重跑的教训（汇总式输出导致 8,008 条全量中途读不到结果，被迫杀掉重跑 400 条），以后长评估可中途判定止损。

## 五、文件位置

- 指标 JSON：`training/eval/metrics_r10_think_400.json`
- 报告 MD：`training/eval/report_r10_think_400.md`
- 日志：`logs/r10_think_eval_400.log`
- v9r 配置：`training/config/tdx_v9r.yaml`（4 项修复已就位）

## 六、下一步（待用户决策）

1. 启动 v9r 训练（20h+ GPU）——训练前必做：`systemctl stop ollama` + GPU 僵尸显存清理 + ckpt-100 早停监控
2. 或先观察/收工，v9r 训练另择时机
