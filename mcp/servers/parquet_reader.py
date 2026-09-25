#!/usr/bin/env python3
"""
mcp/servers/parquet_reader.py — MCP Server: Parquet 数据读取
===========================================================

工具清单（详细 schema 见 schemas/tools.json）：
    1. read_daily_kline  读日 K 线（指定代码 + 日期范围）
    2. read_finance      读财务摘要
    3. read_xdxr         读股本变动
    4. list_codes        按前缀列代码（如 sh6 / sz000 / 8）

协议：MCP 2024-11-05（JSON-RPC over stdio）
"""

import json
import logging
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd


# === MCP 协议最小实现（避免强依赖 mcp sdk）==============================
class McpError(Exception):
    """MCP 错误"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")

# === 路径 ================================================================
ROOT = Path("/home/jiuben/tdx-data-feed")
DAILY_DIR = ROOT / "data/parquet/daily"
FINANCE_PARQUET = ROOT / "data/parquet/finance.parquet"
XDXR_PARQUET = ROOT / "data/parquet/xdxr.parquet"
LOG_FILE = ROOT / "logs/mcp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:parquet-reader] %(message)s",
)


# === 工具实现 =============================================================
@lru_cache(maxsize=128)
def _read_daily_cached(code: str, mtime_check: float):
    """读日 K（带 LRU 缓存 + mtime 失效）"""
    path = DAILY_DIR / f"{code}.parquet"
    if not path.exists():
        raise McpError(404, f"代码 {code} 无日 K parquet")
    return pd.read_parquet(path)


def read_daily_kline(code: str, start: str | None = None, end: str | None = None,
                     limit: int | None = None) -> dict:
    """读日 K 线
    Args:
        code: 标的代码，如 sh600000
        start: 起始日期 YYYYMMDD（可选）
        end: 结束日期 YYYYMMDD（可选）
        limit: 仅返回最后 N 条
    Returns:
        {code, rows, data: [{date, open, high, low, close, volume, amount}, ...]}
    """
    path = DAILY_DIR / f"{code}.parquet"
    mtime = path.stat().st_mtime if path.exists() else 0
    df = _read_daily_cached(code, mtime)
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        df = df.sort_values("date")
    if limit and len(df) > limit:
        df = df.tail(limit)
    return {
        "code": code,
        "rows": len(df),
        "data": df.to_dict("records"),
    }


def read_finance(code: str | None = None, periods: int = 8, metric: str | None = None) -> dict:
    """读财务摘要（finance.parquet 是长格式：symbol/report_date/option/metric/value）
    Args:
        code: 标的代码（None = 全部）
        periods: 每个 code 取最近 N 期
        metric: 可选，过滤特定指标（如 '归母净利润' / '营业收入'）
    """
    if not FINANCE_PARQUET.exists():
        raise McpError(404, f"finance.parquet 不存在：{FINANCE_PARQUET}")
    df = pd.read_parquet(FINANCE_PARQUET)
    if code:
        # 长格式用 symbol 过滤
        if "symbol" in df.columns:
            df = df[df["symbol"] == code]
        elif "code" in df.columns:
            df = df[df["code"] == code]
    if metric:
        df = df[df["metric"].str.contains(metric, na=False)]
    # 按报告日期倒序，每个 code 取最近 N 期
    if "report_date" in df.columns:
        df = df.sort_values("report_date", ascending=False)
        if code and metric:
            df = df.head(periods)
        elif code:
            df = df.groupby("metric").head(periods)
    return {
        "rows": len(df),
        "data": df.to_dict("records"),
    }


def read_xdxr(code: str | None = None) -> dict:
    """读股本变动汇总（xdxr.parquet 实际是汇总表，无逐次事件）
    实际 schema: symbol, 代码, 名称, 上市日期, 累计股息, 年均股息, 分红次数, 融资总额, 融资次数
    """
    if not XDXR_PARQUET.exists():
        raise McpError(404, f"xdxr.parquet 不存在：{XDXR_PARQUET}")
    df = pd.read_parquet(XDXR_PARQUET)
    if code:
        # 实际字段是 symbol，不是 code
        if "symbol" in df.columns:
            df = df[df["symbol"] == code]
        elif "代码" in df.columns:
            # symbol 缺失时用代码列（去掉 sh/sz 前缀）
            df = df[df["代码"] == code[2:]]
    return {
        "rows": len(df),
        "data": df.to_dict("records"),
    }


def list_codes(pattern: str | None = None, market: str | None = None,
               has_data: bool = True) -> dict:
    """列代码清单
    Args:
        pattern: 前缀过滤，如 'sh6'（沪市主板）/ 'sz000'（深市主板）/ 'bj83'（北交所）
        market: 'sh' / 'sz' / 'bj'（北交所）/ None
        has_data: True = 仅返回有 parquet 的
    """
    files = list(DAILY_DIR.glob("*.parquet"))
    codes = [f.stem for f in files]
    if market:
        codes = [c for c in codes if c.startswith(market)]
    if pattern:
        codes = [c for c in codes if c[2:].startswith(pattern.lstrip("shszbj"))]  # noqa: B005
    return {"count": len(codes), "codes": sorted(codes)}


# === MCP 协议 dispatcher ==================================================
TOOLS = {
    "read_daily_kline": {
        "description": "读日 K 线 Parquet",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "标的代码，如 sh600000"},
                "start": {"type": "string", "description": "起始日期 YYYYMMDD"},
                "end": {"type": "string", "description": "结束日期 YYYYMMDD"},
                "limit": {"type": "integer", "description": "仅返回最后 N 条"},
            },
            "required": ["code"],
        },
        "handler": lambda args: read_daily_kline(**args),
    },
    "read_finance": {
        "description": "读财务摘要（核心 300 + 补集，1.67M 行）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "标的代码（None=全部）"},
                "periods": {"type": "integer", "default": 8, "description": "每个 code 取最近 N 期"},
            },
        },
        "handler": lambda args: read_finance(**args),
    },
    "read_xdxr": {
        "description": "读股本变动（除权除息）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "标的代码（None=全部）"},
            },
        },
        "handler": lambda args: read_xdxr(**args),
    },
    "list_codes": {
        "description": "列代码清单（按市场 / 前缀过滤）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "代码前缀，如 sh6 / sz000"},
                "market": {"type": "string", "enum": ["sh", "sz", "bj"], "description": "市场代码段"},
                "has_data": {"type": "boolean", "default": True},
            },
        },
        "handler": lambda args: list_codes(**args),
    },
}


def handle_request(req: dict) -> dict:
    """MCP JSON-RPC dispatcher"""
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-parquet-reader", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }}
        if method == "tools/list":
            tools_list = [{
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            } for name, spec in TOOLS.items()]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}
        if method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            if tool_name not in TOOLS:
                raise McpError(-32602, f"未知工具：{tool_name}")
            t0 = datetime.now()
            result = TOOLS[tool_name]["handler"](tool_args)
            elapsed = (datetime.now() - t0).total_seconds() * 1000
            logging.info(f"{tool_name} latency={elapsed:.1f}ms")
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            }}
        if method == "notifications/initialized":
            return None
        raise McpError(-32601, f"未知方法：{method}")
    except McpError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        logging.exception("handler error")
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"内部错误：{e}"}}


def main():
    """stdio 模式：读一行 JSON 请求，返回一行 JSON 响应"""
    print("[mcp:parquet-reader] 已启动，stdio 模式", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as e:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"JSON 解析失败：{e}"}}
            print(json.dumps(err), flush=True)


if __name__ == "__main__":
    main()
