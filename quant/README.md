# quant（workspace 内数据获取模块）

> 本模块取代并弃用 workspace 外的 `~/quant/` 方案（外部路径在 IDE 内预览/打开受限）。
> 所有项目文件统一置于 workspace 内，可正常预览、打开、提交。

## 设计原则（已采纳的调整，2026-10-06）

1. **路径**：全部在 `tdx-data-feed/` 内，不复用 `~/quant`。
2. **后端**：阶段一固定新浪后端（`stock_zh_a_daily` / `SinaSource`）；`stock_zh_a_hist`（东财）被环境阻断，禁用。
3. **环境**：优先复用现有 `venv`(py3.14)；如需 py3.11 隔离，用 `/opt/miniforge3/bin/conda` 显式建（不在 PATH）。
4. **抽象复用**：基于 `tdxfeed/sources/{base,akshare,sina}_source.py` + `writers.py` 扩展，不重建。
5. **互补源**：优先 easyquotation（Sina/Tencent 后端）；baostock 仅备选。
6. **连续性校验**：用 akshare 真实交易日历，避免节假日误判缺失。
7. **回测**：阶段三优先 `scripts/plan_e_backtest.py` / backtrader；vn.py 仅按需且先验证 py3.14 兼容。
8. **MCP**：仅检索层，与本地行情拉数解耦。

## 用法

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python quant/fetch_history.py --symbol 600519 --start 20240101 --end 20240901
```

输出：`data/quant_demo/{code}_daily.parquet` + 校验报告（行数/字段/区间/应在交易日/缺失交易日/缺失率）。

## 模块

- `datafeed.py`：`DataFeed.fetch()` 返回标准 DataFrame（复用 `tdxfeed` 源）。
- `validate.py`：真实交易日历连续性 + 缺失率校验。
- `fetch_history.py`：阶段一入口（拉取 + 落盘 + 校验）。
