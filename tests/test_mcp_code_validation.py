"""P0-3（KI-MCP-003）：MCP 工具入参 code 必须拒绝路径穿越。

`parquet_reader.py` 直接把调用方的 `code` 拼进 `DAILY_DIR / f"{code}.parquet"`，
历史写法无格式校验，`code="../../x"` 可越出数据目录读任意文件。修复点 `_norm_code`
只接受沪深 A 股 6 位代码（含裸 6 位补前缀），其余一律拒绝、且不触发任何磁盘读取。

不发网络、不读真实 parquet：用 importlib 加载模块 + monkeypatch pandas 守住「不读盘」。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "parquet_reader_under_test", ROOT / "mcp" / "servers" / "parquet_reader.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_norm_code_rejects_path_traversal():
    mod = _load()
    for bad in (
        "../../etc/passwd",
        "/etc/passwd",
        "sh600000/../x",
        "../x.parquet",
        "600000.txt",
        "sh6",
        "abc",
        "",
    ):
        try:
            mod._norm_code(bad)
            raise AssertionError(f"{bad!r} 应该被拒却通过了")
        except mod.McpError as e:
            assert e.code == -32602, f"错误码应为 -32602，实际 {e.code}"


def test_norm_code_accepts_valid():
    mod = _load()
    assert mod._norm_code("sh600000") == "sh600000"
    assert mod._norm_code("600000") == "sh600000"      # 裸 6 位，6 开头补 sh
    assert mod._norm_code("000001") == "sz000001"      # 0 开头补 sz
    assert mod._norm_code("300750") == "sz300750"      # 3 开头补 sz


def test_read_daily_kline_rejects_traversal_without_hitting_disk(monkeypatch):
    """非法 code 必须在路径拼装之前就被拦下，绝不触发任何 parquet 读取。"""
    mod = _load()
    calls: list = []

    def fake_read_parquet(*a, **k):
        calls.append(a)
        raise AssertionError("非法 code 不应触发磁盘读取")

    monkeypatch.setattr(mod.pd, "read_parquet", fake_read_parquet)
    try:
        mod.read_daily_kline("../../x")
        raise AssertionError("非法 code 应通过 McpError 被拒")
    except mod.McpError:
        pass
    assert calls == [], "非法 code 触发了磁盘读取，路径穿越未闭合"


def test_read_finance_rejects_traversal(monkeypatch):
    mod = _load()
    calls: list = []

    def fake_read_parquet(*a, **k):
        calls.append(a)
        raise AssertionError("不应读盘")

    monkeypatch.setattr(mod.pd, "read_parquet", fake_read_parquet)
    try:
        mod.read_finance("../../x")
        raise AssertionError("read_finance 应拒绝非法 code")
    except mod.McpError:
        pass
    assert calls == [], "非法 code 在 read_finance 里触发了读取"


def test_read_xdxr_rejects_traversal(monkeypatch):
    mod = _load()
    calls: list = []

    def fake_read_parquet(*a, **k):
        calls.append(a)
        raise AssertionError("不应读盘")

    monkeypatch.setattr(mod.pd, "read_parquet", fake_read_parquet)
    try:
        mod.read_xdxr("../../x")
        raise AssertionError("read_xdxr 应拒绝非法 code")
    except mod.McpError:
        pass
    assert calls == [], "非法 code 在 read_xdxr 里触发了读取"
