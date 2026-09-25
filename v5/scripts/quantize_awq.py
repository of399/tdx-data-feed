#!/usr/bin/env python3
"""AWQ 量化 Qwen3-14B-tdx-fast-merged → 4-bit。
用法: python quantize_awq.py <src> <dst>

⚠️  DEPRECATED (2026-09-25): vllm-venv 已移除, 2×16GB 硬件 OOM（见 audit 报告）
此脚本保留仅为历史参考. 如需量化, 用 24GB+ 单卡机器 + 主 venv (已装好 awq via pip).

  - 2x RTX 5060 Ti 16GB, device_map="auto" 让 accelerate 自动 TP=2
  - max_calib_samples=32, max_calib_seq_len=512 (节省 GPU 显存)
  - q_group_size=128, w_bit=4, version=GEMV (batch=1 友好)
  - fail-fast: OOM 即抛, 不降 batchsize 重试 (用户明确止损)
"""

import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import torch  # noqa: E402

src = sys.argv[1]
dst = sys.argv[2]

t0 = time.time()

print(f"[{time.strftime('%H:%M:%S')}] src={src}", flush=True)
print(f"[{time.strftime('%H:%M:%S')}] dst={dst}", flush=True)
print(
    f"[{time.strftime('%H:%M:%S')}] torch={torch.__version__} cuda={torch.version.cuda}", flush=True
)
print(f"[{time.strftime('%H:%M:%S')}] devices={torch.cuda.device_count()}", flush=True)
for i in range(torch.cuda.device_count()):
    free, total = torch.cuda.mem_get_info(i)
    print(f"  GPU{i}: {free / 1e9:.1f}GB free / {total / 1e9:.1f}GB total", flush=True)

print(f"[{time.strftime('%H:%M:%S')}] import AutoAWQForCausalLM ...", flush=True)
from awq import AutoAWQForCausalLM  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

print(f"[{time.strftime('%H:%M:%S')}] 加载 model (device_map='auto')...", flush=True)
model = AutoAWQForCausalLM.from_pretrained(
    src,
    device_map="auto",
    dtype=torch.bfloat16,
    safetensors=True,
)

print(f"[{time.strftime('%H:%M:%S')}] 加载 tokenizer ...", flush=True)
tokenizer = AutoTokenizer.from_pretrained(src, trust_remote_code=True)

quant_config = {
    "zero_point": True,
    "q_group_size": 128,
    "w_bit": 4,
    "version": "GEMV",  # 适合 batch=1 推理 + vLLM 兼容
}

print(f"[{time.strftime('%H:%M:%S')}] 量化开始 (本地 32 条样本, seq=512) ...", flush=True)
print(f"[{time.strftime('%H:%M:%S')}]   quant_config: {quant_config}", flush=True)

# 加载本地校准样本（HF 不通, 用本地 JSON）
import json  # noqa: E402

with open("/tmp/awq/calib_samples.json", encoding="utf-8") as f:
    calib_samples = json.load(f)
print(f"[{time.strftime('%H:%M:%S')}] 校准样本数: {len(calib_samples)}", flush=True)

try:
    model.quantize(
        tokenizer,
        quant_config=quant_config,
        calib_data=calib_samples,  # 本地 list[str], 不依赖 HF
        max_calib_samples=32,
        max_calib_seq_len=512,
        n_parallel_calib_samples=1,  # AutoAWQ 的 batch_size 等价参数 (默认 None 自动选)
        max_chunk_memory=536870912,  # 512MB, 最小化校准激活峰值
    )
except torch.cuda.OutOfMemoryError as e:
    print(f"[{time.strftime('%H:%M:%S')}] ✗ OOM during quantize: {e}", flush=True)
    print(f"[{time.strftime('%H:%M:%S')}] 用户止损条件触发: 不降 batchsize 重试", flush=True)
    raise

elapsed = time.time() - t0
print(
    f"[{time.strftime('%H:%M:%S')}] 量化完成 elapsed={elapsed:.0f}s ({elapsed / 60:.1f}min)",
    flush=True,
)

print(f"[{time.strftime('%H:%M:%S')}] 保存到 {dst} ...", flush=True)
model.save_quantized(dst)
tokenizer.save_pretrained(dst)

print(f"[{time.strftime('%H:%M:%S')}] ✓ 完成", flush=True)

# GPU 释放
del model
del tokenizer
torch.cuda.empty_cache()
print(f"[{time.strftime('%H:%M:%S')}] GPU cache 已清空", flush=True)
