#!/usr/bin/env python3
"""
mcp/servers/calc.py — MCP Server: 金融计算
======================================================================

工具：
    1. calc_ma          简单/指数移动均线
    2. calc_macd        MACD（DIF / DEA / HIST / 金叉）
    3. calc_kdj         KDJ
    4. calc_rsi         RSI
    5. calc_boll        布林带
    6. calc_adj_factor  复权因子（前/后复权）

协议：MCP 2024-11-05（JSON-RPC over stdio）
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# === 路径 ================================================================
ROOT = Path("/home/jiuben/tdx-data-feed")
LOG_FILE = ROOT / "logs/mcp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:calc] %(message)s",
)


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


# === 工具实现 =============================================================
def calc_ma(prices: list[float], window: int = 5,
            type: str = "sma") -> list[float]:
    """简单 / 指数移动均线"""
    s = pd.Series(prices, dtype=float)
    if type == "sma":
        out = s.rolling(window).mean()
    elif type == "ema":
        out = s.ewm(span=window, adjust=False).mean()
    else:
        raise McpError(-32602, f"未知 type: {type}（仅支持 sma/ema）")
    return out.where(s.notna(), None).tolist()


def calc_macd(prices: list[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> dict:
    """MACD"""
    s = pd.Series(prices, dtype=float)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    golden = (dif > dea).astype(int)
    return {
        "dif": dif.where(s.notna(), None).tolist(),
        "dea": dea.where(s.notna(), None).tolist(),
        "hist": hist.where(s.notna(), None).tolist(),
        "golden_cross": golden.tolist(),
    }


def calc_kdj(high: list[float], low: list[float], close: list[float],
             n: int = 9) -> dict:
    """KDJ"""
    h = pd.Series(high, dtype=float)
    l = pd.Series(low, dtype=float)
    c = pd.Series(close, dtype=float)
    low_n = l.rolling(n).min()
    high_n = h.rolling(n).max()
    rsv = (c - low_n) / (high_n - low_n + 1e-9) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    j = 3 * k - 2 * d
    return {
        "k": k.where(c.notna(), None).tolist(),
        "d": d.where(c.notna(), None).tolist(),
        "j": j.where(c.notna(), None).tolist(),
    }


def calc_rsi(prices: list[float], n: int = 14) -> list[float]:
    """RSI"""
    s = pd.Series(prices, dtype=float)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / (loss + 1e-9)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(s.notna(), None).tolist()


def calc_boll(prices: list[float], n: int = 20, k: float = 2) -> dict:
    """布林带"""
    s = pd.Series(prices, dtype=float)
    mid = s.rolling(n).mean()
    std = s.rolling(n).std()
    return {
        "mid": mid.where(s.notna(), None).tolist(),
        "upper": (mid + k * std).where(s.notna(), None).tolist(),
        "lower": (mid - k * std).where(s.notna(), None).tolist(),
    }


def calc_adj_factor(xdxr_events: list[dict], end_date: str | None = None) -> dict:
    """复权因子计算
    Args:
        xdxr_events: [{date, songzhuan, rights, bonus}, ...]（按日期升序）
        end_date: 截止日期 YYYYMMDD（默认最末日）
    Returns:
        {fwd_factor: 前复权因子, bwd_factor: 后复权因子}
    """
    if not xdxr_events:
        return {"fwd_factor": 1.0, "bwd_factor": 1.0, "n_events": 0}
    if end_date:
        xdxr_events = [e for e in xdxr_events if str(e.get("date", "")) <= end_date]
    fwd = 1.0  # 前复权因子：累计复权（用于把历史价折算到今天）
    for e in xdxr_events:
        sz = float(e.get("songzhuan", 0) or 0)
        rights = float(e.get("rights", 0) or 0)
        float(e.get("bonus", 0) or 0)  # 分红
        fwd *= (1 + sz + rights)
    return {
        "fwd_factor": round(fwd, 6),
        "bwd_factor": round(1.0 / fwd, 6),
        "n_events": len(xdxr_events),
    }


# === MCP dispatcher ======================================================
TOOLS = {
    "calc_ma": {
        "description": "简单 / 指数移动均线",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prices": {"type": "array", "items": {"type": "number"}},
                "window": {"type": "integer", "default": 5},
                "type": {"type": "string", "enum": ["sma", "ema"], "default": "sma"},
            },
            "required": ["prices"],
        },
        "handler": lambda a: calc_ma(**a),
    },
    "calc_macd": {
        "description": "MACD 指标（含金叉标记）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prices": {"type": "array", "items": {"type": "number"}},
                "fast": {"type": "integer", "default": 12},
                "slow": {"type": "integer", "default": 26},
                "signal": {"type": "integer", "default": 9},
            },
            "required": ["prices"],
        },
        "handler": lambda a: calc_macd(**a),
    },
    "calc_kdj": {
        "description": "KDJ 随机指标",
        "inputSchema": {
            "type": "object",
            "properties": {
                "high": {"type": "array", "items": {"type": "number"}},
                "low": {"type": "array", "items": {"type": "number"}},
                "close": {"type": "array", "items": {"type": "number"}},
                "n": {"type": "integer", "default": 9},
            },
            "required": ["high", "low", "close"],
        },
        "handler": lambda a: calc_kdj(**a),
    },
    "calc_rsi": {
        "description": "RSI 相对强弱指标",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prices": {"type": "array", "items": {"type": "number"}},
                "n": {"type": "integer", "default": 14},
            },
            "required": ["prices"],
        },
        "handler": lambda a: calc_rsi(**a),
    },
    "calc_boll": {
        "description": "布林带（上/中/下轨）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prices": {"type": "array", "items": {"type": "number"}},
                "n": {"type": "integer", "default": 20},
                "k": {"type": "number", "default": 2},
            },
            "required": ["prices"],
        },
        "handler": lambda a: calc_boll(**a),
    },
    "calc_adj_factor": {
        "description": "复权因子计算（前/后）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "xdxr_events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "songzhuan": {"type": "number"},
                            "rights": {"type": "number"},
                            "bonus": {"type": "number"},
                        },
                    },
                    "description": "xdxr 事件列表（按日期升序）",
                },
                "end_date": {"type": "string"},
            },
            "required": ["xdxr_events"],
        },
        "handler": lambda a: calc_adj_factor(**a),
    },
}


def handle_request(req: dict) -> dict:
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-calc", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "tools": [{
                    "name": n,
                    "description": s["description"],
                    "inputSchema": s["inputSchema"],
                } for n, s in TOOLS.items()],
            }}
        if method == "tools/call":
            params = req.get("params", {})
            t_name = params.get("name")
            t_args = params.get("arguments", {})
            if t_name not in TOOLS:
                raise McpError(-32602, f"未知工具：{t_name}")
            t0 = datetime.now()
            result = TOOLS[t_name]["handler"](t_args)
            elapsed = (datetime.now() - t0).total_seconds() * 1000
            logging.info(f"{t_name} latency={elapsed:.1f}ms")
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
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}


def main():
    print("[mcp:calc] 已启动，stdio 模式", file=sys.stderr)
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
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            print(json.dumps(err), flush=True)


if __name__ == "__main__":
    main()
