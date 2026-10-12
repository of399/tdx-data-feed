# 模型训练栈构建提示（LLaMA-Factory + PyTorch · tdx-data-feed · 2026-10-06）

> ⚠️ **已合并**：本文档已与 `dependency_run_verification_prompt.md` 合并为端到端指南 `docs/training_stack_e2e_guide.md`（依赖部分聚焦 torch/llamafactory/bitsandbytes/deepspeed、回填实测结论、消除"待核实"）。请以合并版为准。

> 用途：作为执行"构建完整模型训练栈"任务的指令/提示。覆盖环境配置、依赖安装、数据准备、微调（LoRA/QLoRA/全量）、参数调优、分布式（DeepSpeed/FSDP）、监控、评估与导出、单卡/多卡适配、端到端流程。字段名均取自本项目现役 `training/config/*.yaml`，可直接套用。

## 0. 任务目标
在本项目（Qwen3-14B + 2×RTX 5060 Ti 16GB）上，用 LLaMA-Factory + PyTorch 搭一套**从数据预处理到推理部署**的可复现训练栈，支持 LoRA/QLoRA/全量三种微调方式，支持单卡与多卡，并对训练过程（Loss、梯度范数）具备可观测性，最终产出可经 Ollama 部署的合并模型（GGUF）。

## 1. 项目环境基线（务必先对齐）
- **解释器**：Python 3.14 虚拟环境（项目根 `venv/`）；conda 在 `/opt/miniforge3/bin/conda` 但不在 PATH。
- **硬件**：单节点 2×RTX 5060 Ti 16GB（合计 32GB 显存）；无 nvcc（无法编译 flash-attn）。
- **基础模型**：本地 HF 权重 `/home/jiuben/models/Qwen3-14B`（避免联网）；template 用 `qwen`。
- **现役产物**：已产出 `qwen3-14b-tdx-v5/v6/v7/v7r/v7r1115`，优先顶层 ≈ `checkpoint-1115`；续训失败案例 `v8r`（偏跌退化，已弃用）。
- **约束**：不动 `data/`、`models/` 既有产物；不破坏版本绑定（antlr4 固定 4.9.3）；结论标置信度，"未实测"标"待核实"。

## 2. 环境配置与依赖安装
- **依赖清单**：依赖安装与高风险项（torch CUDA、llamafactory↔antlr4↔omegaconf、bitsandbytes/deepspeed、flash-attn、gguf DFLASH）见 `docs/dependency_run_verification_prompt.md`，本任务在其"跑通"结论之上进行。
- **关键绑定**：`antlr4-python3-runtime==4.9.3`（误升 4.11/4.13 会让 llamafactory 崩）；`flash_attn` 不可编译 → 配置里写 `flash_attn: sdpa`（LLaMA-Factory 自动回退，结果与 fa2 一致）。
- **精度缺省**：本机用 `bf16: true / fp16: false`；QLoRA 计算 dtype 用 `bfloat16`。
- **入口**：训练用 `llamafactory-cli train <config.yaml>`（项目用 `venv/bin/llamafactory-cli`）。

## 3. 数据准备
- **格式**：LLaMA-Factory 数据源在 `train/data/`，需 `dataset_info.json` 注册映射名（如 `tdx_v7_train` / `tdx_v5_val` / `tdx_v4_test`）。样本用 alpaca 或 sharegpt 结构（`instruction`/`input`/`output` 或 `messages`）。
- **防泄漏**：配置里用 `split: split`（时序切分字段）做 train/val 切分，避免未来数据泄漏到验证集；验证集可复用 `tdx_v5_val` 监控过拟合。
- **预处理要点**：
  - 对 tdx 专有任务（翻倍判定、方案 D 预测）构造指令样本，注意标签口径（`peak_date` 计算、前复权价）不可改。
  - `cutoff_len` 控制最大序列长度（现役 512）；长样本需截断或打包。
- **注册示例（dataset_info.json 片段）**：
  ```json
  {
    "tdx_v7_train": {"file_name": "tdx_v7_train.jsonl", "formatting": "alpaca", "split": "split", "columns": {"prompt": "instruction", "query": "input", "response": "output"}}
  }
  ```

## 4. 模型微调（LoRA / QLoRA / 全量）
统一用 LLaMA-Factory 的 yaml 配置驱动，核心字段取自现役配置：

```yaml
model_name_or_path: /home/jiuben/models/Qwen3-14B
adapter_name_or_path: null            # 从零训；续训填 checkpoint 路径
dataset: tdx_v7_train
eval_dataset: tdx_v5_val
dataset_dir: /home/jiuben/tdx-data-feed/train/data
template: qwen
output_dir: /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-vX
overwrite_output_dir: true
```

- **QLoRA（默认，低显存）**：
  ```yaml
  finetuning_type: lora
  quantization_bit: 4
  quantization_type: nf4
  double_quantization: true
  bnb_4bit_compute_dtype: bfloat16
  lora_rank: 8            # 或 16
  lora_alpha: 16          # 常用 alpha = 2 * rank
  lora_dropout: 0.05
  lora_target: q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj
  ```
- **LoRA（不量化，更高精度）**：去掉 `quantization_bit` 等 4 行，其余 LoRA 字段相同；显存占用高于 QLoRA，需靠梯度检查点 + 梯度累积消化。
- **全量微调**：`finetuning_type: full`，移除所有 `lora_*` 字段；显存需求最大，单节点 32GB 下 14B 全量必须走 DeepSpeed ZeRO-3 + 梯度/优化器卸载，否则 OOM。
- **续训陷阱**：本项目 `resume_from_checkpoint` 失效 → 用 `adapter_name_or_path` 指向目标 checkpoint 重新 `do_train`，且 `lora_rank/alpha/target` 必须与目标 adapter_config.json **完全一致**，否则加载即崩（size mismatch）。

## 5. 训练参数调优
现役稳健基线（v7 续训，防灾难性遗忘）：

```yaml
per_device_train_batch_size: 1
gradient_accumulation_steps: 16      # effective batch = 16（双卡则再 ×GPU 数）
learning_rate: 5.0e-5                # 续训保守；从零可 1e-4
lr_scheduler_type: cosine
warmup_ratio: 0.03
num_train_epochs: 1.0
max_grad_norm: 1.0
weight_decay: 0.0
seed: 42
cutoff_len: 512
```

- **学习率**：续训/防遗忘用 5e-5；从零或新域可 1e-4；全量微调再降一档。
- **批次与累积**：单卡 batch=1 受限显存，用 `gradient_accumulation_steps` 把 effective batch 顶到 16~32；多卡时 effective = per_device × GPU数 × accum。
- **warmup**：小比例（0.03~0.05）cosine 即可；数据量小则 warmup_steps 绝对数更可控。
- **梯度范数**：`max_grad_norm: 1.0` 梯度裁剪防爆炸；监控 `grad_norm` 是否突增。
- **加速（可选，新训练启用）**：`packing: true`（多样本拼接 +20~40%）、`torch_compile: true`（+10~20%），双卡 16G 上**保留** `gradient_checkpointing: true` 防 OOM。

## 6. 分布式训练（DeepSpeed / FSDP）
- **单卡**：直接 `llamafactory-cli train config.yaml`，靠 QLoRA + grad ckpt + 梯度累积消化显存。
- **多卡（本项目 2×16G）**：
  - **DeepSpeed ZeRO-2**（LoRA/QLoRA 推荐，省显存同时保留优化器状态）：在 yaml 加 `deepspeed: ds_z2.json`，json 设 `zero_stage: 2`、`gradient_accumulation_steps` 与之对齐、`offload_optimizer` 可选 `cpu` 进一步省显存。
  - **DeepSpeed ZeRO-3**（全量微调必需）：`zero_stage: 3` + `offload_param: cpu` + `offload_optimizer: cpu`，代价是通信/卸载开销。
  - **FSDP**（PyTorch 原生替代）：yaml 用 `fsdp: ["full_shard","grad_op","offload"]` 等，适合不引入 DeepSpeed 的场景。
  - 启动方式：可用 `torchrun --nproc_per_node=2 -m llamafactory.train config.yaml` 或 LLaMA-Factory 自带的 accelerate/deepspeed 启动；本项目多卡用 `venv/bin/llamafactory-cli train` 配合 deepspeed 字段即可。
- **适配矩阵**：

| 场景 | 微调方式 | 显存策略 | 分布式 |
|---|---|---|---|
| 单卡 16G | QLoRA(rank≤16) | grad ckpt + accum16 | 无需 |
| 双卡 32G | QLoRA / LoRA | ZeRO-2 | deepspeed json |
| 双卡 32G 全量 | full | ZeRO-3 + CPU 卸载 | deepspeed json / FSDP |

## 7. 训练监控（Loss 曲线、梯度范数）
- **日志**：`logging_steps: 20`、`save_steps: 200`、`eval_strategy: steps` + `eval_steps: 200`、`save_total_limit: 3`。
- **可视化**：挂载 wandb / swanlab / tensorboard，重点看 `loss`、`eval_loss`、`grad_norm`、`learning_rate`。
- **异常判定**：
  - `loss` 不降或 NaN → 学习率过高 / 数据损坏 / 混合精度溢出（切 `bf16`、降 lr）。
  - `grad_norm` 持续顶到 `max_grad_norm` 上限且 loss 抖动 → 调小 lr 或增大 warmup。
  - `eval_loss` 反升、`loss` 继续降 → 过拟合，缩短 epoch 或加 `lora_dropout` / 早停。
  - 单卡→多卡 loss 数值应有界一致，若漂移过大检查数据切分与 seed。

## 8. 模型评估
- **训练内评估**：`eval_dataset` + `eval_strategy: steps` 持续盯 `eval_loss`。
- **独立评估脚本**：项目已有 `training/eval/run_v4_eval.py`，用 `peft + transformers` 加载 **base + LoRA adapter**（指定 checkpoint 路径）做下游任务准确率评估。
- **指标取向**：本项目经验——2000 样本稳健验证 v7/v7r 准确率≈48~49%（接近多数类基线），续训未提升"直接翻倍判定"；**v8r 续训出现偏跌退化 → 不部署**。评估结论须结合任务（翻倍判定属不平衡分类），不只看 loss。

## 9. 模型导出（合并 → GGUF → Ollama）
- **合并 LoRA**：用 LLaMA-Factory `export` 将 base + adapter 合并为 HF 模型（如 `Qwen3-14B-tdx-vX-merged`）。
- **转 GGUF**：经 llama.cpp **master** 版本（PyPI 0.19.0 缺 DFLASH 会失败）→ 覆盖 site-packages/gguf 后导出 `*-merged-f16.gguf`。
- **Ollama 部署**：写 `Modelfile-vX`（`FROM` 指向 gguf，设 `stop=[]` / `repeat_penalty 1.0` 防立即停），`ollama create` + `ollama run`；本项目已部署 v5/v6/v7/v7r/v7r1115/fast。

## 10. 端到端流程（单卡 / 多卡通用）
1. **数据**：构造指令样本 → 注册 `dataset_info.json` → 时序 `split` 防泄漏。
2. **配置**：复制现役 yaml（如 `tdx_v7_resume.yaml`）→ 调整 dataset / adapter / rank / lr / deepspeed。
3. **训练**：`llamafactory-cli train config.yaml`（多卡加 deepspeed 字段或 torchrun）。
4. **监控**：wandb/tensorboard 盯 loss / eval_loss / grad_norm，按 §7 判定。
5. **评估**：`run_v4_eval.py` 加载 base+adapter 算任务指标；决定是否部署。
6. **导出**：合并 → llama.cpp master 转 GGUF → Modelfile 进 Ollama。
7. **迭代**：结论回写 `training/config/*.yaml` 注释与记忆；失败配置（如 v8r）标注不部署原因。

## 11. 已知陷阱（必读）
- 续训必须用 `adapter_name_or_path` 指 checkpoint，`lora_rank/alpha/target` 对齐 adapter_config，否则 size mismatch 崩溃。
- `flash_attn` 不可编译 → 写 `sdpa`，不要强行 fa2/fa3。
- gguf 转换须用 llama.cpp master（DFLASH），否则失败。
- antlr4 固定 4.9.3，勿升。
- 全量微调在 32GB 上必须 ZeRO-3 + 卸载，否则 OOM。
- 翻倍判定属不平衡分类，loss 低 ≠ 任务准；以任务指标 + 防偏跌退化为验收核心。

## 12. 验收标准与约束
- 单卡与多卡各至少跑通一条 LoRA/QLoRA 训练，loss 收敛、grad_norm 正常、产出 checkpoint。
- 导出链路（合并→GGUF→Ollama）可端到端跑通，推理可加载。
- 不破坏既有数据/模型与版本绑定；新配置独立 `output_dir`，不覆盖现役 v7r。
- 结论标置信度；未实测项（如 FSDP、ZeRO-3 全量）标"待核实"。
- 复用 `docs/dependency_run_verification_prompt.md` 的依赖结论，不在本任务内重复解决依赖。

---
生成时间：2026-10-06
生成者：训练栈提示编写（AI 助手）
数据/环境基准：training/config/{tdx_v7_resume,tdx_lora_packed}.yaml + 项目记忆（v7r 优先 / v8r 弃用 / flash-attn SDPA / gguf DFLASH）
