#!/usr/bin/env bash
# v5-VE1-M2 · vLLM 启动脚本（Phase 3 主入口）
# ⚠️  DEPRECATED (2026-09-25): vllm-venv 已移除（B 方案止损, OOM 不可行）
# 此脚本保留仅为历史参考. 如需 vLLM serve, 需重新装 vllm (pip install vllm) 或换 24GB+ 卡
# 前提：Ollama 已停（sudo snap stop ollama），两张 RTX 5060 Ti 16GB 可用

set -euo pipefail

VLLM_VENV="/home/jiuben/tdx-data-feed/vllm-venv"   # DEPRECATED, 已删除
MODEL="/home/jiuben/models/Qwen3-14B"
LOG="/tmp/vllm-serve.log"
PORT=8001

echo "=== vLLM 启动配置 ==="
echo "  venv: $VLLM_VENV"
echo "  model: $MODEL"
echo "  log: $LOG"
echo "  port: $PORT"

if [ ! -d "$MODEL" ]; then
    echo "✗ 模型目录不存在: $MODEL"
    exit 1
fi

if ! command -v nvidia-smi &> /dev/null; then
    echo "✗ nvidia-smi 不可用"
    exit 1
fi

echo
echo "=== 当前 GPU 状态 ==="
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader

if pgrep -f "vllm serve" > /dev/null; then
    echo "⚠ vLLM 已在运行（PID: $(pgrep -f 'vllm serve')）"
    echo "  重启：pkill -f 'vllm serve' 再跑本脚本"
    exit 1
fi

echo
echo "=== 启动 vLLM TP=2（28GB / 2卡 = 14GB/卡）==="
cd /home/jiuben/tdx-data-feed
nohup "$VLLM_VENV/bin/vllm" serve "$MODEL" \
    --tensor-parallel-size 2 \
    --dtype bfloat16 \
    --max-model-len 16384 \
    --gpu-memory-utilization 0.85 \
    --max-num-seqs 8 \
    --port "$PORT" \
    --served-model-name qwen3-14b-vllm \
    > "$LOG" 2>&1 &

VLLM_PID=$!
echo "  PID: $VLLM_PID"
echo "  日志: $LOG"
echo

echo "=== 等待 vLLM 启动（最多 120s）==="
for i in {1..60}; do
    if curl -s http://127.0.0.1:$PORT/v1/models > /dev/null 2>&1; then
        echo "✓ vLLM 已就绪（耗时 $((i*2))s）"
        echo
        echo "=== 健康检查 ==="
        curl -s http://127.0.0.1:$PORT/v1/models | python3 -m json.tool | head -20
        echo
        echo "=== Benchmark ==="
        curl -s -X POST http://127.0.0.1:$PORT/v1/chat/completions \
            -H "Content-Type: application/json" \
            -d '{
                "model": "qwen3-14b-vllm",
                "messages": [{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                "max_tokens": 50,
                "temperature": 0
            }' | python3 -m json.tool | head -15
        exit 0
    fi
    sleep 2
done

echo "✗ vLLM 未在 120s 内就绪，查看日志："
echo "  tail -f $LOG"
exit 1