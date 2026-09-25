"""
v5/mcp_servers/math_sympy_http.py 验收测试（D6.6）

测试 HTTP 端点：
  - GET  /health
  - GET  /
  - POST /tools/{tool_name}（9 个 Tool）
  - POST /batch（批量）

需要 math-sympy-http 服务在 127.0.0.1:8002 运行。
"""

import asyncio
import json
import sys
import time

try:
    import httpx
except ImportError:
    print("httpx 未装")
    sys.exit(1)

BASE = "http://127.0.0.1:8002"
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def main():
    print("=== math-sympy-http (D6.6 持久化 server) 验收测试 ===\n")

    async with httpx.AsyncClient(base_url=BASE, timeout=10) as c:
        # 1. /health
        print("[1] GET /health")
        r = await c.get("/health")
        check("200 OK", r.status_code == 200, str(r.status_code))
        data = r.json()
        check("status=ok", data.get("status") == "ok")
        check("tool_count=12", data.get("tool_count") == 12)
        print(f"   {data}")

        # 2. /
        print("\n[2] GET /")
        r = await c.get("/")
        data = r.json()
        check("service=math-sympy-http", "math-sympy-http" in data.get("service", ""))
        check("有 12 个 tools", len(data.get("tools", [])) == 12)
        print(f"   uptime: {data.get('uptime_s')}s")

        # 3. 9 个 Tool 端点
        print("\n[3] POST /tools/{tool_name}")
        # 用 cache-friendly 输入（确保命中）
        cases = [
            ("are_equiv", {"latex_a": "x + 1", "latex_b": "1 + x"}),
            ("sympy_compute", {"latex": "x^2 + 2*x + 1", "action": "factor"}),
            ("math_error_report", {"latex": r"\foo", "error_message": "could not parse"}),
            ("math_fix_typo", {"latex": r"\FRAC{1}{2}"}),
            ("math_validate_symbols", {"latex": "x^2 + y", "declared": ["x", "y", "w"]}),
            ("math_cache_stats", {}),
            ("math_numeric_solve", {"latex": "x^3 - 2", "action": "solve"}),
            ("math_ast_dump", {"latex": r"\frac{1}{2}"}),
        ]
        for name, args in cases:
            t0 = time.time()
            r = await c.post(f"/tools/{name}", json=args)
            elapsed_ms = (time.time() - t0) * 1000
            check(f"{name} 200", r.status_code == 200, str(r.status_code))
            data = r.json()
            check(f"{name} 含 _tool", data.get("_tool") == name)
            check(
                f"{name} < 50ms（实测 {elapsed_ms:.1f}ms）", elapsed_ms < 50, f"{elapsed_ms:.1f}ms"
            )
            print(f"   {name:<25} {elapsed_ms:6.1f} ms")

        # 4. /batch
        print("\n[4] POST /batch（一次性 5 个 Tool 调用）")
        r = await c.post(
            "/batch",
            json={
                "calls": [
                    {"tool": "are_equiv", "arguments": {"latex_a": "a+b", "latex_b": "b+a"}},
                    {"tool": "math_cache_stats", "arguments": {}},
                    {"tool": "math_fix_typo", "arguments": {"latex": r"\sqrt{x"}},
                    {
                        "tool": "math_validate_symbols",
                        "arguments": {"latex": "a*b", "declared": ["a", "b"]},
                    },
                    {"tool": "math_ast_dump", "arguments": {"latex": r"\int x dx"}},
                ],
            },
        )
        check("200 OK", r.status_code == 200)
        data = r.json()
        check("返回 5 个结果", len(data.get("results", [])) == 5)
        # 每个 result 都要有 _tool 或 error
        for res in data.get("results", []):
            ok = "_tool" in res or "error" in res
            check("  result 含 _tool/error", ok)

        # 5. 未知 Tool 404
        print("\n[5] POST /tools/unknown_tool → 404")
        r = await c.post("/tools/unknown_tool", json={})
        check("404", r.status_code == 404)

        # 6. 性能基准
        print("\n[6] 性能基准（连续 10 次 are_equiv）")
        times = []
        for _ in range(10):
            t0 = time.time()
            await c.post("/tools/are_equiv", json={"latex_a": "a+b", "latex_b": "b+a"})
            times.append((time.time() - t0) * 1000)
        times.sort()
        p50 = times[len(times) // 2]
        p95 = times[int(len(times) * 0.95)]
        print(f"   p50: {p50:.1f}ms | p95: {p95:.1f}ms | max: {times[-1]:.1f}ms")
        check("p50 < 50ms", p50 < 50, f"p50={p50:.1f}ms")
        check("p95 < 100ms", p95 < 100, f"p95={p95:.1f}ms")

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} pass / {FAIL} fail")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
