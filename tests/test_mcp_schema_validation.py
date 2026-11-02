"""P1-5：MCP 运行时结构化 schema 校验。

验证 `mcp/schema_validation.py` 在 server 端 `parquet_reader.handle_request` 的接入：
- 入站信封校验（jsonrpc / method / params 结构）→ -32600
- 工具入参校验（必填项 / 字段类型 / 枚举 / 取值范围）→ -32602，消息含字段与原因
- 非法参数不再变成 -32603（拼错参数名等问题在入口被拦下）
- 新增 ping 方法
- 出站响应结构自检函数 validate_response

不发网络、不读真实 parquet：用 importlib 加载模块 + monkeypatch 守住「不读盘」。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "parquet_reader_under_test", ROOT / "mcp" / "servers" / "parquet_reader.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 入站信封校验 → -32600
# ---------------------------------------------------------------------------
def test_envelope_missing_jsonrpc():
    mod = _load()
    resp = mod.handle_request({"method": "tools/list"})
    assert resp["error"]["code"] == -32600
    assert "jsonrpc" in resp["error"]["message"]


def test_envelope_bad_method_type():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "method": 123})
    assert resp["error"]["code"] == -32600
    assert "method" in resp["error"]["message"]


def test_envelope_missing_method():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0"})
    assert resp["error"]["code"] == -32600


def test_envelope_params_not_object():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "method": "tools/call", "params": "x"})
    assert resp["error"]["code"] == -32600
    assert "params" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# 工具入参校验 → -32602（含字段与原因）
# ---------------------------------------------------------------------------
def test_unknown_tool():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "nope", "arguments": {}}})
    assert resp["error"]["code"] == -32602
    assert "未知工具" in resp["error"]["message"]


def test_missing_required_code():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "read_daily_kline", "arguments": {"limit": 5}}})
    assert resp["error"]["code"] == -32602
    assert "code" in resp["error"]["message"]
    assert "必填" in resp["error"]["message"]


def test_wrong_type_limit():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "read_daily_kline",
                                          "arguments": {"code": "sh600000", "limit": "abc"}}})
    assert resp["error"]["code"] == -32602
    assert "limit" in resp["error"]["message"]


def test_enum_violation_market():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "list_codes", "arguments": {"market": "xx"}}})
    assert resp["error"]["code"] == -32602
    assert "market" in resp["error"]["message"]


def test_range_periods_too_small():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "read_finance", "arguments": {"periods": 0}}})
    assert resp["error"]["code"] == -32602
    assert "periods" in resp["error"]["message"]


def test_range_periods_too_large():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "read_finance", "arguments": {"periods": 999}}})
    assert resp["error"]["code"] == -32602
    assert "periods" in resp["error"]["message"]


def test_valid_call_passes(monkeypatch):
    """合法入参不应被误杀（无假阳性）。"""
    mod = _load()
    df = pd.DataFrame({
        "date": ["2026-01-01"], "open": [10.0], "high": [10.0], "low": [10.0],
        "close": [10.0], "volume": [1.0], "amount": [1.0],
    })

    def fake_read(code, mtime):
        return df

    monkeypatch.setattr(mod, "_read_daily_cached", fake_read)
    resp = mod.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "read_daily_kline", "arguments": {"code": "sh600000", "limit": 5}},
    })
    assert "error" not in resp, resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["code"] == "sh600000"
    assert payload["rows"] == 1


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------
def test_ping():
    mod = _load()
    resp = mod.handle_request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert "error" not in resp
    assert resp["result"] == {}


# ---------------------------------------------------------------------------
# 出站响应结构自检（validate_response）
# ---------------------------------------------------------------------------
def test_validate_response_ok():
    mod = _load()
    assert mod.validate_response({"jsonrpc": "2.0", "id": 1, "result": {}}) == []
    assert mod.validate_response({"jsonrpc": "2.0", "id": 1,
                                  "error": {"code": -1, "message": "x"}}) == []


def test_validate_response_problems():
    mod = _load()
    probs = mod.validate_response({"jsonrpc": "1.0"})
    assert any("jsonrpc" in p for p in probs)
    probs = mod.validate_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}})
    assert any("error" in p for p in probs)
    assert any("message" in p for p in probs)
