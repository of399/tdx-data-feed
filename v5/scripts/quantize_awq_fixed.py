#!/usr/bin/env python3
"""
quantize_awq_fixed.py · vLLM AWQ 量化（修复 OOM 版）

⚠️  DEPRECATED (2026-09-25): vllm-venv 已移除（B 方案止损）
此脚本保留仅为历史参考. 如需量化请用主 venv + 24GB+ 单卡.

原 quantize_awq.py 在 GPU 0 (16GB) 加载 28GB Qwen3-14B + AWQ 校准峰值 → CUDA OOM
修复尝试（最终未能跑通）:
  1. PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True（避免碎片）
  2. CUDA_VISIBLE_DEVICES=1（强制 GPU 1，避开 X11 占用的 GPU 0）
  3. 减小 max_calib_samples (32→16) + max_calib_seq_len (512→256)
  4. zero_point=True（INT4 而不是 FP16 校准，省一半显存）

输出：/home/jiuben/models/Qwen3-14B-AWQ（约 7GB）

用法（vllm-venv 存在时）:
    vllm-venv/bin/python v5/scripts/quantize_awq_fixed.py
"""

from __future__ import annotations

import os

# 必须在 import torch 之前设置
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"  # 强制 GPU 1（GPU 0 被 X11 占）

import sys
import time
from pathlib import Path

MODEL = "/home/jiuben/models/Qwen3-14B"
OUTPUT = "/home/jiuben/models/Qwen3-14B-AWQ"


def main():
    if Path(OUTPUT).exists():
        print(f"⚠ AWQ model already exists at {OUTPUT}")
        return 0

    print("=== AWQ 量化（修复 OOM 版）===")
    print(f"  model: {MODEL}")
    print(f"  output: {OUTPUT}")
    print("  CUDA_VISIBLE_DEVICES=1 (GPU 1 空闲)")
    print("  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True")
    print()

    import torch
    from awq import AutoAWQForCausalLM
    from transformers import AutoTokenizer

    print("[1/3] 加载 tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)

    print("[2/3] 加载 model（GPU 1）...")
    t0 = time.time()
    model = AutoAWQForCausalLM.from_pretrained(
        MODEL,
        safetensors=True,
        torch_dtype=torch.float16,
        device_map="cuda:0",  # CUDA_VISIBLE_DEVICES=1 后，cuda:0 = GPU 1
    )
    print(f"  加载耗时: {time.time() - t0:.1f}s")
    print(f"  GPU 1 显存: {torch.cuda.memory_allocated() / 1e9:.1f}GB / 16GB")

    print("[3/3] AWQ 量化（INT4）...")
    t0 = time.time()
    quant_config = {
        "zero_point": True,  # INT4（不是 FP16）
        "q_group_size": 128,
        "w_bit": 4,
        "version": "GEMM",
    }
    model.quantize(
        tokenizer,
        quant_config=quant_config,
        max_calib_samples=16,  # 从 32 减到 16
        max_calib_seq_len=256,  # 从 512 减到 256
        max_chunk_memory=268435456,  # 256MB（从 512MB 减半）
    )
    print(f"  量化耗时: {time.time() - t0:.1f}s")

    print(f"[4/4] 保存到 {OUTPUT}...")
    model.save_quantized(OUTPUT)
    tokenizer.save_pretrained(OUTPUT)

    print(f"\n✓ 完成 → {OUTPUT}")
    print(
        f"  大小: {sum(p.stat().st_size for p in Path(OUTPUT).rglob('*') if p.is_file()) / 1e9:.1f}GB"
    )
    print()
    print("下一步：")
    print(f"  vllm-venv/bin/vllm serve {OUTPUT} \\")
    print("    --tensor-parallel-size 1 \\")
    print("    --gpu-memory-utilization 0.9 \\")
    print("    --port 8000 --max-model-len 16384")


if __name__ == "__main__":
    sys.exit(main())
