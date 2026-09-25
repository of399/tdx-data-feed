# vLLM 部署探索记录（2026-09-21）

## 目标

把 Qwen3-14B-tdx-fast-merged 模型量化到 AWQ-int4（约 7GB），用 vLLM serve 替代 Ollama 兜底链。

## 现状

- 硬件：2x RTX 5060 Ti 16GB（双卡 sm_120, Blackwell）
- 模型：Qwen3-14B-tdx-fast-merged（28GB f16 safetensors, 8 文件）
- Python：3.14.4（极新，多数 wheel 还在用 cp312）
- torch：2.11.0+cu128

## 探索步骤

### 步骤 1：vllm-venv + vLLM 0.29.0 成功

```bash
python3 -m venv /home/jiuben/tdx-data-feed/vllm-venv
/home/jiuben/tdx-data-feed/vllm-venv/bin/pip install vllm==0.29.0
```

- vLLM 0.29.0 abi3 cu129 wheel 在 Python 3.14 上可直接装且可 import
- vLLM 内置 `vllm/model_executor/layers/quantization/auto_awq.py` 支持加载 AWQ 模型

### 步骤 2：选择 AWQ 量化工具

| 工具 | 装上 | Py3.14 import | 备注 |
|---|---|---|---|
| llmcompressor 0.13.0（vLLM 官方接管） | 是 | 否 TypeError | Pydantic typing 不兼容，此路径堵死 |
| AutoAWQ 0.2.9 | 是 | 是 | AutoAWQ 已 deprecated, 官方推荐 llmcompressor, 但 0.2.9 仍可用 |
| auto-gptq | 否 | — | 依赖旧 torch 装不上 |
| bitsandbytes | 是 | — | NF4 量化, vLLM 0.29 不直接支持 |

结论：用 AutoAWQ 0.2.9（虽 deprecated 但能跑）。

### 步骤 3：修复 AutoAWQ API 调用

```
TypeError: AwqQuantizer.__init__() got an unexpected keyword argument 'batch_size'
```

修复：AutoAWQ 用 `n_parallel_calib_samples` 控批次，默认 None。改为 `n_parallel_calib_samples=1` + `max_chunk_memory=512MB`。

### 步骤 4：本地校准数据

```
'timed out' thrown while requesting HEAD https://huggingface.co/datasets/mit-han-lab/pile-val-backup/resolve/main/README.md
```

- `huggingface.co` 在本机不通（IPv6 SYN-SENT 卡 8s+）
- `hf-mirror.com` 通, 但没有 pile-val-backup 数据集（HTTP 404）

修复：放弃 HF pileval, 本地构造 32 条多样化校准样本（数学/代码/对话/中文 各 8 条, 存入临时 JSON）。

### 步骤 5：执行量化 - OOM

```
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 680.00 MiB.
GPU 0 has a total capacity of 15.51 GiB of which 421.81 MiB is free.
Including non-PyTorch memory, this process has 13.65 GiB memory in use.
Of the allocated memory 12.87 GiB is allocated by PyTorch,
and 641.03 MiB is reserved by PyTorch but unallocated.
```

触发点：`pseudo_quantize_tensor(fc.weight.data)`, 在校准阶段 `_compute_best_scale` 内部分配 680MB 临时张量失败。

硬性结论：2 张 16GB 量化 14B f16 不可行
- 模型权重：14GB/卡 ×2 = 28GB
- 校准 activation 峰值：0.6-1GB/卡
- AutoAWQ 临时张量：680MB+ × 40 层 = 约 50GB 临时（碎片化）
- 总需求 >= 32GB vs 可用 32GB = 顶到上限

遵循止损原则（OOM 立即回退, 不降 batchsize 重试）。

## 资产沉淀

| 文件 | 状态 | 用途 |
|---|---|---|
| ~~`/home/jiuben/tdx-data-feed/vllm-venv/` (8.4GB)~~ | **已删除 (2026-09-25)** | "完整套装 90min" 清理步骤释放磁盘 |
| `v5/scripts/quantize-awq.sh` (3KB) | 永久 | 量化 launcher, 带超时 + 进度轮询 + 资源监控 |
| `v5/scripts/quantize_awq.py` (3KB) | 永久 | 量化 Python 主体（已含本地校准数据 + n_parallel_calib_samples） |
| `models/awq-quantize-2026-09-21.failed.log` (14KB) | 永久 | 失败证据, 含 OOM 栈 |
| `models/Qwen3-14B-tdx-fast-merged/` | 保留 | LoRA merge 后产物 |
| `models/Qwen3-14B-tdx-fast-merged-f16.gguf` (28GB) | 保留 | A 方案 ollama 用的 GGUF |

## 决策

接受 vLLM AWQ 量化硬件不可行, 回退到 Ollama 兜底链（D2.4 + A 方案验证）。

理由：
- Ollama `qwen3-14b-tdx-fast:14b`（Ollama 自带 Q4_K_M 量化）已就绪
- math-sympy-http service unit 已加 `DEFAULT_JUDGE_MODEL=qwen3-14b-tdx-fast:14b` env
- W10 测试 113/113 通过
- 链路已验证从 prometheus 到 alertmanager 到 webhook 全通
- vLLM 不是链路必需项（仅性能优化）

## 解锁路径

未来如需启用 vLLM：

1. 量化模型 + 24GB+ GPU：找单卡 24GB+ 机器量化 AWQ, 下载 7GB AWQ, 本机 vLLM serve（最高 ROI）
2. 更小模型：Qwen3-7B-AWQ（约 4GB/卡）, LoRA 重训
3. 硬件升级：2x RTX 4090 24GB 或 2x A100 80GB
4. 云端 vLLM：本地跑 Ollama, 云端跑 vLLM judge

## 关键工程教训

| 教训 | 说明 |
|---|---|
| Py3.14 + AutoAWQ 兼容性栈还在路上 | AutoAWQ 已 deprecated, llmcompressor 在 Py3.14 上 Pydantic typing 不兼容 |
| 量化 f16 14B 需 >= 32GB GPU 总显存 | 28GB 权重 + 校准峰值 + 临时张量 = 32GB 起步 |
| HF pile-val-backup 在国内镜像上没有 | fallback 到本地校准样本时务必准备充分 |
| AutoAWQ API 与新 transformers 不匹配 | 必须看 AwqQuantizer.__init__ 真实签名, 不能照搬 README |
| abandoned library 仍可短期使用 | AutoAWQ deprecated 但 0.2.9 在 Py3.14 上仍可装可跑 |