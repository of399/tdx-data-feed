"""
v5/mcp_servers/math_sympy_http.py · ask 路径 e2e 测试 (P2.2)

目的: 验证 fallback chain 第二层 (ask) 能真被触发，避免
       fallback_attempts_total{strategy="ask"} 永远 absent 触发误报。

依赖: math-sympy-http 在 127.0.0.1:8002 运行。
       Ollama 可选（不会真用到，因为 ask 失败后被 numeric 兜底）。

运行:
    venv/bin/pytest v5/tests/test_ask_path_integration.py -v -s
    # 跑一次约 5s，跑 5 个 parametrize case ≈ 25s

参考: v5/audit/2026-09-20-alerting-pipeline-verification.md §5.1
"""

import json
import urllib.error
import urllib.request

import pytest

API = "http://127.0.0.1:8002"
ASK_STRATEGY = 'fallback_attempts_total{strategy="ask"}'

# 已知能触发 ask 路径的测试输入（score ∈ [30, 55) → primary=ask → chain 必含 ask）
# 详见 v5/mcp_math/strategy_selector.py:51 THRESHOLD_ASK=30 / THRESHOLD_NUMERIC=55
ASK_PATH_CASES = [
    pytest.param(
        "x^{x^{x}}",  # nested exponential: high_power + depth → 35.5
        "solve",
        "x",
        id="nested-exponential-solve",
    ),
    pytest.param(
        "\\Gamma(x+1)",  # special_func → 30.5
        "simplify",
        "x",
        id="gamma-xplus1-simplify",
    ),
    pytest.param(
        "\\Gamma(x)",  # special_func + limit bonus → 39.5
        "limit",
        "x",
        id="gamma-x-limit",
    ),
    pytest.param(
        "\\int x^2 dx",  # integral + solve bonus → 51.5
        "solve",
        "x",
        id="integral-solve",
    ),
]


def _post_tool(name: str, args: dict, timeout_s: int = 30, canary_ratio: float = 0.0):
    """调用 /tools/{name}。默认 canary_ratio=0 走直接 sympy（不受 canary_state.json 影响）.

    设置 canary_ratio=0.5 可主动触发 fallback chain 测试。
    """
    body = json.dumps(args | {"canary_ratio": canary_ratio}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/tools/{name}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", errors="replace")[:200]}
    except urllib.error.URLError as e:
        return 0, {"_err": str(e.reason)}


def _get_metric(name: str) -> float:
    """读 /metrics，找 name 开头的 counter line，返回最后数值（0.0 表示 absent）。"""
    with urllib.request.urlopen(f"{API}/metrics", timeout=5) as r:
        for line in r.read().decode().splitlines():
            if line.startswith(name):
                return float(line.split()[-1])
    return 0.0


class TestAskPath:
    """验证 fallback chain 的 ask 层能被真触发"""

    @pytest.mark.parametrize("latex,action,var", ASK_PATH_CASES)
    def test_attempts_includes_ask(self, latex, action, var):
        """每次请求至少在 attempts 列表里看到一次 strategy=ask"""
        code, body = _post_tool(
            "math_strategy_route_and_execute",
            {"latex": latex, "action": action, "var": var},
        )
        assert code == 200, f"HTTP {code}: {body}"

        # chain 必须在
        plan = body.get("plan", {})
        chain = plan.get("fallback_chain", [])
        assert "ask" in chain, f"chain 不含 ask: {chain}"

        # attempts 必须有 ask（即使 ok=False 也要 +1）
        attempts = body.get("attempts", [])
        asked_strats = [a["strategy"] for a in attempts]
        assert "ask" in asked_strats, (
            f"case {latex}/{action} 未触发 ask attempt，"
            f"实际={asked_strats}, score={plan.get('score')}, primary={plan.get('strategy')}"
        )

    def test_metric_increments_per_call(self):
        """连发 3 次相同请求，ask counter 至少 +3"""
        before = _get_metric(ASK_STRATEGY)
        for _ in range(3):
            _post_tool(
                "math_strategy_route_and_execute",
                {"latex": "x^{x^{x}}", "action": "solve", "var": "x"},
            )
        after = _get_metric(ASK_STRATEGY)
        delta = after - before
        assert delta >= 3, f"3 次请求后 ask counter 应增加 ≥ 3，实际 {before} → {after} (Δ={delta})"

    def test_chain_starts_with_sympy(self):
        """fallback chain 第一项应该是 sympy（设计如此）"""
        code, body = _post_tool(
            "math_strategy_route_and_execute",
            {"latex": "x^{x^{x}}", "action": "solve", "var": "x"},
        )
        assert code == 200, body
        chain = body.get("plan", {}).get("fallback_chain", [])
        assert chain and chain[0] == "sympy", f"chain 第一项应为 sympy，实际={chain}"

    def test_score_in_ask_range(self):
        """让 ask 触发的输入 score 应落在 [30, 55)"""
        code, body = _post_tool(
            "math_strategy_route_and_execute",
            {"latex": "x^{x^{x}}", "action": "solve", "var": "x"},
        )
        assert code == 200, body
        score = body.get("plan", {}).get("score", 0)
        assert 30 <= score < 55, (
            f"score={score} 不在 ask 区间 [30, 55)；"
            "可能 THRESHOLD 改了，或者 LaTeX 不再 trigger ask。"
            "如确需更新测试用例，调整 ASK_PATH_CASES 即可。"
        )


class TestFallbackChain:
    """chain 行为 sanity check"""

    def test_simple_input_only_uses_sympy(self):
        """简单 LaTeX 不应触发 ask（保留 ask 段节能）

        设计说明 (2026-09-25): `math_strategy_route_and_execute` 入口本身
        就是"必走 fallback chain 拿 telemetry"的工具（见 strategy_selector.py
        execute_with_fallback), 4 阶段 (sympy/ask/numeric/llm) attempts 必有。
        对简单输入的 sanity check 应看:
          - primary plan.strategy = sympy (低 score)
          - strategy_used 最终 = sympy (sympy 阶段 ok=True)
          - chain 不应误把简单输入推到 llm
        """
        code, body = _post_tool(
            "math_strategy_route_and_execute",
            {"latex": "x + 1", "action": "simplify", "var": "x"},
        )
        assert code == 200, body
        plan = body.get("plan", {})
        assert plan.get("strategy") == "sympy", f"plan.primary 应为 sympy，实际={plan}"
        # 最终成功 strategy 应是 sympy (x+1 应 sympy 一次成功)
        assert body.get("strategy_used") == "sympy", (
            f"简单输入 x+1 应 sympy 直接成功，不该 fallback 到 llm，实际={body.get('strategy_used')}"
        )
        # chain 必然 4 阶段 (入口设计如此), 但 strategy_used 应停在 sympy
        attempts = body.get("attempts", [])
        sympy_attempt = next((a for a in attempts if a["strategy"] == "sympy"), None)
        assert sympy_attempt and sympy_attempt.get("ok") is True, (
            f"sympy 阶段应 ok=True, 实际={sympy_attempt}"
        )
