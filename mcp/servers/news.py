#!/usr/bin/env python3
"""
mcp/servers/news.py — MCP Server: 实时新闻 / 公告检索
=====================================================

工具：
    1. search_stock_news     个股新闻（东财，优先按股票名称检索）
    2. search_announcements  个股公告（巨潮资讯）
    3. market_news           市场要闻（财新，失败时回退新闻联播）

数据源：akshare（东财 / 巨潮 / 财新），进程内 5 分钟缓存。
协议：MCP 2024-11-05（JSON-RPC over stdio）
"""

import json
import logging
import re
import socket
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
socket.setdefaulttimeout(20)

ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = ROOT / "logs" / "mcp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:news] %(message)s",
)

UNIVERSE_FILE = ROOT / "data/all_a_stocks.json"
CACHE_TTL_SEC = 300


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# === 代码与名称 ============================================================
def _norm_code(code: str) -> tuple[str, str]:
    """返回 (symbol, six)。仅接受沪深 A 股。"""
    s = str(code or "").strip().lower().replace(".", "")
    if re.fullmatch(r"(sh|sz)\d{6}", s):
        sym, six = s, s[2:]
    elif re.fullmatch(r"\d{6}", s):
        six = s
        if six.startswith("6"):
            sym = "sh" + six
        elif six.startswith(("0", "3")):
            sym = "sz" + six
        else:
            raise McpError(-32602, f"仅支持沪深 A 股代码，收到：{code}")
    else:
        raise McpError(-32602, f"非法代码：{code}（示例 sh600000 / 600000）")
    return sym, six


def _universe() -> dict[str, str]:
    try:
        return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("读取股票池失败：%s", e)
        return {}


# === 缓存 ==================================================================
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _cached(key: tuple, producer, refresh: bool) -> tuple[dict, bool]:
    now = time.time()
    if not refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1], True
    data = producer()
    with _CACHE_LOCK:
        _CACHE[key] = (now, data)
    return data, False


def _ak():
    import akshare as ak
    return ak


def _parse_time(value) -> datetime | None:
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:
        return None


def _clean_text(value, limit: int = 600) -> str:
    if value is None:
        return ""
    s = re.sub(r"\s+", " ", str(value)).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    return s[:limit]


# === 工具实现 ==============================================================
def search_stock_news(code: str, days: int = 7, limit: int = 20,
                      refresh: bool = False) -> dict:
    """个股新闻。优先用股票名称检索，结果更相关；名称无结果时回退代码。"""
    sym, six = _norm_code(code)
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    universe = _universe()
    if universe and six not in universe:
        raise McpError(404, f"{sym} 不在股票池（data/all_a_stocks.json）")
    name = re.sub(r"\s+", "", universe.get(six, ""))
    as_of = datetime.now()

    def produce() -> dict:
        queries = [q for q in (name, six) if q]
        last_err = ""
        rows: list[dict] = []
        match_mode = ""
        for q in queries:
            try:
                df = _ak().stock_news_em(symbol=q)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logging.warning("stock_news_em(%s) 失败：%s", q, e)
                continue
            if df is None or df.empty:
                continue
            raw = []
            cutoff = as_of - timedelta(days=days)
            for r in df.to_dict("records"):
                title = _clean_text(r.get("新闻标题"), 200)
                if not title:
                    continue
                published = _parse_time(r.get("发布时间"))
                if published is not None and published < cutoff:
                    continue
                content = _clean_text(r.get("新闻内容"), 600)
                raw.append({
                    "kind": "news",
                    "title": title,
                    "content": content,
                    "text": f"{title}\n{content}".strip(),
                    "publish_time": published.strftime("%Y-%m-%d %H:%M:%S") if published else None,
                    "source": _clean_text(r.get("文章来源"), 80),
                    "url": _clean_text(r.get("新闻链接"), 500),
                })
            if raw:
                rows = raw
                match_mode = "name" if q == name else "code"
                break
        # 去重 + 排序 + 截断
        seen, dedup = set(), []
        for r in sorted(rows, key=lambda x: x["publish_time"] or "", reverse=True):
            if r["title"] in seen:
                continue
            seen.add(r["title"])
            dedup.append(r)
        items = dedup[:limit]
        return {
            "ok": bool(items) or not last_err,
            "code": sym,
            "name": name,
            "keyword": name or six,
            "match_mode": match_mode or None,
            "window_days": days,
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
            "count": len(items),
            "source": "eastmoney.stock_news_em",
            "error": None if items else (last_err or f"近 {days} 天无个股新闻"),
        }

    data, cached = _cached(("stock_news", sym, days, limit), produce, refresh)
    data["cached"] = cached
    logging.info("search_stock_news %s days=%s items=%s cached=%s",
                 sym, days, data.get("count"), cached)
    return data


def search_announcements(code: str, days: int = 30, limit: int = 20,
                         refresh: bool = False) -> dict:
    """个股公告（巨潮资讯）。"""
    sym, six = _norm_code(code)
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    universe = _universe()
    if universe and six not in universe:
        raise McpError(404, f"{sym} 不在股票池（data/all_a_stocks.json）")
    name = re.sub(r"\s+", "", universe.get(six, ""))
    today = datetime.now()

    def produce() -> dict:
        start = (today - timedelta(days=days)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        try:
            df = _ak().stock_zh_a_disclosure_report_cninfo(
                symbol=six, market="沪深京", start_date=start, end_date=end)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            logging.warning("cninfo %s 失败：%s", sym, e)
            return {"ok": False, "code": sym, "name": name, "items": [], "count": 0,
                    "window_days": days, "as_of": today.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "cninfo.disclosure", "error": msg}
        items, seen = [], set()
        if df is not None and not df.empty:
            for r in df.to_dict("records"):
                title = _clean_text(r.get("公告标题"), 300)
                if not title or title in seen:
                    continue
                seen.add(title)
                dt = _parse_time(r.get("公告时间"))
                items.append({
                    "kind": "announcement",
                    "title": title,
                    "content": "",
                    "text": title,
                    "publish_time": dt.strftime("%Y-%m-%d") if dt else None,
                    "source": _clean_text(r.get("简称"), 80) or "巨潮资讯",
                    "url": _clean_text(r.get("公告链接"), 500),
                })
        items.sort(key=lambda x: x["publish_time"] or "", reverse=True)
        items = items[:limit]
        return {
            "ok": True,
            "code": sym,
            "name": name,
            "window_days": days,
            "as_of": today.strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
            "count": len(items),
            "source": "cninfo.disclosure",
            "error": None if items else f"近 {days} 天无公告",
        }

    data, cached = _cached(("announcements", sym, days, limit), produce, refresh)
    data["cached"] = cached
    logging.info("search_announcements %s days=%s items=%s cached=%s",
                 sym, days, data.get("count"), cached)
    return data


def market_news(limit: int = 20, refresh: bool = False) -> dict:
    """市场要闻。财新为主，失败时回退前一交易日新闻联播。"""
    limit = max(1, min(int(limit), 100))

    def produce() -> dict:
        try:
            df = _ak().stock_news_main_cx()
            items = []
            if df is not None and not df.empty:
                for r in df.to_dict("records"):
                    summary = _clean_text(r.get("summary"), 400)
                    if not summary:
                        continue
                    items.append({
                        "kind": "market_news",
                        "title": summary[:80],
                        "content": summary,
                        "text": summary,
                        "publish_time": None,
                        "tag": _clean_text(r.get("tag"), 40),
                        "source": "财新",
                        "url": _clean_text(r.get("url"), 500),
                    })
            if items:
                return {"ok": True, "items": items[:limit], "count": min(len(items), limit),
                        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "caixin.stock_news_main_cx", "error": None}
        except Exception as e:
            logging.warning("stock_news_main_cx 失败：%s", e)
        # 回退：前一交易日新闻联播
        try:
            day = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            df = _ak().news_cctv(date=day)
            items = []
            if df is not None and not df.empty:
                for r in df.to_dict("records"):
                    title = _clean_text(r.get("title"), 200)
                    content = _clean_text(r.get("content"), 600)
                    if not title:
                        continue
                    items.append({
                        "kind": "market_news",
                        "title": title,
                        "content": content,
                        "text": f"{title}\n{content}".strip(),
                        "publish_time": _clean_text(r.get("date"), 20),
                        "tag": "新闻联播",
                        "source": "央视新闻联播",
                        "url": "",
                    })
            return {"ok": bool(items), "items": items[:limit], "count": min(len(items), limit),
                    "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "cctv.news_cctv",
                    "error": None if items else "市场新闻源均不可用"}
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            logging.warning("market_news 全部失败：%s", e)
            return {"ok": False, "items": [], "count": 0,
                    "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": None, "error": msg}

    data, cached = _cached(("market_news", limit), produce, refresh)
    data["cached"] = cached
    logging.info("market_news items=%s cached=%s", data.get("count"), cached)
    return data


# === MCP dispatcher ========================================================
TOOLS = {
    "search_stock_news": {
        "description": "检索个股近期新闻（东财；优先按股票名称匹配）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码，如 sh600000 / 600000"},
                "days": {"type": "integer", "default": 7, "description": "回看天数（1-365）"},
                "limit": {"type": "integer", "default": 20, "description": "最多返回条数（1-100）"},
                "refresh": {"type": "boolean", "default": False, "description": "跳过 5 分钟缓存"},
            },
            "required": ["code"],
        },
        "handler": lambda a: search_stock_news(**a),
    },
    "search_announcements": {
        "description": "检索个股近期公告（巨潮资讯）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码，如 sh600000 / 600000"},
                "days": {"type": "integer", "default": 30, "description": "回看天数（1-365）"},
                "limit": {"type": "integer", "default": 20, "description": "最多返回条数（1-100）"},
                "refresh": {"type": "boolean", "default": False, "description": "跳过 5 分钟缓存"},
            },
            "required": ["code"],
        },
        "handler": lambda a: search_announcements(**a),
    },
    "market_news": {
        "description": "市场要闻（财新；失败时回退新闻联播）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "description": "最多返回条数（1-100）"},
                "refresh": {"type": "boolean", "default": False, "description": "跳过 5 分钟缓存"},
            },
        },
        "handler": lambda a: market_news(**a),
    },
}


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-news", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "tools": [{"name": n, "description": s["description"],
                           "inputSchema": s["inputSchema"]} for n, s in TOOLS.items()],
            }}
        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name not in TOOLS:
                raise McpError(-32602, f"未知工具：{name}")
            t0 = time.time()
            result = TOOLS[name]["handler"](args)
            logging.info("tool=%s latency=%.1fms", name, (time.time() - t0) * 1000)
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, default=str)}],
            }}
        if method == "notifications/initialized":
            return None
        raise McpError(-32601, f"未知方法：{method}")
    except McpError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        logging.exception("handler error")
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)}}


def main():
    print("[mcp:news] 已启动，stdio 模式（东财/巨潮/财新）", file=sys.stderr)
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
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    main()
