#!/usr/bin/env python3
"""
mcp/schema_validation.py — MCP 运行时结构化 schema 校验
======================================================

校验范围（覆盖 P1-5 要求：消息结构 / 字段类型 / 必填项 / 取值范围）：
    - validate_request : 入站 JSON-RPC 请求信封结构
    - validate_tool_args: 工具入参（字段类型 / 必填项 / 取值范围 / 枚举）
    - validate_response : 出站响应结构自检（仅告警，不阻断）

触发时机与处理流程（服务端 handle_request）：
    1. 每帧请求先过 validate_request（信封：jsonrpc==2.0 / method 非空 / params 为对象）。
    2. tools/call 在交给 handler 前，过 validate_tool_args（按工具的 inputSchema 校验）。
    3. 校验失败抛 McpValidationError(code, field, reason)，由 server 的 except 分支
       转成 JSON-RPC error（信封/未知方法 -32600 / 入参 -32602），消息含具体字段与原因。
    4. 出站响应在回写前过 validate_response，仅 logging.warning，不改写或丢弃。

依赖策略：jsonschema 不可用时（仅 requirements-ci 有），退化为内置轻量校验
（arguments 必须是 dict + 必填项非空），保证最坏情况下仍有基线防护、且不阻断启动。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# 吞错计数（SEM002）：依赖缺失退化必须留痕（键 "<module>" = 模块级 import 退化）
_ERROR_COUNT: Counter[str] = Counter()

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover - 依赖缺失时退化
    _ERROR_COUNT["<module>"] += 1
    jsonschema = None
    _HAS_JSONSCHEMA = False


class McpValidationError(Exception):
    """校验失败。code 默认 -32602（Invalid Params），信封级错误用 -32600。"""

    def __init__(self, field: str, reason: str, code: int = -32602):
        self.field = field
        self.reason = reason
        self.code = code
        super().__init__(f"[{code}] 字段 '{field}': {reason}")


def validate_request(req: Any) -> None:
    """校验 JSON-RPC 请求信封结构。失败抛 McpValidationError(code=-32600)。"""
    if not isinstance(req, dict):
        raise McpValidationError("<root>", "请求必须是 JSON 对象", code=-32600)
    if req.get("jsonrpc") != "2.0":
        raise McpValidationError("jsonrpc", "必须为 '2.0'", code=-32600)
    method = req.get("method")
    if not isinstance(method, str) or not method:
        raise McpValidationError("method", "必须是非空字符串", code=-32600)
    params = req.get("params")
    if params is not None and not isinstance(params, dict):
        raise McpValidationError("params", "必须是对象", code=-32600)


def validate_tool_args(tool_name: str, schema: dict, args: Any) -> None:
    """校验工具入参：dict 结构 / 必填项 / 字段类型 / 取值范围 / 枚举。

    无 jsonschema 时仅做必填+结构基线校验；有 jsonschema 时启用完整 schema 校验。
    失败抛 McpValidationError(code=-32602)，field 为出错路径，reason 为原因。
    """
    if not isinstance(args, dict):
        raise McpValidationError("<root>", "arguments 必须是对象", code=-32602)
    # 必填项基线（即使无 jsonschema 也生效）
    for field in schema.get("required", []):
        if field not in args or args.get(field) is None:
            raise McpValidationError(field, "必填项缺失", code=-32602)
    if not _HAS_JSONSCHEMA:
        return
    try:
        jsonschema.validate(instance=args, schema=schema)
    except jsonschema.ValidationError as e:
        field = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise McpValidationError(field, e.message, code=-32602) from e
    except jsonschema.SchemaError as e:  # 工具 inputSchema 自身写错
        raise McpValidationError(
            "<schema>", f"工具 {tool_name} 的 inputSchema 非法: {e.message}", code=-32603) from e


def validate_response(resp: Any) -> list[str]:
    """出站响应结构自检。返回问题列表（空 = OK）。仅告警用，不阻断。"""
    problems: list[str] = []
    if not isinstance(resp, dict):
        return ["响应必须是 JSON 对象"]
    if resp.get("jsonrpc") != "2.0":
        problems.append("jsonrpc 必须为 '2.0'")
    has_result = "result" in resp
    has_error = "error" in resp
    if not has_result and not has_error:
        problems.append("响应必须含 result 或 error")
        return problems
    if has_error:
        err = resp["error"]
        if not isinstance(err, dict) or "code" not in err or "message" not in err:
            problems.append("error 必须含 code 与 message")
    if has_result:
        result = resp["result"]
        if isinstance(result, dict) and "content" in result and not isinstance(result["content"], list):
            problems.append("result.content 必须是列表")
    return problems
