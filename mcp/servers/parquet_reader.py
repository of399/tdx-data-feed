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
import re
import sys
from collections import Counter
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
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import daily_dir, finance_parquet, xdxr_path  # noqa: E402

# P1-5：MCP 运行时结构化 schema 校验（入站信封/工具入参 + 出站响应自检）。
# 以脚本方式运行或无 mcp 包时，按文件直载兜底，保证两种加载方式都可用。
try:
    from mcp.schema_validation import (
        McpValidationError,
        validate_request,
        validate_response,
        validate_tool_args,
    )
except Exception:  # pragma: no cover - 退化路径
    import importlib.util as _ilu
    import os as _os
    _vs = _ilu.spec_from_file_location(
        "tdx_mcp_schema_validation",
        _os.path.join(_os.path.dirname(__file__), "..", "schema_validation.py"))
    _vm = _ilu.module_from_spec(_vs)
    _vs.loader.exec_module(_vm)
    validate_request = _vm.validate_request
    validate_tool_args = _vm.validate_tool_args
    validate_response = _vm.validate_response
    McpValidationError = _vm.McpValidationError

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

DAILY_DIR = daily_dir()
FINANCE_PARQUET = finance_parquet()
XDXR_PARQUET = xdxr_path()
LOG_FILE = ROOT / "logs/mcp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:parquet-reader] %(message)s",
)


# === 工具实现 =============================================================
def _norm_date(value: str | None) -> str | None:
    """统一成 YYYY-MM-DD，兼容调用方传 YYYYMMDD。"""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _norm_code(code: str) -> str:
    """沪深 A 股代码归一化 + 校验（与 mcp/servers/news.py:60 同口径，避免再造一份实现）。

    仅接受 `sh|sz` + 6 位数字；裸 6 位按首位补前缀（6→sh，0/3→sz）。
    拒绝任何可能越出数据目录的路径片段（`../`、绝对路径、`/`）——这是 KI-MCP-003 的修复点。
    """
    s = str(code or "").strip().lower().replace(".", "")
    if re.fullmatch(r"(sh|sz)\d{6}", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        return ("sh" + s) if s.startswith("6") else ("sz" + s)
    raise McpError(-32602, f"非法代码：{code!r}（仅支持沪深 A 股 6 位代码，不接受路径）")


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → 严格 JSON 可序列化记录（NaN/inf 变 null）。"""
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


@lru_cache(maxsize=128)
def _read_daily_cached(code: str, mtime_check: float):
    """读日 K（带 LRU 缓存 + mtime 失效）"""
    path = DAILY_DIR / f"{code}.parquet"
    if not path.exists():
        raise McpError(404, f"代码 {code} 无日 K parquet")
    return pd.read_parquet(path)


@lru_cache(maxsize=1)
def _read_finance_cached(mtime_check: float):
    return pd.read_parquet(FINANCE_PARQUET)


def read_daily_kline(code: str, start: str | None = None, end: str | None = None,
                     limit: int | None = None) -> dict:
    """读日 K 线
    Args:
        code: 标的代码，如 sh600000
        start: 起始日期（YYYYMMDD 或 YYYY-MM-DD）
        end: 结束日期（YYYYMMDD 或 YYYY-MM-DD）
        limit: 仅返回最后 N 条
    Returns:
        {code, rows, data: [{date, open, high, low, close, volume, amount}, ...]}
    """
    code = _norm_code(code)
    path = DAILY_DIR / f"{code}.parquet"
    mtime = path.stat().st_mtime if path.exists() else 0
    df = _read_daily_cached(code, mtime).copy()
    if "date" in df.columns:
        df["date"] = df["date"].astype(str).map(_norm_date)
        start_n, end_n = _norm_date(start), _norm_date(end)
        if start_n:
            df = df[df["date"] >= start_n]
        if end_n:
            df = df[df["date"] <= end_n]
        df = df.sort_values("date")
    if limit and limit > 0 and len(df) > limit:
        df = df.tail(limit)
    return {
        "code": code,
        "rows": len(df),
        "data": _records(df),
    }


PREFERRED_OPTIONS = {
    "常用指标": 0, "盈利能力": 1, "成长能力": 2, "每股指标": 3,
    "收益质量": 4, "财务风险": 5, "营运能力": 6,
}


def read_finance(code: str | None = None, periods: int = 8, metric: str | None = None) -> dict:
    """读财务摘要（finance.parquet 是长格式：symbol/report_date/option/metric/value）
    Args:
        code: 标的代码（None = 全部）
        periods: 每个 code 取最近 N 期
        metric: 精确指标名（如 '归母净利润' / '营业总收入'）
    """
    if code:
        code = _norm_code(code)
    if not FINANCE_PARQUET.exists():
        raise McpError(404, f"finance.parquet 不存在：{FINANCE_PARQUET}")
    df = _read_finance_cached(FINANCE_PARQUET.stat().st_mtime).copy()
    # 长格式用 symbol 过滤
    if "symbol" in df.columns:
        df = df[df["symbol"] == code]
    elif "code" in df.columns:
        df = df[df["code"] == code]
    if metric:
        # 精确优先；模糊命中多个指标时拒绝猜测，返回候选让调用方选择
        exact = df[df["metric"] == metric]
        if len(exact):
            df = exact
        else:
            matched = sorted(
                df.loc[df["metric"].astype(str).str.contains(metric, regex=False, na=False),
                       "metric"].unique())
            if len(matched) == 1:
                df = df[df["metric"] == matched[0]]
            elif len(matched) > 1:
                raise McpError(-32602,
                               f"指标 '{metric}' 不唯一，请使用精确名称：{', '.join(matched[:10])}")
            else:
                df = df.iloc[0:0]
    # 同一报告期/指标在多个 option 下重复，优先常用指标，避免混算同比
    if "report_date" in df.columns:
        df = df.assign(_pref=df["option"].map(PREFERRED_OPTIONS).fillna(9)) \
            if "option" in df.columns else df.assign(_pref=0)
        df = df.sort_values(["report_date", "_pref"], ascending=[False, True])
        df = df.drop_duplicates(["report_date", "metric"], keep="first")
        df = df.drop(columns=["_pref"])
        if code and metric:
            df = df.head(periods)
        elif code:
            df = df.groupby("metric", sort=False).head(periods)
    return {
        "rows": len(df),
        "data": _records(df),
    }


def read_xdxr(code: str | None = None) -> dict:
    """读股本变动汇总（xdxr.parquet 实际是汇总表，无逐次事件）
    实际 schema: symbol, 代码, 名称, 上市日期, 累计股息, 年均股息, 分红次数, 融资总额, 融资次数
    """
    if code:
        code = _norm_code(code)
    if not XDXR_PARQUET.exists():
        raise McpError(404, f"xdxr.parquet 不存在：{XDXR_PARQUET}")
    df = pd.read_parquet(XDXR_PARQUET)
    # 实际字段是 symbol，不是 code
    if "symbol" in df.columns:
        df = df[df["symbol"] == code]
    elif "代码" in df.columns:
        # symbol 缺失时用代码列（去掉 sh/sz 前缀）
        df = df[df["代码"] == code[2:]]
    return {
        "rows": len(df),
        "data": _records(df),
    }


def list_codes(pattern: str | None = None, market: str | None = None,
               has_data: bool = True) -> dict:
    """列代码清单
    Args:
        pattern: 前缀过滤，如 'sh6'（沪市主板）/ 'sz000'（深市主板）
        market: 'sh' / 'sz' / None
        has_data: True = 仅返回有 parquet 的
    """
    files = [p for p in DAILY_DIR.glob("*.parquet") if ".bak" not in p.name]
    codes = [f.stem for f in files]
    if market:
        codes = [c for c in codes if c.startswith(market)]
    if pattern:
        codes = [c for c in codes if c[2:].startswith(pattern.lstrip("shsz"))]  # noqa: B005
    return {"count": len(codes), "codes": sorted(codes)}


# === MCP 协议 dispatcher ==================================================
TOOLS = {
    "read_daily_kline": {
        "description": "读日 K 线 Parquet",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "标的代码，如 sh600000"},
                "start": {"type": "string", "description": "起始日期（YYYYMMDD 或 YYYY-MM-DD）"},
                "end": {"type": "string", "description": "结束日期（YYYYMMDD 或 YYYY-MM-DD）"},
                "limit": {"type": "integer", "minimum": 1, "description": "仅返回最后 N 条（≥1）"},
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
                "periods": {"type": "integer", "default": 8, "minimum": 1, "maximum": 200, "description": "每个 code 取最近 N 期（1-200）"},
                "metric": {"type": "string",
                           "description": "精确指标名，如 营业总收入 / 归母净利润 / 净资产收益率(ROE)"},
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
                "market": {"type": "string", "enum": ["sh", "sz"], "description": "市场代码段"},
                "has_data": {"type": "boolean", "default": True},
            },
        },
        "handler": lambda args: list_codes(**args),
    },
}


def handle_request(req: dict) -> dict:
    """MCP JSON-RPC dispatcher（含 P1-5 入站校验）"""
    method = req.get("method")
    req_id = req.get("id")
    try:
        # P1-5：入站请求信封校验（jsonrpc / method / params 结构）
        validate_request(req)
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
            # P1-5：工具入参校验（必填/类型/枚举/取值范围），失败 → -32602 含字段与原因
            validate_tool_args(tool_name, TOOLS[tool_name]["inputSchema"], tool_args)
            t0 = datetime.now()
            result = TOOLS[tool_name]["handler"](tool_args)
            elapsed = (datetime.now() - t0).total_seconds() * 1000
            logging.info(f"{tool_name} latency={elapsed:.1f}ms")
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)}],
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "notifications/initialized":
            return None
        raise McpError(-32601, f"未知方法：{method}")
    except McpValidationError as e:
        _ERROR_COUNT['handle_request'] += 1
        # 信封/入参校验失败：Invalid Params（-32600/-32602），消息含具体字段与原因
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": e.code, "message": f"{e}"}}
    except McpError as e:
        _ERROR_COUNT['handle_request'] += 1
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        _ERROR_COUNT['handle_request'] += 1
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
                # P1-5：出站响应结构自检（仅告警，不阻断）
                _probs = validate_response(resp)
                if _probs:
                    logging.warning("响应结构异常: %s", "; ".join(_probs))
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as e:
            _ERROR_COUNT['main'] += 1
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"JSON 解析失败：{e}"}}
            print(json.dumps(err), flush=True)


if __name__ == "__main__":
    main()
