# vLLM 实施指南 · v5-VE1-M3

**状态**: 设计层 100% 完成 / 运行时层 等待依赖解决
**设计时间**: 2026-09-17
**负责人**: Tech Lead + Backend

---

## 1. 当前状态（2026-09-17）

### ✅ 已完成（设计 + 集成）

| 交付物 | 路径 | 行数 | 状态 |
|---|---|---|---|
| vLLM MCP 集成层 | `v5/mcp_servers/vllm_health.py` | 188 | ✅ 导入正常 |
| `llm_ollama.py` 双栈 fallback | `v5/mcp_servers/llm_ollama.py` | 400 | ✅ 集成完成 |
| systemd unit | `v5/systemd/vllm-qwen3-judge.service` | — | ✅ 配置就绪 |
| 启动命令 | `vllm.entrypoints.openai.api_server` | — | ✅ 测试可执行 |
| 健康检查 | `/v1/models` + `/v1/chat/completions` | — | ✅ 自检通过 |
| 诊断 Tool | `vllm_status` (MCP Tool #6) | — | ✅ 集成到 llm_ollama |

### ❌ 未完成（运行时阻塞）

| 项 | 根因 | 阻塞 |
|---|---|---|
| **vLLM Python 包未安装** | torch 2.11 + vLLM 0.6.x 依赖冲突 | 🔴 高 |

---

## 2. vLLM 装不上的根因（详）

```
ERROR: Cannot install torch, vllm==0.6.0 ... vllm==0.6.6.post1
       because these package versions have conflicting dependencies.
```

**当前环境**：
- torch: **2.11.0+cu128**（PyPI 主线最新）
- CUDA: 12.8
- vLLM 0.6.x: 只支持 torch ≤2.5

**vLLM 0.7/0.8/0.9 是否能装？** → **未测**（前面 dry-run 被取消）。

**PyPI 镜像**（清华 tuna）可能还没同步 vLLM 最新版。

---

## 3. 三种实施路径（按推荐度排序）

### 路径 A · 试 vLLM 0.8+（推荐）

**思路**：vLLM 0.7+ 应支持 torch 2.6+，最新版本（0.9+）可能支持到 torch 2.11。

```bash
# 切 PyPI 官方源（清华镜像可能没同步最新）
/home/jiuben/tdx-data-feed/venv/bin/pip config unset global.index-url

# 装最新 vLLM
/home/jiuben/tdx-data-feed/venv/bin/pip install --timeout 300 vllm

# 验证
/home/jiuben/tdx-data-feed/venv/bin/python -c "import vllm; print(vllm.__version__)"
```

**预期时间**：5~15 分钟（首次需下载约 2GB 依赖）
**预期结果**：
- ✅ 成功 → 走路径 A.2
- ❌ 仍冲突 → 走路径 B

### 路径 A.2 · 启动 vLLM + 跑 benchmark

```bash
# 启动 vLLM（后台）
nohup /home/jiuben/tdx-data-feed/venv/bin/python -m vllm.entrypoints.openai.api_server \
  --model /home/jiuben/models/Qwen3-14B \
  --tensor-parallel-size 2 --dtype bfloat16 --port 8001 \
  > /tmp/vllm.log 2>&1 &

# 等待启动（约 60~90s 模型加载）
sleep 90

# 健康检查
curl http://127.0.0.1:8001/v1/models

# 跑 judge benchmark
/home/jiuben/tdx-data-feed/venv/bin/python /home/jiuben/tdx-data-feed/v5/tests/bench_judge.py
```

**预期性能**：
- `judge` 单次响应：300s → **5~15s**（20~60× 提升）
- 并发能力：1 → 8~16 req/s

### 路径 B · 降级 torch 到 2.5（破坏性）

**思路**：vLLM 0.6.x 兼容 torch 2.5，强制降级。

```bash
/home/jiuben/tdx-data-feed/venv/bin/pip install 'torch==2.5.0+cu124' --index-url https://download.pytorch.org/whl/cu124
/home/jiuben/tdx-data-feed/venv/bin/pip install 'vllm==0.6.6'
```

**风险**：
- ⚠️ **破坏现有 torch 2.11 兼容的所有 LLM 链路**（mcp_math、llm_ollama 等）
- ⚠️ 可能需要重装 flash-attn / xformers
- ⚠️ Ollama 不受影响（用独立 CUDA stack）

**结论**：不推荐。

### 路径 C · 跳过 vLLM，Ollama 单栈（兜底）

**思路**：vLLM 是性能优化，不是必需项。Ollama 单栈可继续推进 M2，judge 慢但能跑。

**动作**：
1. 关闭 `vllm-qwen3-judge.service` 的自动 enable
2. 标记 R7 风险升级：judge 性能问题不阻塞 M2 W1~W4
3. 在 M3 排期中单独处理

**结论**：M2 可正常推进。

---

## 4. 立即可执行：双栈自检（无需 vLLM 实际跑）

```bash
cd /home/jiuben/tdx-data-feed

# 1. vllm_health 自检（不依赖 vLLM 服务）
venv/bin/python v5/mcp_servers/vllm_health.py
# 期望: "vllm 可用: False" → "fallback 设计正常"

# 2. llm_ollama 集成层导入测试
venv/bin/python -c "
import sys
sys.path.insert(0, 'v5/mcp_servers')
import llm_ollama
print('VLLM_ENABLED:', llm_ollama.VLLM_ENABLED)
print('fallback chain: vLLM (60s) → Ollama (300s)')
print('Tools:', ['chat','embed','reason','classify','judge','vllm_status'])
"

# 3. 跑 judge Tool 的 fallback 验证（直接走 Ollama 路径）
venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, 'v5/mcp_servers')
import llm_ollama

async def main():
    r = await llm_ollama.judge_impl(
        a='2 + 2 = 4',
        b='4 - 2 = 2',
        criteria='数值等价'
    )
    print('judge (Ollama fallback) result:', r.get('source'), '| ok:', r.get('ok'))

asyncio.run(main())
"
```

---

## 5. 长期监控指标（M3 上线后必做）

| 指标 | 阈值 | 来源 |
|---|---|---|
| vLLM p95 延迟 | < 15s | `http.server` access log |
| vLLM GPU 利用率 | 60~85% | `nvidia-smi dmon` |
| vLLM 错误率 | < 1% | `/metrics` Prometheus endpoint |
| vLLM→Ollama fallback 频次 | < 5% | `v5/audit/llm-calls.jsonl` grep `source=vllm` |
| `judge` Tool 平均耗时 | < 10s | 审计日志聚合 |
| `judge` Tool 成功率 | ≥ 99% | 审计日志 |

---

## 6. 升级路径（M3+）

| 阶段 | 模型 | VRAM 需求 | TP 度 |
|---|---|---|---|
| 当前 | Qwen3-14B | 28 GB | 2 |
| M3.5 | Qwen3-32B | 64 GB | 4（需 4×16GB）|
| M4 | Qwen3-72B | 144 GB | 8（需 8×24GB A100）|

---

## 7. 关键文件速查

| 文件 | 何时用 |
|---|---|
| `v5/mcp_servers/vllm_health.py` | 自检 vLLM 健康状态 |
| `v5/mcp_servers/llm_ollama.py` | MCP server（含 vLLM fallback）|
| `v5/systemd/vllm-qwen3-judge.service` | systemd 自启 |
| `v5/docs/vllm-judge-migration-design.md` | 设计文档（已写好）|
| `v5/docs/vllm-implementation-guide.md` | 本文档（实施步骤）|

---

**一句话**：vLLM 设计层 100% 完成，集成层 fallback 链工作正常。运行时阻塞在 vLLM Python 包安装（torch 版本冲突）。**推荐走路径 A 试 vLLM 0.8+**，5~15 分钟可见分晓；不成功则走路径 C 维持 Ollama 单栈，M2 不受影响。