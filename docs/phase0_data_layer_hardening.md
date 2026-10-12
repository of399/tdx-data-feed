# 阶段 0 数据层加固 (2026-10-10)

## 📊 核心指标
- 新增数据源：`tdxfeed/sources/mootdx_source.py`（`MootdxSource`），替代已归档的 `pytdx` 作为通达信主通道
- 指数主通道切换：`full_sync.download_indices` 由 `PytdxSource` → `MootdxSource`，Sina 兜底保留
- 新增质量指标：`verify_daily_freshness.py` 的「源失败率（同步质量）」段落，读取 `logs/sync_summary.json`
- mootdx 版本：**0.11.7**（httpx 0.28.1 环境下可正常导入，模块路径为 `mootdx.quotes`）

## 🎯 改造明细
| 文件 | 改动 | 说明 |
|---|---|---|
| `tdxfeed/sources/mootdx_source.py` | 新增 | `MootdxSource(DataSource)`；`fetch_bars`/`fetch_index_bars`；连接/返回空时抛 `RuntimeError` 触发回退 |
| `tdxfeed/full_sync.py` | 修改 | 指数主通道切 mootdx；`download_indices` 返回通道计数；新增 `write_sync_summary` 写出 `sync_summary.json` |
| `scripts/verify_daily_freshness.py` | 修改 | 新增 `source_failure_block`，报告源失败率与个股/指数通道分布 |
| `tdxfeed/sources/pytdx_source.py` | 保留 | 作为回滚兜底，暂未删除 |

## 📈 关键设计
- **失败语义**：`MootdxSource` 连接失败或返回空均抛 `RuntimeError`，由 `full_sync` 调用方捕获并回退 Sina —— 支撑「源失败率趋向 0」目标。
- **服务器选取**：`_default_server()` 复用 `pytdx.config.hosts.hq_hosts` 首个地址显式传入 `Quotes.factory(server=...)`，避免 `bestip` 网络探测在本环境失败（曾导致 `ValueError: not enough values to unpack`）。
- **volume 单位对齐**：mootdx `bars`/`index_bars` 底层为 `get_security_bars`/`get_index_bars`，`vol` 单位=**手**；Sina 兜底 `fetch_bars` 已 `÷100` 转手。两者落盘单位一致，复用 `writers.merge_write` 的 `kind='mergesort'` 去重修复保证增量覆盖正确，无需额外 `÷100` 处理。
- **源失败率口径**：`(个股失败 + 指数失败) / (个股成功 + 个股失败 + 指数成功 + 指数失败)`，目标 0%。本环境 TDX 公共服务器不可达时由 Sina 兜底，指数通道不计失败。

## ⚠️ 风险提示
1. **本环境 TDX 公共服务器仍返回 0 根**（网络/IP 层问题，非代码 bug），指数实际仍走 Sina 兜底；mootdx 主通道价值在于活跃维护库 + 其他环境可用，本次不宣称"修复同步"。
2. mootdx `index_bars` 内部按代码前缀判定 market（00/88→沪、99→深），与 `INDICES` 表一致；若新增非标准指数需复核。
3. `pytdx_source.py` 暂保留作回滚；若确认 mootdx 稳定可后续删除。
4. GitHub MCP 令牌已由用户侧回填并实测连通（`search_repositories` 返回 200），可用于持续跟踪上游更新（详见附录"GitHub MCP 连通复盘"）。

## 🔧 下一步建议
1. 运行 `venv/bin/python -m tdxfeed.full_sync` 实际同步，观察 `logs/sync_summary.json` 的源失败率与指数通道分布。
2. 运行 `venv/bin/python scripts/verify_daily_freshness.py` 查看含「源失败率」的完整报告。
3. ~~用户侧回填 GitHub MCP 令牌~~ ✅ 已完成：令牌回填并经完整 IDE 重启后实测连通（`search_repositories` 返回 200；根因为 IDE 缓存了启动时的占位符配置，需完整重启才生效）。
4. 跨环境验证：在可直连 TDX 服务器的机器上确认 mootdx 主通道真实命中（`mootdx主` > 0）。

---
生成时间：2026-10-10 11:50:00
生成者：tdx-data-feed 阶段0落地（agent）
模型版本：v7r
数据日期：2026-10-10

---

## 附录：GitHub MCP 连通复盘（2026-10-10）
- **现象**：在 `.codebuddy/mcp.json` 填好 `GITHUB_PERSONAL_ACCESS_TOKEN` 后，MCP 调用仍报 `401 Bad credentials`。
- **排查**：直连 `api.github.com/user` 用同一令牌返回 200（网络层正常）；但 `docker inspect` 显示 MCP 容器注入的 env 仍是占位符 `<请到GitHub重新生成后填回>`。
- **根因**：IDE 启动时会把当时的 mcp.json 缓存进内存；仅"重启 MCP"只重连客户端、不会重读磁盘，重建容器时仍注入缓存里的旧占位符。（中途曾因 `sed` 误测令牌长度为 39（实际 40）而一度误判令牌无效，已用 Python 精确读取纠正。）
- **解决**：对 IDE 执行**完整重启**（关闭窗口再打开），使其从磁盘重新读取配置；重启后 `search_repositories` 正常返回数据，401 消失。
- **结论**：修改 MCP 令牌类配置后，必须完整重启 IDE（而非仅重启 MCP 服务）才能生效。
