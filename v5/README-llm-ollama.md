# mcp-llm-ollama 安装与使用指南

## 概述

`mcp-llm-ollama` 是 v5-VE1-M2 的 LLM 接入 MCP server，把本地 Ollama daemon 暴露为 5 个 MCP Tool：

| Tool | 默认模型 | 用途 |
|---|---|---|
| `chat` | qwen3:14b | 通用对话（支持 JSON mode） |
| `embed` | nomic-embed-text:latest | 768 维向量嵌入 |
| `reason` | deepseek-r1:14b | 推理兜底（8s 超时降级到 qwen3） |
| `classify` | qwen3:14b | 文本分类（强制 JSON） |
| `judge` | qwen3:14b | 双文本判定（强制 JSON） |

## 文件结构

```
v5/
├── mcp_servers/
│   └── llm_ollama.py           # MCP server 主程序（~330 行）
├── llm/prompts/
│   ├── latex_repair/system.txt       # D1.5 LaTeX 修复模板
│   ├── math_reason/system.txt        # D2.5 推理模板
│   ├── bilingual_translate/system.txt # D4.2 双语翻译模板
│   └── embedding_input/system.txt    # D6.7 嵌入使用说明
├── tests/
│   └── test_llm_ollama.py      # 验收测试（双层：HTTP / MCP 协议）
├── audit/
│   └── llm-calls.jsonl         # 自动生成的审计日志
├── systemd/
│   ├── ollama.service          # Ollama daemon 自启
│   └── mcp-llm-ollama.service  # MCP server 自启（可选）
└── README-llm-ollama.md        # 本文档
```

## 快速测试

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python v5/tests/test_llm_ollama.py
```

预期：18+ pass / 0~2 fail（fail 项通常是 judge 工具因硬件限制 300s 仍不够）。

## 启动 MCP server

```bash
# 方式 1：stdio（被 MCP client 进程化调用，推荐）
venv/bin/python v5/mcp_servers/llm_ollama.py

# 方式 2：HTTP（用于调试 / 远程调用，需修改主程序使用 streamable_http_app）
```

## 配置 MCP client（CodeBuddy / Claude Desktop）

`~/.config/CodeBuddy*/.../mcp.json` 或 Claude Desktop 配置：

```json
{
  "mcpServers": {
    "llm-ollama": {
      "command": "/home/jiuben/tdx-data-feed/venv/bin/python",
      "args": ["/home/jiuben/tdx-data-feed/v5/mcp_servers/llm_ollama.py"],
      "env": {
        "OLLAMA_HOST": "http://127.0.0.1:11434"
      }
    }
  }
}
```

## 自启配置

```bash
# Ollama daemon 自启
sudo cp v5/systemd/ollama.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ollama.service
sudo systemctl status ollama.service

# （可选）mcp-llm-ollama 自启
sudo cp v5/systemd/mcp-llm-ollama.service /etc/systemd/system/
sudo systemctl daemon-reload
# 注：stdio 模式下 mcp server 由 client 启动，无需 enable
```

## 调优参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama daemon 地址 |
| `LLM_DEFAULT_CHAT` | `qwen3:14b` | chat Tool 默认模型 |
| `LLM_DEFAULT_REASON` | `deepseek-r1:14b` | reason Tool 默认模型 |
| `LLM_DEFAULT_EMBED` | `nomic-embed-text:latest` | embed Tool 默认模型 |
| `LLM_CHAT_TIMEOUT_S` | `300` | chat 超时（14B 模型生成慢） |
| `LLM_REASON_TIMEOUT_S` | `8` | reason 超时（必须短，触发降级） |
| `LLM_EMBED_TIMEOUT_S` | `10` | embed 超时 |
| `LLM_MAX_INPUT_CHARS` | `32000` | 防 DoS |

## 性能说明（基于本机实测）

- **GPU**：2x RTX 5060 Ti 16GB（共 32GB VRAM）
- **qwen3:14b**：约 9 GB Q4_K_M 量化 → 跨双卡，单次推理 **60~90 秒**（取决于输出长度）
- **deepseek-r1:14b**：约 9 GB Q4_K_M → 跨双卡，推理 **60~120 秒**
- **nomic-embed-text**：100 MB → 单卡，**0.04 秒/批**
- **冷启动**：首次加载模型需 30~40 秒

**重要**：由于 VRAM 限制，**同时只能驻留 1 个 14B 模型**。reason 工具会触发模型 swap，可能 60~120 秒延迟。

## 已知限制

1. **judge 工具在 300s 超时下仍可能超时**——14B 模型对长 LaTeX + JSON 输出组合慢
   - 缓解：生产中用 8B 模型（qwen3:8b）替代
   - 或：减少 judge 输入字符数
2. **reason 工具 swap 慢**——deepseek ↔ qwen3 切换需 ~30s
   - 设计上 reason 8s 超时强制降级到 qwen3
3. **多并发支持有限**——`OLLAMA_NUM_PARALLEL=1`（一个请求处理完才能接下一个）
4. **审计日志无轮转**——`llm-calls.jsonl` 会持续增长
   - 缓解：定期用 `logrotate` 或自己写 cron

## 与 v5-VE1-M2 引用点对应

| M2 引用 | 对应 Tool | 默认模型 |
|---|---|---|
| D1.5 语义修复 | `chat` (format=json) + `latex_repair` 模板 | qwen3:14b |
| D2.5 DeepSeek-R1 兜底 | `reason` + `math_reason` 模板 | deepseek-r1:14b（fallback qwen3:14b）|
| D4.2 双语翻译 | `classify` (format=json) + `bilingual_translate` 模板 | qwen3:14b |
| D6.7 LSH 预筛 | `embed` + `embedding_input` 模板 | nomic-embed-text:latest |
| M2 文档误分类时纠错 | `judge` + 文档相似度判定 | qwen3:14b |

## 故障排查

| 症状 | 排查 |
|---|---|
| `Ollama 不可达` | `pgrep -af "ollama serve"`；`curl http://127.0.0.1:11434/api/tags` |
| 30s 超时频繁 | 模型未驻 VRAM；调用前先用 `ollama run <model> ""` 预热 |
| `mcp.client` 找不到工具 | 检查 `python` 路径；先单独跑 `llm_ollama.py` 看是否能列出工具 |
| 输出非 JSON | Ollama 版本过低（<0.33）不支持 `format: "json"`；升级 ollama |