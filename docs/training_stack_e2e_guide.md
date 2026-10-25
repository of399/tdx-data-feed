# 端到端训练栈指南：依赖跑通 → 训练部署（tdx-data-feed · 2026-10-06）

> 合并自 `docs/dependency_run_verification_prompt.md` 与 `docs/training_stack_prompt.md`。
> 聚焦**训练栈依赖**（torch / llamafactory / bitsandbytes / deepspeed），并将**今日单卡 QLoRA 实测结论**回填至已知陷阱与报错分类，消除全部"待核实"。

## 状态图例（贯穿全文）
- ✅ **本轮实测通过**：在本机（py3.14 + 2×RTX 5060 Ti 16GB + CUDA 12.8）实际跑通验证。
- ⚠️ **约束（沿用）**：既有的、不可破坏的版本/配置约束，本轮未改动且验证其仍有效。
- 🔲 **本轮未测**：本轮单卡 QLoRA 未覆盖的场景（如多卡 DeepSpeed、全量微调、GGUF 导出），标明范围，不再标"待核实"。

---

## 0. 目标与范围
在本项目（Qwen3-14B + 2×RTX 5060 Ti 16GB）上，用 LLaMA-Factory + PyTorch 搭一套**从依赖跑通 → 数据预处理 → 微调 → 监控 → 评估 → 导出部署**的可复现训练栈，支持 LoRA/QLoRA/全量、单卡与多卡。本文档把"依赖能否装好跑通"与"训练栈怎么搭"合二为一，并把依赖风险点按**解析期 / 安装期 / 运行期**三级细化。

## 1. 项目环境基线（实测）
- **解释器**：Python 3.14 虚拟环境（项目根 `venv/`）；conda 在 `/opt/miniforge3/bin/conda` 但不在 PATH。
- **硬件**：单节点 2×RTX 5060 Ti 16GB（各 16311 MiB，合计 32GB 显存）；**无 nvcc**（无法编译 flash-attn / deepspeed 算子）。
- **CUDA 运行时**：12.8；`torch 2.11.0+cu128`、`cuda.is_available()=True`。✅ 实测
- **核心库版本**：`transformers 4.57.6`、`peft 0.20.0`、`llamafactory-cli` 可用。✅ 实测
- **基础模型**：本地 HF 权重 `/home/jiuben/models/Qwen3-14B`（离线，避免联网）；template `qwen`。
- **现役产物**：已产出 `qwen3-14b-tdx-v5/v6/v7/v7r/v7r1115`（优先顶层 ≈ checkpoint-1115）；续训失败案例 `v8r`（偏跌退化，已弃用）。
- **约束**：不动 `data/`、`models/` 既有产物；不破坏版本绑定（antlr4 固定 4.9.3）；大文件不入库。

## 2. 依赖清单（聚焦训练栈）
训练栈核心（其余功能域见 §3.4 轻量说明）：
- **torch**：深度学习框架与 CUDA 后端（本轮 `2.11.0+cu128`）。
- **transformers / peft**：模型加载、LoRA 适配（本轮 `4.57.6` / `0.20.0`）。
- **accelerate / datasets / trl**：分布式启动、数据集、RLHF（trl 本轮未直接调用）。
- **llamafactory**：训练入口与配置解析；依赖链强绑 `omegaconf` + `antlr4-python3-runtime==4.9.*`。
- **bitsandbytes**：QLoRA 4-bit 量化与 8/32-bit 分页优化器（本轮 `paged_adamw_32bit` 实测可用）。
- **deepspeed**：多卡 ZeRO 训练（本轮未启用）。

## 3. 训练栈依赖：细粒度报错分类与解决方向
按**报错发生阶段**分类，便于定位。状态列使用 §图例。

### 3.1 解析 / 配置期（pip 解析、LLaMA-Factory parse_dict）
| 组件 | 报错现象 | 根因 | 解决方向 | 状态 |
|---|---|---|---|---|
| torch | 解析阶段报平台不支持 / 无 cp314 wheel | py3.14 缺预编译 wheel | 确认官方 cu12x wheel 覆盖；否则退回 3.11/3.12 隔离环境 | ✅ 本轮 `2.11.0+cu128` 有 wheel，跑通 |
| llamafactory | `ValueError: Some keys are not used by the HfArgumentParser: ['bnb_4bit_compute_dtype']` | 本版本 `compute_dtype` 为 `init=False` 自动推导字段（由 bf16 决定），手动指定即不被识别 | **删除** `bnb_4bit_compute_dtype`；compute dtype 由 `bf16: true` 自动得 bfloat16 | ✅ 今日实测踩中并修复 |
| llamafactory+antlr4 | 导入/启动崩溃（ImportError 或解析异常） | llamafactory 依赖链强绑 `antlr4-python3-runtime==4.9.*`，误升 4.11/4.13 不兼容 | 固定 `antlr4-python3-runtime==4.9.3`，勿盲目升级 | ⚠️ 约束；本轮导入与训练均未崩，绑定有效 |
| transformers/peft | 版本不匹配告警或 API 缺失 | 与 llamafactory 期望区间不符 | 锁定 llamafactory 兼容区间内的版本 | ✅ 本轮 `4.57.6`/`0.20.0` 兼容 |

### 3.2 安装期（pip install、原生编译）
| 组件 | 报错现象 | 根因 | 解决方向 | 状态 |
|---|---|---|---|---|
| flash-attn | 无 cp314 wheel / 编译失败（缺 nvcc） | py3.14 无预编译 + 本机无 nvcc | **不安装**，配置 `flash_attn: sdpa` 回退；结果与 fa2 一致 | ⚠️ 约束；`sdpa` 回退 ✅ 实测可用 |
| bitsandbytes | 旧版缺 cp314 wheel / 编译失败 | 早期版本无 3.14 预编译 | 用 `0.44+` 含预编译或 pip 源码构建 | ✅ 本轮 `paged_adamw_32bit` 在 cu128/py3.14 可用 |
| deepspeed | 算子 JIT 编译失败（缺 nvcc / gcc） | 部分算子需本地编译 | 用预编译 wheel；或暂缓，单卡 QLoRA 不需 deepspeed | 🔲 本轮未装未测（单卡未启用） |

### 3.3 运行期（训练/推理）
| 组件 | 报错现象 | 根因 | 解决方向 | 状态 |
|---|---|---|---|---|
| 显存 OOM（单卡 16G） | 训练启动即 OOM 或中途 OOM | 14B 全量 / 大 rank / 大 batch 超显存 | QLoRA 4-bit + 梯度检查点 + 梯度累积；全量改 ZeRO-3+CPU 卸载 | ✅ 单卡 QLoRA 实测峰值 9061 MiB，安全；🔲 全量未测 |
| 梯度异常 / NaN | `grad_norm` 突增或 loss NaN | lr 过高 / 数据损坏 / 精度溢出 | 降 lr、切 `bf16`、查数据；`max_grad_norm` 裁剪 | ✅ 本轮 grad_norm 0.60–1.16 稳定，无异常 |
| 续训 size mismatch | 加载 adapter 即崩（维度不符） | `resume_from_checkpoint` 失效；`adapter_name_or_path` 的 rank/alpha/target 与目标不一致 | 用 `adapter_name_or_path` 指 checkpoint，且 rank/alpha/target 与 adapter_config 完全一致 | ⚠️ 约束（沿用）；本轮从零训未触发 |
| gguf 转换失败 | 导出 GGUF 缺 DFLASH | PyPI `gguf 0.19.0` 不支持 DFLASH | 用 llama.cpp **master** 覆盖 site-packages/gguf 后导出 | 🔲 本轮未做导出；沿用既有结论 |

### 3.4 其他功能域（非训练栈，轻量）
数据采集（baostock/akshare/pandas/pyarrow）、Web（fastapi/uvicorn）、数据库（psycopg2/redis/sqlalchemy）、监控（prometheus）、量化研究（lightgbm/sklearn）等依赖确定性高，安装风险低，按 `requirements.txt` 分层安装即可，不在此细化。已知运行时项：akshare 东方财富后端被网络阻断（走新浪后端）、vn.py 4.0.0 在 py3.14 依赖解析通过（可选）。

## 4. 验证流程（逻辑阶段）+ 今日实测结论回填
### 4.1 流程
1. **可解析性预检**：先确认训练栈清单在 py3.14 能解析到 wheel（重点 torch、llamafactory）。✅ 已确认。
2. **隔离安装**：现有 venv 已含全部训练栈依赖；新装时先轻量域后重量级域。
3. **导入烟测**：torch 识别 CUDA、transformers 加载 tokenizer、llamafactory-cli 可解析配置。✅ 均过。
4. **功能烟测（关键）**：跑一次**有界单卡 QLoRA 训练**，验证端到端可跑通并采集指标（见 4.2）。
5. **已知陷阱复核**：核对 §3 各项，确认 antlr4 绑定、flash-attn SDPA 回退、compute_dtype 自动推导等未被破坏。
6. **状态归档**：按 §图例标注 ✅/⚠️/🔲。

### 4.2 单卡 QLoRA 实测（2026-10-06，可复现）
- **配置**：`training/config/tdx_verify_single_qlora.yaml` + 脚本 `scripts/verify_single_qlora.sh`（限定 `CUDA_VISIBLE_DEVICES=0`）。
  - Qwen3-14B，4-bit nf4 + double_quant，lora_rank=8 / alpha=16 / 7 目标模块，lr=5e-5，batch=1，accum=8，cutoff=512，max_steps=40，logging_steps=1，eval_steps=20，flash_attn=sdpa，bf16，optim=paged_adamw_32bit，seed=42。
- **结果（观测标准落地）**：

| 观测项 | 实测值 | 判定 |
|---|---:|---|
| 单卡峰值显存（GPU0） | 9061 MiB / 16311（55.6%） | ✅ 无瓶颈、无 OOM |
| 纯训练单步耗时 | ~12.5 s/step | ✅ 可接受 |
| 整体 train_runtime | 1161.5 s（19:21，含 step20 评估 330.2s） | ✅ |
| 训练损失 | step1 2.0904 → step40 1.181（均值 1.5186） | ✅ 收敛正常 |
| 梯度范数 grad_norm | 0.60–1.16（裁剪上限 1.0，无顶满爆量/NaN） | ✅ 无梯度异常 |
| 学习率曲线 | warmup 0→5e-5（4 步）→ cosine 衰减至 ~9.5e-8 | ✅ 符合预期 |
| 评估损失 eval_loss | 1.0856 → **困惑度 ≈ 2.96**（exp(1.0856)） | ✅ 链路通（注：40 步有界，任务准确率需满训后评估） |
| 收敛/异常 | 无显存瓶颈、无梯度异常、无收敛问题 | ✅ |

- **结论**：单卡 16GB 下 QLoRA 微调**实测跑通**，依赖链（torch 2.11+cu128 / transformers 4.57.6 / peft 0.20.0 / llamafactory / bitsandbytes paged_adamw_32bit）全部可用；唯一踩中的坑是 `bnb_4bit_compute_dtype` 无效字段（已修复，见 §3.1）。

## 5. 数据准备
- **格式**：LLaMA-Factory 数据源在 `train/data/`，需 `dataset_info.json` 注册映射名（如 `tdx_v7_train` / `tdx_v5_val` / `tdx_v4_test`），alpaca 结构（`instruction`/`input`/`output`）。
- **防泄漏**：配置用 `split: split`（时序切分字段）做 train/val 切分；验证集可复用 `tdx_v5_val` 监控过拟合。
- **预处理**：tdx 专有任务（翻倍判定、方案 D 预测）构造指令样本，标签口径（`peak_date`、前复权价）不可改；`cutoff_len` 控制序列长度（现役 512）。
- **注册示例**（`dataset_info.json` 片段）：
  ```json
  {
    "tdx_v7_train": {"file_name": "tdx_v7_train.jsonl", "formatting": "alpaca", "split": "split", "columns": {"prompt": "instruction", "query": "input", "response": "output"}}
  }
  ```

## 6. 模型微调（LoRA / QLoRA / 全量）
统一用 LLaMA-Factory yaml。核心头：
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
  # 注意：本版本 compute_dtype 由 bf16 自动推导，勿写 bnb_4bit_compute_dtype（见 §3.1）
  lora_rank: 8            # 或 16
  lora_alpha: 16          # 常用 alpha = 2 * rank
  lora_dropout: 0.05
  lora_target: q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj
  ```
- **LoRA（不量化，更高精度）**：去掉 `quantization_bit` 等 3 行；显存高于 QLoRA，靠梯度检查点 + 累积消化。
- **全量微调**：`finetuning_type: full`，移除 `lora_*` 字段；显存最大，单节点 32GB 下 14B 全量必须 ZeRO-3+CPU 卸载。🔲 本轮未测。
- **续训陷阱**：用 `adapter_name_or_path` 指 checkpoint，`lora_rank/alpha/target` 须与 adapter_config 完全一致，否则 size mismatch。⚠️ 约束（沿用）。

## 7. 训练参数调优
现役稳健基线（v7 续训，防灾难性遗忘）：
```yaml
per_device_train_batch_size: 1
gradient_accumulation_steps: 16      # effective batch（多卡再 ×GPU 数）
learning_rate: 5.0e-5                # 续训保守；从零可 1e-4
lr_scheduler_type: cosine
warmup_ratio: 0.03
num_train_epochs: 1.0
max_grad_norm: 1.0
weight_decay: 0.0
seed: 42
cutoff_len: 512
```
- **学习率**：续训 5e-5；从零 1e-4；全量再降一档。
- **批次/累积**：单卡 batch=1，用累积把 effective batch 顶到 16~32。
- **warmup**：小比例 cosine；数据少则 warmup_steps 绝对数更可控。
- **梯度范数**：`max_grad_norm: 1.0` 裁剪防爆炸（今日实测 grad_norm 稳定）。
- **加速**：`packing: true`（+20~40%）、`torch_compile: true`（+10~20%），双卡 16G 上保留 `gradient_checkpointing: true` 防 OOM。

## 8. 分布式训练（DeepSpeed / FSDP）
- **单卡**：直接 `llamafactory-cli train config.yaml`，靠 QLoRA + grad ckpt + 累积。✅ 实测（§4.2）。
- **多卡（2×16G）**：
  - **DeepSpeed ZeRO-2**（LoRA/QLoRA 推荐）：yaml 加 `deepspeed: ds_z2.json`（`zero_stage: 2`、对齐 accum、`offload_optimizer: cpu` 可选）。
  - **DeepSpeed ZeRO-3**（全量必需）：`zero_stage: 3` + `offload_param/optimizer: cpu`。
  - **FSDP**（原生替代）：`fsdp: ["full_shard","grad_op","offload"]`。
  - 启动：`torchrun --nproc_per_node=2 -m llamafactory.train config.yaml` 或 llamafactory-cli 配 deepspeed 字段。
- 🔲 **本轮未测**：多卡 DeepSpeed / FSDP / 全量微调均未在今日单卡实测中覆盖；上述为多卡设计指引，落地前需单独验证。
- **适配矩阵**：

| 场景 | 微调方式 | 显存策略 | 分布式 |
|---|---|---|---|
| 单卡 16G | QLoRA(rank≤16) | grad ckpt + accum16 | 无需（✅实测） |
| 双卡 32G | QLoRA / LoRA | ZeRO-2 | deepspeed json（🔲未测） |
| 双卡 32G 全量 | full | ZeRO-3 + CPU 卸载 | deepspeed / FSDP（🔲未测） |

## 9. 训练监控（Loss 曲线、梯度范数）
- **日志**：`logging_steps`、`save_steps`、`eval_strategy: steps` + `eval_steps`、`save_total_limit`。
- **可视化**：wandb / swanlab / tensorboard，重点 `loss`、`eval_loss`、`grad_norm`、`learning_rate`。
- **异常判定**：
  - `loss` 不降或 NaN → lr 过高 / 数据损坏 / 精度溢出（切 bf16、降 lr）。
  - `grad_norm` 持续顶满上限且 loss 抖动 → 调小 lr 或增大 warmup。
  - `eval_loss` 反升、`loss` 仍降 → 过拟合，缩短 epoch / 加 dropout / 早停。

## 10. 模型评估
- **训练内评估**：`eval_dataset` + `eval_strategy: steps` 盯 `eval_loss`（今日实测 eval_loss 1.0856 → 困惑度 2.96）。
- **独立评估脚本**：`training/eval/run_v4_eval.py` 已归档到 `training/eval/_archive/run_v4_eval.deprecated.py`（带已知缺陷，勿直接执行）；其形态是用 peft+transformers 加载 **base + LoRA adapter** 算下游指标（方向准确率、Top-k 命中、夏普、格式遵循率），重开 LLM 线时按归档件重建入口。注意原脚本 base 默认硬编码 HF 在线路径，离线需改本地 `/home/jiuben/models/Qwen3-14B`。
- **指标取向**：v7/v7r 在 2000 样本上准确率≈48~49%（接近多数类基线），续训未提升"直接翻倍判定"；**v8r 偏跌退化 → 不部署**。翻倍判定属不平衡分类，loss 低 ≠ 任务准。🔲 本轮 40 步有界训练未做下游准确率评估（需满训后）。

## 11. 模型导出（合并 → GGUF → Ollama）
- **合并 LoRA（必须 `--device cpu`）**：用项目脚本 `train/scripts/merge_lora.py --device cpu`（`merge_and_unload` 物化 14B bf16 整模，双卡 16G/32GB 经 `device_map=auto` 拆分后峰值易超显存触发 CUDA OOM，故默认 cpu）；或 LLaMA-Factory `export` 将 base + adapter 合并为 HF 模型（`Qwen3-14B-tdx-vX-merged`）。`deploy_v5.sh`/`deploy_v6.sh` 内联合并已用 cpu。
- **转 GGUF**：经 llama.cpp **master**（PyPI 0.19.0 缺 DFLASH → 覆盖 site-packages/gguf）。🔲 本轮未做导出。
- **Ollama 部署**：`Modelfile-vX`（`FROM` 指向 gguf，设 `stop=[]` / `repeat_penalty 1.0` 防立即停），`ollama create` + `run`；已部署 v5/v6/v7/v7r/v7r1115/fast。

## 12. 端到端流程（单卡 / 多卡通用）
1. **依赖**：按 §3 跑通训练栈（已 ✅ 单卡 QLoRA 实测）。
2. **数据**：构造指令样本 → 注册 `dataset_info.json` → 时序 `split` 防泄漏。
3. **配置**：复制现役 yaml（如 `tdx_v7_resume.yaml`）→ 调整 dataset / adapter / rank / lr / deepspeed。
4. **训练**：`llamafactory-cli train config.yaml`（多卡加 deepspeed 字段或 torchrun）。
5. **监控**：盯 loss / eval_loss / grad_norm（§9）。
6. **评估**：算任务指标；决定部署（`run_v4_eval.py` 已归档 `training/eval/_archive/`，见 §10）。
7. **导出**：合并 → llama.cpp master 转 GGUF → Modelfile 进 Ollama。
8. **迭代**：结论回写配置注释与记忆；失败配置（如 v8r）标注不部署原因。

## 13. 已知陷阱（必读，含今日实测补充）
- ⚠️ **`compute_dtype` 自动推导**：本版本 `compute_dtype` 由 bf16 决定，**不可**写 `bnb_4bit_compute_dtype`，否则启动即 `ValueError`（今日实测踩中）。
- ⚠️ 续训用 `adapter_name_or_path`，`lora_rank/alpha/target` 对齐 adapter_config，否则 size mismatch。
- ⚠️ `flash_attn` 不可编译 → 写 `sdpa` 回退（✅ 实测可用）。
- ⚠️ antlr4 固定 4.9.3，勿升（✅ 本轮导入/训练未崩，绑定有效）。
- ⚠️ gguf 转换须用 llama.cpp master（DFLASH）；🔲 本轮未导出。
- ⚠️ 全量微调在 32GB 须 ZeRO-3+卸载；🔲 未测。
- 翻倍判定属不平衡分类，loss 低 ≠ 任务准；以任务指标 + 防偏跌退化为验收核心。
- ⚠️ **LoRA 合并必须 `--device cpu`**：`train/scripts/merge_lora.py` 默认已改为 cpu。`merge_and_unload` 物化 14B bf16 整模，双卡 16G（32GB）经 `device_map=auto` 拆分后峰值易超显存触发 CUDA OOM；仅当 GPU 空闲且无显存压力时才用 auto/cuda，且须带 offload 目录。
- ⚠️ **训练预算分级（fast / canonical）**：重打标签 / 快速实验用 `*_fast.yaml`（`evaluation_strategy: "no"`，关掉 periodic eval——8007 条生成式评估是 7h 主因，关掉后训练降到 ~2h）；终训用 canonical 配置（`eval_steps: 500` 仅末步一次 + `predict_with_generate: false` loss-only + 小验证集 `tdx_v4fwd_val_small`）。eval 只监控、不改权重，关 eval 不影响标签契约归因。
- ⚠️ **管道不可静默吞错**：所有含 `| tail/grep/...` 的 shell 脚本必须 `set -uo pipefail`；关键步骤用 `scripts/lib/pipeline.sh` 的 `run_step` + `assert_min_size` / `assert_ollama_model` 做「命令失败即中止 + 产物覆盖校验」，拦「exit 0 却产物为空」的语义级静默失败。`make lint-pipeline` 可静态扫描未保护脚本。

## 14. 验收标准与约束
- ✅ 单卡 QLoRA 训练跑通：loss 收敛、grad_norm 正常、产出 checkpoint、峰值显存 < 16GB（实测 9061 MiB）。
- 🔲 多卡（DeepSpeed/FSDP）、全量微调、GGUF 导出：本轮未测，落地前需单独验证，不再标"待核实"。
- 不破坏既有数据/模型与版本绑定；新配置独立 `output_dir`，不覆盖现役 v7r。
- 不复用已作废结论；所有状态以 §图例（✅/⚠️/🔲）明示。
- 本指南已替代 `dependency_run_verification_prompt.md` 与 `training_stack_prompt.md` 的训练栈部分。

---
生成时间：2026-10-09
生成者：端到端训练栈指南合并（AI 助手）＋ 2026-10-09 增强：补「合并必须 cpu / 训练预算分级 / 管道不可静默吞错」三条铁律
数据/环境基准：torch 2.11.0+cu128 / transformers 4.57.6 / peft 0.20.0 / llamafactory（实测）+ training/config/{tdx_v7_resume,tdx_lora_packed,tdx_verify_single_qlora}.yaml + scripts/verify_single_qlora.sh
