"""MCP 契约：schema 与实现同名同序；math 的 stdio/HTTP 工具表同源。"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

ROOT = Path(__file__).resolve().parents[1]

SERVER_MODULES = {
    "tdx-parquet-reader": "parquet_reader.py",
    "tdx-calc": "calc.py",
    "tdx-vault": "vault.py",
    "tdx-news": "news.py",
    "tdx-finance-deep": "finance_deep.py",
    "tdx-backtest": "backtest.py",
}


def _load(rel: str, name: str):
    """按文件路径加载独立模块（mcp/servers 不是包，避免 sys.path 互相打架）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dispatch_names(mod, func_name: str) -> list[str]:
    """抽取函数里所有 `name == "..."` 分支（if/elif 链，按源码顺序）。"""
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name)
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name) and node.left.id == "name"
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)):
            out.append(node.comparators[0].value)
    return out


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads((ROOT / "mcp" / "schemas" / "tools.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def servers() -> dict:
    return {key: _load(f"mcp/servers/{fn}", f"mcp_srv_{fn[:-3]}")
            for key, fn in SERVER_MODULES.items()}


@pytest.fixture(scope="module")
def math_specs():
    sys.path.insert(0, str(ROOT / "v5"))
    return importlib.import_module("mcp_servers.math_tool_specs")


def test_schema_totals(schema):
    assert len(schema["servers"]) == 6
    assert schema["tool_count"] == schema["total_tools"] == 23
    assert sum(len(s["tools"]) for s in schema["servers"].values()) == 23


def test_server_tools_match_schema(schema, servers):
    for key, mod in servers.items():
        declared = [t["name"] for t in schema["servers"][key]["tools"]]
        assert declared == list(mod.TOOLS), key


def test_each_tool_has_valid_schema(servers):
    for key, mod in servers.items():
        for name, spec in mod.TOOLS.items():
            assert spec["description"].strip(), (key, name)
            assert callable(spec["handler"]), (key, name)
            input_schema = spec["inputSchema"]
            validator_for(input_schema).check_schema(input_schema)
            assert set(input_schema.get("required", [])) <= set(input_schema.get("properties", {})), (key, name)


def test_calc_ma_over_jsonrpc(servers):
    resp = servers["tdx-calc"].handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "calc_ma", "arguments": {"prices": [1, 2, 3, 4, 5], "window": 2}},
    })
    assert "error" not in resp, resp.get("error")
    assert json.loads(resp["result"]["content"][0]["text"]) == [None, 1.5, 2.5, 3.5, 4.5]


def _calc(servers, name, arguments):
    resp = servers["tdx-calc"].handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert "error" not in resp, resp.get("error")
    return json.loads(resp["result"]["content"][0]["text"])


def test_calc_macd_flat_series(servers):
    out = _calc(servers, "calc_macd", {"prices": [5, 5, 5, 5, 5], "fast": 2, "slow": 3, "signal": 2})
    assert out["dif"] == [0.0] * 5
    assert out["hist"] == [0.0] * 5
    assert out["golden_cross"] == [0] * 5
    assert out["above_dea"] == [0] * 5


def test_calc_kdj_flat_series(servers):
    out = _calc(servers, "calc_kdj", {"high": [10] * 5, "low": [10] * 5, "close": [10] * 5, "n": 3})
    # 前 n-1 根是滚动窗口暖机期（NaN → null），其后平台序列的 RSV / K / D / J 恒为 0。
    assert out["k"] == [None, None, 0.0, 0.0, 0.0]
    assert out["d"] == [None, None, 0.0, 0.0, 0.0]
    assert out["j"] == [None, None, 0.0, 0.0, 0.0]


def test_calc_rsi_one_way_up(servers):
    out = _calc(servers, "calc_rsi", {"prices": [1, 2, 3, 4, 5], "n": 2})
    assert out[:2] == [None, None]
    assert all(abs(v - 100.0) < 1e-6 for v in out[2:])


def test_calc_boll_two_point_window(servers):
    out = _calc(servers, "calc_boll", {"prices": [1, 2, 3], "n": 2, "k": 2})
    band = 2 * math.sqrt(0.5)  # 样本标准差 ddof=1
    assert out["mid"][2] == 2.5
    assert abs(out["upper"][2] - (2.5 + band)) < 1e-9
    assert abs(out["lower"][2] - (2.5 - band)) < 1e-9


def test_calc_adj_factor_product(servers):
    out = _calc(servers, "calc_adj_factor", {"xdxr_events": [
        {"date": "20200101", "songzhuan": 0.5, "rights": 0, "bonus": 0.1},
        {"date": "20210101", "songzhuan": 0.0, "rights": 0.0, "bonus": 0.0},
    ]})
    assert out == {"fwd_factor": 1.5, "bwd_factor": 0.666667, "n_events": 2}


def test_calc_adj_factor_empty(servers):
    out = _calc(servers, "calc_adj_factor", {"xdxr_events": []})
    assert out == {"fwd_factor": 1.0, "bwd_factor": 1.0, "n_events": 0}


def test_tools_list_endpoint_matches_registry(servers):
    resp = servers["tdx-calc"].handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in resp["result"]["tools"]] == list(servers["tdx-calc"].TOOLS)


def test_math_specs_shape(math_specs):
    specs = math_specs.STDIO_TOOL_SPECS
    assert len(specs) == 20
    assert {tuple(s) for s in specs} == {("name", "description", "inputSchema")}


def test_math_http_is_stdio_plus_three(math_specs):
    # HTTP 现序里 3 个独有工具在下标 9..11（不是拼在末尾），所以这里只断集合相等。
    assert set(math_specs.HTTP_TOOL_NAMES) == (
        set(math_specs.STDIO_TOOL_NAMES) | set(math_specs.HTTP_ONLY_TOOL_NAMES)
    )
    assert len(math_specs.STDIO_TOOL_NAMES) == 20
    assert len(math_specs.HTTP_TOOL_NAMES) == 23
    assert math_specs.HTTP_ONLY_TOOL_NAMES == [
        "math_concept_link", "math_bilingual_translate", "math_ontology_stats",
    ]


def test_http_tool_order_is_pinned(math_specs):
    """HTTP /tools 的列举顺序是对外契约：23 名逐位钉死，3 个独有工具必须仍在下标 9..11。"""
    assert math_specs.HTTP_TOOL_NAMES == [
        "are_equiv",
        "sympy_compute",
        "math_error_report",
        "math_fix_typo",
        "math_validate_symbols",
        "math_cache_stats",
        "math_numeric_solve",
        "math_ast_dump",
        "math_batch_equiv",
        "math_concept_link",
        "math_bilingual_translate",
        "math_ontology_stats",
        "prove_equiv_with_precondition",
        "math_derivation_chain",
        "math_derivation_replay",
        "math_strategy_route",
        "math_strategy_route_and_execute",
        "solve_inequality",
        "simplify_piecewise",
        "math_step_extract",
        "math_derivation_cache_stats",
        "math_derivation_build_cached",
        "math_derivation_reverse",
    ]
    assert math_specs.HTTP_TOOL_NAMES[9:12] == math_specs.HTTP_ONLY_TOOL_NAMES


def test_math_dispatch_matches_names(math_specs):
    stdio = importlib.import_module("mcp_servers.math_sympy")
    http = importlib.import_module("mcp_servers.math_sympy_http")
    stdio_names = _dispatch_names(stdio, "call_tool")
    http_names = _dispatch_names(http, "_run_tool")
    assert set(stdio_names) == set(math_specs.STDIO_TOOL_NAMES) and len(stdio_names) == 20
    assert set(http_names) == set(math_specs.HTTP_TOOL_NAMES) and len(http_names) == 23
    assert http.TOOL_NAMES == math_specs.HTTP_TOOL_NAMES


def test_stdio_list_tools_is_generated():
    tree = ast.parse((ROOT / "v5" / "mcp_servers" / "math_sympy.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_tools")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "Tool"]
    assert len(calls) == 1
    assert calls[0].keywords and calls[0].keywords[0].arg is None, "list_tools 应由 **spec 生成"
