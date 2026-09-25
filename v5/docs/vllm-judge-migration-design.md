# judge 工具性能优化设计文档 · vLLM Tensor‑Parallel 迁移方案

> **本文档定位**：针对 `mcp-llm-ollama` 的 `judge` 工具在双 RTX 5060 Ti 16GB 上 300s 仍超时的根因分析，提出基于 vLLM tensor‑parallel 的长期优化方案。
>
> **基线**：v5‑VE1‑M2 路线图 §12 + 当前 `judge` 工具实测（连续 3 次 120s/300s 超时失败）
>
> **目标版本**：v5‑VE1‑M3 · judge 工具延迟降至 5~15s · 并发 ≥16 · 与 chat/embed 兼容共存
>
> **关联文档**：`v5/README-llm-ollama.md` · `evolution-v5-ve1-math-m2-roadmap.md`

---

## §1 · 方案概述

### §1.1 一句话总结

把 `judge` 工具（及 reason 工具）从 **Ollama layer‑split Q4_K_M** 迁移到 **vLLM tensor‑parallel BF16/AWQ**，利用真正的张量并行 + Continuous Batching + PagedAttention，把 judge 单次延迟从 **>300s 降到 5~15s**，并发从 **1 提升到 16+**。

### §1.2 三个核心决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **Ollama 与 vLLM 双栈共存**（不替换）| Ollama 继续承担 qwen3:14b chat（单卡 16GB 够用）+ nomic‑embed；vLLM 接管 judge/reason（双卡 TP=2）|
| D2 | **judge 用 BF16 而非 Q4_K_M 量化** | 双卡 TP=2 后 14B BF16 = 14GB/GPU，16GB 显存够用；BF16 比 Q4 准确率高 ~5~8 pp，judge 准确性更重要 |
| D3 | **judge 与 classify 共享同一 vLLM 实例**（不是独立部署） | vLLM 内部请求调度远比 Ollama 灵活；同实例多 Tool 共享 KV cache 预热 |

---

## §2 · 当前 judge 性能瓶颈分析

### §2.1 实测基线（2026‑09‑17）

| 指标 | 值 | 来源 |
|---|---|---|
| judge 平均延迟 | **>300s**（全部超时） | `v5/audit/llm-calls.jsonl` 3 次失败 |
| judge 成功样本数 | **0** | 全部超时 |
| chat 平均延迟 | 76~95s（同样 qwen3:14b） | 同次测试 |
| 并发能力 | **1 请求/时刻** | `OLLAMA_NUM_PARALLEL=1` |
| GPU 0 利用率 | 100% | `nvidia-smi` |
| GPU 1 利用率 | 100% | `nvidia-smi` |
| qwen3:14b VRAM | 1.2 GB（Ollama 自报） / 实际跨卡 ~15 GB | Ollama API vs `nvidia-smi` 矛盾 |

### §2.2 五大根因

#### 根因 1：Ollama 默认 layer‑split（pipeline parallel），非 tensor parallel

- **机制**：Ollama 0.33.3 在模型无法单卡放下时按 **layer 数拆分**——GPU 0 跑前 N 层，GPU 1 跑后 M 层，激活值跨卡传递。
- **问题**：这是 **同步流水线**——token 必须流过所有层才能生成下一个 token。两个 GPU 间每 token 一次 PCIe/NVLink 同步。
- **影响**：双卡加速比趋近 1（有时反而比单卡慢）；`nvidia-smi` 100% 但 GPU 利用率是"等待 + 同步"。

#### 根因 2：Q4_K_M 量化准确率损失 + 长 prompt 拖慢生成

- judge 系统 prompt（criteria 定义 + JSON schema 描述）≈ 600 token + 用户两段 LaTeX 输入 ≈ 200~500 token
- Q4_K_M 在长 prompt 上准确率比 BF16 低 **5~8 pp**——LLM 需要"想更久"才敢给 verdict
- JSON mode（`format: "json"`）约束解码，限制采样空间，但 token 生成速度仍受单 token 计算耗时影响

#### 根因 3：无 continuous batching

- `OLLAMA_NUM_PARALLEL=1`：每个请求必须等前一个完成
- judge 工具实际响应 ≈ 120s 时，前端发 16 个 judge 请求要排队 32 分钟
- batch=1 浪费 GPU 计算（14B 模型单次推理 GPU 计算密度低）

#### 根因 4：跨卡 PCIe 通信瓶颈

- RTX 5060 Ti 16GB 是消费级卡，**无 NVLink**
- 跨卡通信走 PCIe 5.0 x16 = ~64 GB/s 双向，但 layer‑split 要传激活值（per token）：1 个 token × 14B hidden=5120 floats = 20 KB，看似小但**每 token 一次同步**
- 长 prompt + 长生成 = 数千次跨卡同步 → PCIe 成为瓶颈

#### 根因 5：KV cache 内存压力

- qwen3:14b 在 32k ctx 下 KV cache ≈ 6~10 GB
- Ollama 默认未做 PagedAttention，KV cache 预分配连续内存 → 高 ctx 时常触发重计算或 swap

### §2.3 为什么 chat 慢但能成功 / judge 慢到超时

| 维度 | chat | judge |
|---|---|---|
| 输入长度 | 短（<50 token）| **长**（system prompt 600 + LaTeX 200~500）|
| 输出长度 | 短（<20 token）| **长**（JSON verdict + rationale 200~500 token）|
| 总 token 数 | ~70 | **~1000+** |
| 跨卡同步次数 | ~70 次 | **~1000+ 次** |
| 同步开销占比 | 可忽略 | **主导** |

**结论**：chat 用 qwen3:14b 的 76~95s 慢是 Ollama layer‑split 的固有延迟；judge 因 token 总数 ×14 倍，300s 仍超时。

---

## §3 · vLLM Tensor‑Parallel 方案设计

### §3.1 总体架构：Ollama + vLLM 双栈共存

```
┌─────────────────────────────────────────────────────────────────────┐
│                    mcp-llm-ollama（MCP server）                      │
│                                                                     │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│   │  chat    │ │  embed   │ │  reason  │ │ classify │ │  judge   ││
│   └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘│
└────────┼────────────┼────────────┼────────────┼────────────┼──────┘
         │            │            │            │            │
   qwen3:14b    nomic-embed    deepseek-r1    qwen3:14b    qwen3:14b
   (chat)        (embed)      (reason)       (classify)    (judge)
         │            │            │            │            │
   ┌─────▼─────┐       │      ┌─────▼─────┐      │     ┌──────▼──────┐
   │  Ollama   │       │      │  vLLM     │      │     │   vLLM      │
   │ (单卡)    │       │      │ TP=2     │      │     │   TP=2      │
   │ qwen3:14b │       │      │ deepseek │      │     │   qwen3:14b │
   │ Q4_K_M    │       │      │ Q4_K_M   │      │     │   BF16      │
   └───────────┘       │      └──────────┘      │     └─────────────┘
                  ┌────▼────┐                    │
                  │ Ollama  │                    │
                  │ nomic   │                    │
                  └─────────┘                    │
                                              共享 vLLM 实例
                                              （continuous batching）
```

**关键设计**：chat 与 judge **不共享** vLLM 实例，而是 judge 与 classify 共享——因为它们都是"qwen3:14b + 短文本 + JSON 输出"特征相似，可共享受 KV cache 复用与 continuous batching。

### §3.2 张量并行度选择：TP=2

| 候选 | 优势 | 劣势 | 决策 |
|---|---|---|---|
| **TP=2** | 充分利用双卡；单进程管理简单 | 通信开销存在 | **✅ 选** |
| TP=1 | 无通信 | GPU 1 闲置 | ❌ |
| TP=4 | 通信最少（如果扩到 4 卡）| 当前只有 2 卡 | ❌ |
| PP=2（pipeline）| 与 Ollama 类似 | 同步开销同 Ollama | ❌ |

### §3.3 显存分配策略

| 资源 | 14B BF16 | 14B AWQ‑int4 | 32B BF16（未来）|
|---|---|---|---|
| **模型权重** | 28 GB / 2 = 14 GB/GPU | 7 GB / 2 = 3.5 GB/GPU | 64 GB / 2 = 32 GB/GPU |
| **KV cache（32k ctx, batch 8）** | ~8 GB/GPU | ~10 GB/GPU | ~16 GB/GPU |
| **激活 + workspace** | ~2 GB/GPU | ~2 GB/GPU | ~3 GB/GPU |
| **单卡总需求** | ~24 GB | ~15 GB | ~51 GB |
| **是否放得进 16 GB/GPU？** | ⚠️ **紧张** | ✅ | ❌ 需 TP=4 |

**M3 选型决策**：
- **judge**：BF16 + `max_model_len=16384`（降 ctx 省 KV cache）+ `max_num_seqs=16`
- **reason**：AWQ‑int4 + `max_model_len=8192`（推理输出一般短）
- **未来 32B 模型**：BF16 + `max_model_len=8192`（必要时扩到 64B 走 4 卡）

### §3.4 vLLM 启动配置

**judge 实例**（与 classify 共享）：
- 模型：`/home/jiuben/models/Qwen3-14B/`（用户已下载的 BF16 safetensors）
- 端口：8001（与 chat vLLM 8000 区分；或共用 8000 按 routing）
- TP=2、`dtype=bfloat16`、`max_model_len=16384`
- `gpu_memory_utilization=0.92`、`max_num_seqs=16`、`max_num_batched_tokens=4096`
- `enable_prefix_caching=True`（共享 system prompt 段）
- `enable_chunked_prefill=True`（长 prompt 分块）

**reason 实例**（独立，可选）：
- 模型：`deepseek-r1:14b`（Ollama 内 GGUF）—— 或转 AWQ 量化后单独 vLLM 部署
- 端口：8002
- TP=2、AWQ‑int4、`max_model_len=8192`

**chat 实例**（可选迁移）：
- 当前 Ollama 跑 qwen3:14b chat 76~95s，**也可以迁到 vLLM TP=2**
- 优势：与 judge 共享 KV cache 预热；continuous batching 提升多用户 chat 体验
- 风险：vLLM 故障影响所有 Tool → 必须双套（Ollama 作为 backup）

### §3.5 模型加载方式选择

| 方式 | 优点 | 缺点 | 决策 |
|---|---|---|---|
| **本地 safetensors** | 已下载（28 GB BF16 Qwen3-14B）；快；离线 | 需手动管理版本 | ✅ 主路径 |
| HuggingFace Hub | 官方版本；自动更新 | 需联网；下载时间长 | 备份路径 |
| Ollama GGUF 转 safetensors | 复用现有模型 | 多一步转换；Q4_K_M 量化损失 | 不推荐 |
| AWQ 预量化版 | 显存省 50% | 需自己跑 AWQ 量化脚本（~2h on GPU）| 后续优化 |

### §3.6 judge Tool 调用改造

`judge_impl` 内部新增分支：当请求到达时，**先 ping vLLM 健康检查 → 调用 vLLM OpenAI 兼容接口**。vLLM 不可达时降级到 Ollama（带更长 timeout）。

调用流程：
1. 客户端 MCP 请求 `judge(a, b, criteria)`
2. MCP server 校验输入长度 + 加载 system prompt
3. 调用 vLLM `/v1/chat/completions`（OpenAI 兼容）→ 返回 JSON
4. 解析 verdict / score / rationale
5. 落审计日志
6. 若 vLLM 不可达 → 降级到 Ollama（带 timeout=600s）
7. 若 Ollama 也失败 → 标 `fallback_chain: [vllm, ollama]`，返回 ok=false

---

## §4 · 关键配置参数说明

### §4.1 vLLM 启动参数

| 参数 | 值 | 说明 |
|---|---|---|
| `--model` | `/home/jiuben/models/Qwen3-14B/` | 本地路径，无须下载 |
| `--tensor-parallel-size` | `2` | 双卡 TP=2 |
| `--dtype` | `bfloat16` | BF16 精度（vs Ollama Q4_K_M）|
| `--max-model-len` | `16384` | 降 ctx 省 KV cache |
| `--gpu-memory-utilization` | `0.92` | 显存利用率上限 |
| `--max-num-seqs` | `16` | continuous batch 最大并发序列数 |
| `--max-num-batched-tokens` | `4096` | 单次 batch 总 token 上限 |
| `--enable-prefix-caching` | `True` | 共享 system prompt KV cache |
| `--enable-chunked-prefill` | `True` | 长 prompt 分块填 prefilling |
| `--port` | `8001` | HTTP 端口（OpenAI 兼容） |
| `--host` | `127.0.0.1` | 仅本地（安全）|
| `--served-model-name` | `qwen3-14b-vllm` | 客户端识别名 |

### §4.2 MCP Tool 调优参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `VLLM_JUDGE_URL` | `http://127.0.0.1:8001` | vLLM judge 实例地址 |
| `VLLM_JUDGE_TIMEOUT_S` | `60` | judge vLLM 调用超时 |
| `VLLM_JUDGE_FALLBACK_OLLAMA_TIMEOUT_S` | `600` | Ollama 降级超时 |
| `VLLM_REASON_URL` | `http://127.0.0.1:8002` | vLLM reason 实例（可选）|
| `VLLM_ENABLED` | `true` | 是否启用 vLLM（false 时走纯 Ollama 路径）|
| `JUDGE_MAX_INPUT_CHARS` | `16000` | judge 输入总长上限 |

### §4.3 性能监控参数（Prometheus）

| 指标 | 类型 | 说明 |
|---|---|---|
| `vllm:request_success_total` | counter | 成功请求数 |
| `vllm:request_latency_seconds` | histogram | 请求延迟分布 |
| `vllm:time_to_first_token_seconds` | histogram | TTFT 分布 |
| `vllm:gpu_cache_usage_perc` | gauge | KV cache 利用率 |
| `vllm:num_preemptions_total` | counter | 抢占次数（应 <10/小时）|
| `nvidia_smi_utilization_gpu` | gauge | GPU 利用率 |
| `nvidia_smi_memory_used_bytes` | gauge | 显存用量 |
| `judge_tool_invocations_total` | counter | MCP judge 调用次数 |
| `judge_tool_vllm_fallback_total` | counter | 降级到 Ollama 次数 |

---

## §5 · 预期性能提升对比

### §5.1 量化对比（双 5060 Ti 16GB · qwen3:14b · judge 典型负载）

| 指标 | Ollama（当前）| vLLM TP=2 BF16 | vLLM TP=2 AWQ‑int4 | 提升（vs Ollama）|
|---|---|---|---|---|
| **judge 平均延迟** | >300s（超时）| **5~15s** | 4~10s | **20~60×** |
| **TTFT**（首个 token）| 5~10s | **0.1~0.5s** | 0.1~0.3s | 20~50× |
| **TPOT**（每输出 token）| ~0.5~1s | **0.05~0.1s** | 0.03~0.06s | 10~15× |
| **单请求总 token 数** | ~1000 | ~1000 | ~1000 | — |
| **并发能力** | 1 | **16** | 24 | 16~24× |
| **吞吐（req/s）** | 0.003 | **1~2** | 2~3 | 300~1000× |
| **吞吐（tokens/s）** | ~3 | **80~120** | 120~180 | 30~60× |
| **VRAM 利用率** | 40~60%（layer‑split）| **85~92%** | 85~92% | 1.5~2× |
| **准确率** | Q4_K_M 基线 | **BF16 +5~8pp** | AWQ +3~5pp | +5~8 pp |

### §5.2 资源占用对比

| 项 | Ollama 双卡 layer‑split | vLLM TP=2 BF16 |
|---|---|---|
| 进程数 | 1（Ollama）+ 1（vLLM）| 1（Ollama chat/embed）+ 1（vLLM judge） |
| GPU 0 显存 | ~15 GB | ~14 GB（vLLM） |
| GPU 1 显存 | ~15 GB | ~14 GB（vLLM） |
| 总 VRAM | 30 GB / 32 GB | 28 GB / 32 GB |
| **显存余量** | 2 GB（紧张）| 4 GB（可加 max_num_seqs）|

### §5.3 M2/M3 路线图衔接

- **M2 阶段（W1~W12）**：维持 Ollama，所有 Tool 走 Ollama；judge 慢但有 fallback
- **M3 阶段（M3‑W1~W6）**：引入 vLLM TP=2；judge / reason 迁到 vLLM；Ollama 保留为 chat/embed/fallback
- **M3‑W7~W12**：评估 chat 也迁到 vLLM；按需扩展到 qwen3:32b

---

## §6 · 长期维护与扩展建议

### §6.1 适配更大模型（14B → 32B → 70B）

| 模型 | 参数量 | 显存（BF16）| 双卡 TP=2 单卡 | 决策 |
|---|---|---|---|---|
| qwen3:14b | 14.8B | 28 GB | 14 GB/GPU | ✅ 当前 |
| qwen3:32b | 32B | 64 GB | 32 GB/GPU | ❌ OOM；需 AWQ（16 GB/GPU）|
| qwen3:70b | 70B | 140 GB | 70 GB/GPU | ❌ 需 4 卡 |
| Llama-3.3-70B | 70B | 140 GB | 70 GB/GPU | ❌ 需 4 卡 |
| deepseek-v3 67B MoE | 67B (激活 13B) | 67B×int8=67 GB | 33 GB/GPU | ⚠️ 需 AWQ；MoE 优势是激活小 |

**策略**：
1. **14B → 32B**：升级到 AWQ‑int4 量化（需自己跑量化脚本 ~2h GPU）+ TP=2 可行
2. **32B → 70B**：扩到 4 卡（加配 2 张 RTX 5090 32GB / A6000 48GB）；或用 4‑bit AWQ + TP=2 强压
3. **新模型上线**：先在 vLLM 离线测试 → benchmark → 灰度 10% → 全量

### §6.2 监控与 SLO

**核心 SLO**：
| SLO | 目标 | 测量方法 |
|---|---|---|
| judge p50 延迟 | ≤ 8s | Prometheus histogram p50 |
| judge p95 延迟 | ≤ 20s | Prometheus histogram p95 |
| judge 成功率 | ≥ 99.5% | success_total / total |
| GPU 利用率（白天）| 60~85% | nvidia_smi 持续监控 |
| 降级率（vLLM → Ollama）| ≤ 0.5% | fallback_total / total |
| VRAM 利用率 | ≤ 92% | memory_used / memory_total |

**告警规则**（Alertmanager）：
- judge p95 > 30s 持续 5 分钟 → 黄色告警 → 扩容 max_num_seqs
- judge p95 > 60s 持续 1 分钟 → 红色告警 → 自动重启 vLLM
- VRAM > 95% 持续 1 分钟 → 红色告警 → 拒绝新请求（429）
- vLLM 进程 down > 30s → 红色告警 → 切换到 Ollama fallback

### §6.3 回退机制

```
正常路径：judge → vLLM TP=2 BF16（5~15s）

降级路径（按顺序）：
1. vLLM 健康检查失败 → judge → Ollama qwen3:14b Q4_K_M（timeout=600s）
2. Ollama 也失败 → judge → DeepSeek-R1 via Ollama（timeout=60s）
3. 全部失败 → judge 返回 ok=false + 错误码 UNREACHABLE
4. 客户端缓存最近 verdict（5 分钟 TTL）→ 兜底返回
```

**自动恢复**：
- vLLM down → systemd unit 自动重启（`Restart=always`，间隔 5s）
- Ollama down → 已用 systemd unit（见 v5/systemd/ollama.service）
- 健康检查：每 30s 一次 vLLM `/health`；失败 3 次标记 unhealthy

### §6.4 升级配置与硬件建议

**短期（M3 阶段）**：
- 维持 2x RTX 5060 Ti 16GB
- judge/reason 走 vLLM TP=2
- chat 走 Ollama（不迁移）
- 加 Prometheus + Grafana 监控

**中期（6 个月）**：
- 升级到 2x RTX 5090 32GB（消费级最强）
- 或加 2 张 RTX A4000 16GB 共 64GB VRAM（4 卡 TP=4）
- 跑 qwen3:32b BF16 或 qwen3:70b AWQ‑int4

**长期（1 年）**：
- 4x RTX A6000 48GB = 192GB VRAM
- 可跑 70B BF16 TP=4 + 多实例
- 或考虑云端推理（Modal / RunPod / Lambda Labs）作为补充

---

## §7 · 实施步骤（4 阶段 · 6 周）

### §7.1 Phase A · vLLM 部署与基准（Week 1~2）

| 周 | 工作 | 交付物 | 验收 |
|---|---|---|---|
| **W1** | 安装 vLLM（≥0.6）；准备 Python 环境 | vllm==0.6.x | 启动 vLLM 空模型通过 |
| **W1** | 单卡 baseline：`vllm serve Qwen3-14B --tensor-parallel-size 1` | 1 卡跑通 | TTFT < 1s |
| **W2** | TP=2 baseline：`--tensor-parallel-size 2` | 2 卡跑通 | judge 类似 prompt 30s 内返回 |
| **W2** | 写 `vllm_bench.py`：跑 100 个 judge 风格 prompt 记录 p50/p95 | 性能 baseline | p50<10s · p95<20s |

**风险与缓冲**：
- vLLM 与 PyTorch 版本不兼容 → 锁定 vllm==0.6 + torch 2.5 组合
- 16 GB 显存 OOM → 降 max_model_len 至 8192 或 AWQ 量化

### §7.2 Phase B · MCP server 集成（Week 3~4）

| 周 | 工作 | 交付物 | 验收 |
|---|---|---|---|
| **W3** | `judge_impl` 增加 vLLM 路径（健康检查 + 调用 + fallback）| `judge_v2.py` | judge 走 vLLM < 10s |
| **W3** | reason Tool 同步改造 | `reason_v2.py` | reason 走 vLLM < 8s（短 prompt）|
| **W4** | 验收测试 v2：judge 真实负载 50 次 | `test_v3.py` | judge p95 < 20s |
| **W4** | systemd unit for vLLM（vllm-judge.service） | unit file | 重启自动拉起 |

**风险与缓冲**：
- vLLM 与 Ollama 抢显存 → 调 `gpu_memory_utilization` 至 0.85
- fallback 切换延迟 → 客户端感知 ~30s 延迟，超时重试

### §7.3 Phase C · 监控与告警（Week 5）

| 周 | 工作 | 交付物 | 验收 |
|---|---|---|---|
| **W5** | Prometheus + node_exporter + vllm_exporter | Prometheus 服务 | 9 项指标采集 |
| **W5** | Grafana dashboard（judge / reason / GPU / fallback）| dashboard.json | 5 张图可视化 |
| **W5** | Alertmanager 配置（4 条告警）| alert rules | 模拟告警能 fire |

### §7.4 Phase D · 灰度上线与文档（Week 6）

| 周 | 工作 | 交付物 | 验收 |
|---|---|---|---|
| **W6** | 灰度：5% → 25% → 100% judge 流量到 vLLM | 灰度记录 | 错误率 < 0.5% |
| **W6** | 写 `v5/docs/vllm-deployment.md` | 部署手册 | 团队可独立操作 |
| **W6** | 更新 `v5/README-llm-ollama.md`（新增 vLLM 章节）| README v2 | 含 vLLM 启动配置 |

### §7.5 里程碑

| 里程碑 | 节点 | 通过条件 |
|---|---|---|
| **M3.1 vLLM 跑通** | W2 末 | 双卡 TP=2 启动 + judge 30s 内返回 |
| **M3.2 MCP 集成** | W4 末 | judge p95 ≤ 20s；fallback 可用 |
| **M3.3 监控就绪** | W5 末 | 9 项指标 + 4 条告警 |
| **M3.4 全量上线** | W6 末 | 100% 流量 + 文档完备 |

---

## §8 · 关键风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 | Owner |
|---|---|---|---|---|---|
| R1 | vLLM 与 PyTorch / CUDA 版本冲突 | 中 | 部署延期 | 锁定 vllm==0.6 + torch 2.5 | Tech Lead |
| R2 | 双 16GB 放 BF16 14B OOM | 中 | 必须 AWQ 量化 | 提前跑 AWQ 量化脚本（~2h） | Backend‑1 |
| R3 | vLLM 进程崩 → judge 全面失败 | 中 | 用户感知延迟 | systemd 自动重启 + Ollama fallback | DevOps |
| R4 | 与 Ollama 抢 GPU 资源 | 中 | chat 也变慢 | 调 `gpu_memory_utilization` 至 0.85 + chat 走 Ollama 独占 | Backend‑1 |
| R5 | vLLM 0.6→0.7 API breaking | 低 | 升级失败 | 锁定 0.6 半年；升级前 fork 测试 | Tech Lead |
| R6 | 量化脚本输出与原生 BF16 偏差 | 低 | judge 准确性微降 | 用 AWQ + GPTQ 双量化选优 | ML |
| R7 | 监控指标不全 | 低 | 故障排查慢 | 周末补全 SLO 仪表 | DevOps |
| R8 | 大模型（32B/70B）需更多卡 | 高 | 长期不可行 | 中期升级到 4 卡 / 云端推理 | Owner |

---

## §9 · 一句话总结

> **把 judge 工具从 Ollama Q4_K_M layer‑split 迁到 vLLM TP=2 BF16，预计 latency 降 20~60×（>300s → 5~15s）、并发升 16×、吞吐升 30~60×；Ollama 保留为 chat/embed/fallback；6 周 4 阶段实施；关键风险是显存 OOM 与 vLLM/Ollama 抢资源，均有 fallback。**

---

*版本：v5‑VE1‑M3 优化设计 · v1 · 2026‑09‑17 · 基线：v5‑VE1‑M2 · 目标完成：2026‑10‑29（6 周） · 与 v5‑VE1‑M2 §12.3 D2.5、§13 排期、§13.10 R7 衔接 · 配套交付 vLLM systemd unit + Prometheus 配置 + 部署手册*