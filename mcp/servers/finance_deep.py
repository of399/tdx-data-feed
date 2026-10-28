#!/usr/bin/env python3
"""
mcp/servers/finance_deep.py — MCP Server: 财务深度数据
=======================================================

工具：
    1. get_metrics            财务指标（核心 300 读本地 finance.parquet，其余按需拉取）
    2. get_statement          三大报表（资产负债表 / 利润表 / 现金流量表）
    3. list_statement_fields  列出报表字段（随行业/公司类型变化）

数据源：data/parquet/finance.parquet + akshare.stock_financial_abstract
        + akshare.stock_financial_report_sina
磁盘缓存：data/parquet/finance_deep/{symbol}_{abstract|balance|income|cashflow}.jsonl
协议：MCP 2024-11-05（JSON-RPC over stdio）
"""

import json
import logging
import math
import os
import re
import socket
import sys
import time
import warnings
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("TQDM_DISABLE", "1")
warnings.filterwarnings("ignore")
socket.setdefaulttimeout(20)

LOG_FILE = Path("/home/jiuben/tdx-data-feed/logs/mcp.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [mcp:finance-deep] %(message)s",
)

ROOT = Path("/home/jiuben/tdx-data-feed")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import finance_deep_dir, finance_parquet  # noqa: E402

FIN_PARQUET = finance_parquet()
DEEP_DIR = finance_deep_dir()
DEEP_DIR.mkdir(parents=True, exist_ok=True)
UNIVERSE_FILE = ROOT / "data/all_a_stocks.json"
CACHE_TTL_DAYS = 7

STATEMENTS = {
    "balance": "资产负债表",
    "income": "利润表",
    "cashflow": "现金流量表",
    "资产负债表": "资产负债表",
    "利润表": "利润表",
    "现金流量表": "现金流量表",
}
STATEMENT_KEY = {"资产负债表": "balance", "利润表": "income", "现金流量表": "cashflow"}

PREFERRED_OPTIONS = {
    "常用指标": 0, "盈利能力": 1, "成长能力": 2, "每股指标": 3,
    "收益质量": 4, "财务风险": 5, "营运能力": 6,
}
DEFAULT_METRICS = [
    "营业总收入", "归母净利润", "扣非净利润", "基本每股收益",
    "净资产收益率(ROE)", "毛利率", "销售净利率", "资产负债率",
    "经营现金流量净额", "每股净资产",
]


class McpError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# === 代码 / 值清洗 =========================================================
def _norm_code(code: str) -> tuple[str, str]:
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


def _name_of(six: str) -> str:
    try:
        return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8")).get(six, "")
    except Exception:
        return ""


def _validate_on_demand(sym: str, six: str) -> None:
    """股票池可读且明确不含该代码时直接 404，避免 akshare 抛出难懂的 KeyError/TypeError。"""
    try:
        universe = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if universe and six not in universe:
        raise McpError(404, f"{sym} 不在股票池（data/all_a_stocks.json）")


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, str):
        s = v.strip()
        return None if s.lower() in ("", "nan", "none", "nat") else s
    return v


def _num(v):
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "--", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return v


def _norm_date(v) -> str:
    s = str(v).strip()
    if re.fullmatch(r"\d{8}", s):
        return s
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if not pd.isna(ts):
            return ts.strftime("%Y%m%d")
    except Exception:
        pass
    return s


def _ak():
    import akshare as ak
    return ak


# === 缓存读写 ==============================================================
def _paths(symbol: str, kind: str) -> tuple[Path, Path]:
    return (DEEP_DIR / f"{symbol}_{kind}.jsonl",
            DEEP_DIR / f"{symbol}_{kind}.meta.json")


def _read_cache(symbol: str, kind: str) -> tuple[list[dict], dict] | None:
    data_path, meta_path = _paths(symbol, kind)
    if not data_path.exists():
        return None
    try:
        rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return rows, meta
    except Exception as e:
        logging.warning("读取缓存 %s 失败：%s", data_path, e)
        return None


def _write_cache(symbol: str, kind: str, rows: list[dict], meta: dict) -> None:
    data_path, meta_path = _paths(symbol, kind)
    tmp = data_path.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    tmp.replace(data_path)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _fresh(meta: dict) -> bool:
    try:
        t = datetime.fromisoformat(meta["fetched_at"])
        return (datetime.now() - t).days < CACHE_TTL_DAYS
    except Exception:
        return False


# === 财务指标 ==============================================================
@lru_cache(maxsize=1)
def _local_finance(mtime: float) -> pd.DataFrame:
    if not FIN_PARQUET.exists():
        return pd.DataFrame(columns=["symbol", "report_date", "option", "metric", "value"])
    return pd.read_parquet(FIN_PARQUET)


def _local_df() -> pd.DataFrame:
    return _local_finance(FIN_PARQUET.stat().st_mtime if FIN_PARQUET.exists() else 0)


def _abstract_from_akshare(symbol: str, six: str, refresh: bool) -> tuple[pd.DataFrame, str, str]:
    cached = None if refresh else _read_cache(symbol, "abstract")
    if cached and _fresh(cached[1]):
        rows, meta = cached
        return pd.DataFrame(rows), "cache", meta.get("fetched_at", "")
    if cached:
        stale_rows, stale_meta = cached
    else:
        stale_rows, stale_meta = [], {}

    _validate_on_demand(symbol, six)
    try:
        df = _ak().stock_financial_abstract(symbol=six)
        if df is None or df.empty or "指标" not in df.columns:
            raise RuntimeError("akshare 返回空表")
        date_cols = [c for c in df.columns if re.fullmatch(r"\d{8}", str(c))]
        date_cols.sort(reverse=True)
        rows = []
        for r in df.to_dict("records"):
            option = _clean(r.get("选项")) or ""
            metric = _clean(r.get("指标")) or ""
            if not metric:
                continue
            for d in date_cols:
                val = _num(r.get(d))
                if val is None:
                    continue
                rows.append({"report_date": d, "option": option,
                             "metric": metric, "value": val})
        if not rows:
            raise RuntimeError("akshare 返回的指标为空")
        fetched_at = datetime.now().isoformat(timespec="seconds")
        meta = {"symbol": symbol, "kind": "abstract", "source": "akshare.stock_financial_abstract",
                "fetched_at": fetched_at, "rows": len(rows),
                "metrics": len({r["metric"] for r in rows})}
        _write_cache(symbol, "abstract", rows, meta)
        logging.info("abstract 拉取 %s rows=%s", symbol, len(rows))
        return pd.DataFrame(rows), "akshare", fetched_at
    except Exception as e:
        if stale_rows:
            logging.warning("abstract 拉取失败，使用过期缓存 %s：%s", symbol, e)
            return pd.DataFrame(stale_rows), "stale_cache", stale_meta.get("fetched_at", "")
        raise McpError(-32603, f"{symbol} 财务指标拉取失败：{type(e).__name__}: {e}") from e


def _select_metrics(available: list[str], metrics) -> list[str]:
    if metrics is None:
        selected = [m for m in DEFAULT_METRICS if m in available]
        if not selected:
            selected = available[:15]
        return selected
    if isinstance(metrics, str):
        metrics = [metrics]
    if not metrics or any(str(m).strip() in ("*", "all", "全部") for m in metrics):
        return available
    selected: list[str] = []
    for pat in metrics:
        pat = str(pat).strip()
        if not pat:
            continue
        if pat in available:
            selected.append(pat)
            continue
        matched = [m for m in available if pat in m]
        if not matched:
            raise McpError(-32602, f"指标未找到：{pat}；可用示例：{', '.join(available[:12])}")
        selected.extend(matched)
    # 去重保序
    return list(dict.fromkeys(selected))


def _yoy(values: dict[str, float], rd: str) -> float | None:
    if len(rd) != 8:
        return None
    prev = values.get(f"{int(rd[:4]) - 1}{rd[4:]}")
    cur = values.get(rd)
    if prev is None or cur is None or prev == 0 or prev < 0:
        return None
    return round((cur / prev - 1) * 100, 2)


def get_metrics(code: str, metrics=None, periods: int = 8,
                refresh: bool = False) -> dict:
    """查询财务指标。核心 300 读本地 parquet；其他标的按需拉取并缓存 7 天。"""
    sym, six = _norm_code(code)
    periods = max(1, min(int(periods), 40))

    local = _local_df()
    rows = local[local["symbol"] == sym].copy() if "symbol" in local.columns else pd.DataFrame()
    source, fetched_at = "finance.parquet", ""
    if rows.empty:
        rows, source, fetched_at = _abstract_from_akshare(sym, six, refresh)
        source = f"tdx-finance-deep:{source}"
    else:
        rows = rows.copy()

    if rows.empty:
        raise McpError(404, f"{sym} 无财务指标数据")

    rows["report_date"] = rows["report_date"].astype(str)
    available = sorted(rows["metric"].dropna().astype(str).unique().tolist())
    selected = _select_metrics(available, metrics)

    # 同一报告期/指标可能有多个口径，优先常用指标
    rows["_pref"] = rows.get("option", pd.Series(index=rows.index, dtype=object)) \
        .map(PREFERRED_OPTIONS).fillna(9)
    all_dates = sorted(rows["report_date"].unique(), reverse=True)
    want_dates = all_dates[:periods]
    data = []
    for metric in selected:
        sub = rows[rows["metric"] == metric].sort_values(["report_date", "_pref"],
                                                         ascending=[False, True])
        sub = sub.drop_duplicates(["report_date"], keep="first")
        by_date = {str(r["report_date"]): _num(r["value"])
                   for r in sub.to_dict("records") if _num(r["value"]) is not None}
        if not by_date:
            continue
        values = []
        for rd in want_dates:
            if rd not in by_date:
                continue
            values.append({"period_end": rd, "value": by_date[rd], "yoy_pct": _yoy(by_date, rd)})
        if values:
            opt = sub[sub["report_date"] == values[0]["period_end"]]["option"]
            data.append({
                "metric": metric,
                "option": _clean(opt.iloc[0]) if len(opt) else None,
                "values": values,
            })

    if not data:
        raise McpError(404, f"{sym} 未找到指定指标：{metrics}")
    report_dates = sorted({v["period_end"] for m in data for v in m["values"]}, reverse=True)
    return {
        "ok": True,
        "code": sym,
        "name": _name_of(six),
        "source": source,
        "fetched_at": fetched_at,
        "as_of": report_dates[0] if report_dates else None,
        "periods": report_dates,
        "data": data,
        "metric_count": len(data),
        "coverage": "local_core" if source == "finance.parquet" else "on_demand",
        "note": ("核心 300 只来自 data/parquet/finance.parquet；其余标的首次查询时从 "
                 "akshare.stock_financial_abstract 拉取并缓存 7 天"),
    }


# === 三大报表 ==============================================================
def _statement_from_akshare(symbol: str, six: str, key: str,
                            refresh: bool) -> tuple[list[dict], str, str, list[str]]:
    cached = None if refresh else _read_cache(symbol, key)
    if cached and _fresh(cached[1]):
        rows, meta = cached
        return rows, "cache", meta.get("fetched_at", ""), meta.get("columns", [])
    stale = cached
    _validate_on_demand(symbol, six)
    try:
        cn = STATEMENTS[key]
        df = _ak().stock_financial_report_sina(stock=symbol, symbol=cn)
        if df is None or df.empty:
            raise RuntimeError("akshare 返回空表")
        if "报告日" in df.columns:
            df = df.rename(columns={"报告日": "report_date"})
        elif "report_date" not in df.columns:
            raise RuntimeError(f"未找到报告日列，实际列：{list(df.columns)[:8]}")
        df["report_date"] = df["report_date"].map(_norm_date)
        df = df.drop_duplicates("report_date", keep="first")
        df = df.sort_values("report_date", ascending=False)
        columns = [c for c in df.columns if c != "report_date"]
        rows = [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]
        fetched_at = datetime.now().isoformat(timespec="seconds")
        meta = {"symbol": symbol, "kind": key, "statement": cn,
                "source": "akshare.stock_financial_report_sina",
                "fetched_at": fetched_at, "rows": len(rows), "columns": columns}
        _write_cache(symbol, key, rows, meta)
        logging.info("statement 拉取 %s/%s rows=%s fields=%s",
                     symbol, key, len(rows), len(columns))
        return rows, "akshare", fetched_at, columns
    except Exception as e:
        if stale:
            logging.warning("报表拉取失败，使用过期缓存 %s/%s：%s", symbol, key, e)
            rows, meta = stale
            return rows, "stale_cache", meta.get("fetched_at", ""), meta.get("columns", [])
        raise McpError(-32603, f"{symbol} {STATEMENTS[key]}拉取失败：{type(e).__name__}: {e}") from e


def _match_fields(all_fields: list[str], fields) -> list[str]:
    if fields is None or fields == "":
        return list(all_fields)
    if isinstance(fields, str):
        fields = [fields]
    if any(str(f).strip() in ("*", "all", "全部") for f in fields):
        return list(all_fields)
    selected: list[str] = []
    for pat in fields:
        pat = str(pat).strip()
        if not pat:
            continue
        if pat in all_fields:
            selected.append(pat)
        else:
            matched = [c for c in all_fields if pat in c]
            if not matched:
                raise McpError(-32602,
                               f"字段未找到：{pat}；可用字段示例：{', '.join(all_fields[:15])}")
            selected.extend(matched)
    return list(dict.fromkeys(selected))


def get_statement(code: str, statement: str = "balance", periods: int = 8,
                  fields=None, refresh: bool = False) -> dict:
    """查询三大报表。默认返回最近 8 期全部字段。"""
    sym, six = _norm_code(code)
    if statement not in STATEMENTS:
        raise McpError(-32602, f"未知报表：{statement}；可选：balance / income / cashflow")
    key = STATEMENT_KEY[STATEMENTS[statement]]
    periods = max(1, min(int(periods), 40))
    rows, source, fetched_at, all_fields = _statement_from_akshare(sym, six, key, refresh)
    selected = _match_fields(all_fields, fields)
    data = []
    for r in rows[:periods]:
        item = {"report_date": r.get("report_date")}
        for f in selected:
            item[f] = r.get(f)
        data.append(item)
    return {
        "ok": True,
        "code": sym,
        "name": _name_of(six),
        "statement": STATEMENTS[key],
        "statement_key": key,
        "source": source,
        "fetched_at": fetched_at,
        "periods": len(data),
        "report_dates": [d["report_date"] for d in data],
        "fields": selected,
        "field_count": len(selected),
        "data": data,
        "note": "字段随行业/公司类型变化；可用 list_statement_fields 查看完整字段",
    }


def list_statement_fields(statement: str = "balance", code: str = "sh600000",
                          refresh: bool = False) -> dict:
    """列出某张报表的字段（默认用 sh600000 样本）。"""
    if statement not in STATEMENTS:
        raise McpError(-32602, f"未知报表：{statement}；可选：balance / income / cashflow")
    sym, six = _norm_code(code)
    key = STATEMENT_KEY[STATEMENTS[statement]]
    rows, source, fetched_at, fields = _statement_from_akshare(sym, six, key, refresh)
    sample_date = rows[0].get("report_date") if rows else None
    return {
        "ok": True,
        "code": sym,
        "statement": STATEMENTS[key],
        "statement_key": key,
        "source": source,
        "fetched_at": fetched_at,
        "sample_report_date": sample_date,
        "field_count": len(fields),
        "fields": fields,
        "note": "字段随行业/公司类型变化；换 code 可查看该公司实际字段",
    }


# === MCP dispatcher ========================================================
TOOLS = {
    "get_metrics": {
        "description": "查询财务指标（核心 300 本地，其余按需拉取；支持指标名模糊匹配）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码，如 sh600000 / 600000"},
                "metrics": {
                    "description": "指标名、列表或 '*'（默认核心 10 项）",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "periods": {"type": "integer", "default": 8, "description": "最近 N 期（1-40）"},
                "refresh": {"type": "boolean", "default": False, "description": "跳过 7 天缓存"},
            },
            "required": ["code"],
        },
        "handler": lambda a: get_metrics(**a),
    },
    "get_statement": {
        "description": "查询三大报表（资产负债表 / 利润表 / 现金流量表）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "沪深代码，如 sh600000 / 600000"},
                "statement": {"type": "string", "default": "balance",
                              "enum": ["balance", "income", "cashflow",
                                       "资产负债表", "利润表", "现金流量表"]},
                "periods": {"type": "integer", "default": 8, "description": "最近 N 期（1-40）"},
                "fields": {
                    "description": "字段名/关键词/列表；缺省返回全部字段",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "refresh": {"type": "boolean", "default": False, "description": "跳过 7 天缓存"},
            },
            "required": ["code"],
        },
        "handler": lambda a: get_statement(**a),
    },
    "list_statement_fields": {
        "description": "列出报表字段（用于发现可用字段）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "default": "balance",
                              "enum": ["balance", "income", "cashflow",
                                       "资产负债表", "利润表", "现金流量表"]},
                "code": {"type": "string", "default": "sh600000",
                         "description": "样本代码（银行与制造业字段不同）"},
                "refresh": {"type": "boolean", "default": False},
            },
        },
        "handler": lambda a: list_statement_fields(**a),
    },
}


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "tdx-finance-deep", "version": "0.1.0"},
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
    print("[mcp:finance-deep] 已启动，stdio 模式（finance.parquet + akshare）", file=sys.stderr)
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
