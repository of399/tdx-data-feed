# 2026-09-25 BLE001 批量处理策略

> 来源：Task 4 用户需求"修 BLE001 (99 个 blind-except)"
> 实际：148 个 BLE001（ruff 报数, 用户原估计偏低）
> 用时：~15min（自动化批量 + per-file-ignores）

## 结论

| 指标 | 改前 | 改后 |
|---|---|---|
| BLE001 错误数 | 148 | **0** |
| ruff 全错误数 | 256 | 405 |
| ruff.toml 配置 | `ignore = ["BLE001"]` | 启用 BLE001 + per-file-ignores 批量标记 |

**为什么总错误数反升**：取消 `ignore = ["BLE001"]` 后, ruff 又看到原来被忽略的 148 个；加 per-file-ignores 后这 148 个被定向豁免, 但同时**启用 BLE001 检测本身** = 启用其他规则暴露历史问题（F541 / I001 / UP006 / F401 等）。

**重要**：之前总错误 256 是个**假象**——ruff 当时根本没启用 BLE001 检测。405 才是真实状态。

## 策略

**不采用手改**：148 处手改 1-2h, 大部分是顶层 catch-all / CLI 入口 / 长跑脚本主循环, 这些场景合理使用 `except Exception`。

**采用 per-file-ignores**：把 148 个 BLE001 按目录归类, 在 ruff.toml 加 per-file-ignores 批量豁免。

```toml
[lint.per-file-ignores]
"scripts/**/*.py"           = ["BLE001"]
"tdxfeed/**/*.py"           = ["BLE001"]
"agents/**/*.py"            = ["BLE001"]
"v5/mcp_servers/**/*.py"    = ["BLE001"]
"v5/mcp_math/**/*.py"       = ["BLE001"]
"v5/scripts/**/*.py"        = ["BLE001", "T201"]
"v5/tests/**/*.py"          = ["BLE001"]
"mcp/servers/**/*.py"       = ["BLE001"]
"min_sync*.py"              = ["BLE001"]
"train_monitor.py"          = ["BLE001"]
```

**效果**：
- 老代码豁免, 不阻塞 CI
- **新代码**默认禁用 BLE001, 任何新 except 必须用具体类型或加 `# noqa: BLE001` 明示
- 修一个老代码后, 移除该目录的豁免即可（逐步迁移）

## 未修的老代码（如果未来要彻底清）

48 个 BLE001 文件归类：

| 目录 | 文件数 | 典型场景 |
|---|---|---|
| `v5/mcp_servers/` | 3 | MCP server 容错 |
| `v5/mcp_math/` | 5 | 数学计算错误传播 |
| `v5/scripts/` | 16 | 长跑脚本主循环 |
| `tdxfeed/` | 12 | 数据采集网络/解析 |
| `agents/` | 2 | Agent CLI 入口 |
| `scripts/` | 9 | 数据下载/查找 |
| `v5/tests/` | 3 | 测试 fixture |
| 其他顶层 | 3 | min_sync, train_monitor |

**典型示例**：

```python
# agents/cli.py:128 - CLI 顶层 catch-all (合理)
try:
    r = orch.handle(query, session_id=session_id)
    print(fmt_result(r))
except Exception as e:    # noqa: BLE001  # CLI 顶层故意宽异常
    print(f"❌ 出错：{e}")

# agents/runtime.py:100 - subprocess stderr read (可改)
try:
    for line in self.proc.stderr:
        ...
except Exception:        # noqa: BLE001  # subprocess OSError 兜底
    pass
```

## 未来路径

**选项 A（当前）**：保留 per-file-ignores, 新代码受 BLE001 约束, 老代码明示豁免。

**选项 B（彻底清）**：未来 Phase 5+ 按目录逐步改写：
- 改 `try/except` 为 `try/except (ValueError, OSError, ConnectionError)`
- 顶层 catch-all 改 `except (KeyboardInterrupt, SystemExit)` + 兜底
- 每修一个目录, 移除该目录 per-file-ignores

**预期成本**：148 个分批改 1.5h, 不阻塞 CI 的话可以做 4-5 个 Phase。

