"""P1-4：LLM 降级双跑回归（KI-SKILL-003 衍生）。

问题：runtime 已按 fallback_skill 降级并返 fallback_from，orchestrator 又因
'llm_output' not in result 再次执行 indicator-match → MCP 调用翻倍。

验收（来自 REMEDIATION_PLAN P1-4）：LLM 不可用时，kline-analysis 经编排层走到
indicator-match 的 MCP 调用应 = indicator-match 直接执行的一组，不应翻倍。

覆盖场景：
1. 编排层整链（Orchestrator.handle）：LLM 不可用 → runtime 内部降级 → 不再二次执行。
2. runtime 隔离（Fix A）：根本无 LLM 时也应按 fallback_skill 降级，而非直接报错。
"""
from pathlib import Path
from typing import Any

from skill_router.orchestrator import Orchestrator
from skill_router.runtime import SkillRuntime

ROOT = Path(__file__).resolve().parents[1]


def _fake_mcp(server: str, tool: str, arguments: dict) -> Any:
    """indicator-match 降级路径所需的合理假响应（仅满足取值，不校验业务逻辑）。"""
    if tool == "read_daily_kline":
        return {"data": [{
            "date": "20260101", "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 10.0, "volume": 1.0, "amount": 1.0, "adj_factor": 1.0,
        }]}
    if tool == "calc_macd":
        return {"dif": [1.0], "dea": [1.0], "golden_cross": [1]}
    if tool == "calc_kdj":
        return {"j": [20.0]}
    if tool == "calc_rsi":
        return [30.0]
    if tool == "calc_ma":
        return [10.0]
    return {}


class _CountingRuntime(SkillRuntime):
    """子类化只为了：① 把真实 MCP 子进程替换为 计数+假数据；② 强制 LLM 不可用。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mcp_calls = 0

    def call_mcp(self, server: str, tool: str, arguments: dict) -> Any:
        self.mcp_calls += 1
        return _fake_mcp(server, tool, arguments)

    def _ensure_llm(self) -> bool:
        # 模拟「根本无 LLM」：不加载权重，直接返回不可用
        return False


def _make_orchestrator() -> Orchestrator:
    orch = Orchestrator(
        skills_dir=ROOT / "skills",
        mcp_dir=ROOT / "mcp" / "servers",
    )
    # 用计数版 runtime 替换（构造时已 load_all_skills 一次，这里再 load 一次到计数实例）
    orch.runtime = _CountingRuntime(
        skills_dir=ROOT / "skills",
        mcp_dir=ROOT / "mcp" / "servers",
    )
    orch.runtime.load_all_skills()
    return orch


def test_llm_unavailable_kline_degrades_without_double_run():
    """编排层整链：kline-analysis 在 LLM 不可用时只跑一组 indicator-match（不翻倍）。"""
    orch = _make_orchestrator()
    rt: _CountingRuntime = orch.runtime

    # 1) 走完整编排流程：sh600000 怎么看 → kline-analysis → runtime 内部降级
    rt.mcp_calls = 0
    handle_result = orch.handle("sh600000 怎么看")
    via_handle = rt.mcp_calls

    # 2) 直接执行 indicator-match 的一组调用数（基准）
    rt.mcp_calls = 0
    orch.runtime.execute_skill("indicator-match", {"code": "sh600000"})
    direct = rt.mcp_calls

    # 3) 断言：编排层只跑了一组（== 直接基准），且确实走了降级
    assert direct > 0, "indicator-match 基准调用数应为正"
    assert via_handle == direct, (
        f"降级导致 MCP 调用翻倍：编排层 {via_handle} 次 vs 单组 {direct} 次"
    )
    assert handle_result["intent"] == "kline-analysis"
    assert handle_result["result"].get("ok") is True
    assert handle_result["result"].get("fallback_from") == "kline-analysis"
    assert handle_result["result"].get("skill") == "indicator-match"


def test_runtime_honors_fallback_when_llm_unavailable():
    """Fix A 隔离：根本无 LLM 时，runtime 也应按 fallback_skill 降级（而非报错）。"""
    rt = _CountingRuntime(skills_dir=ROOT / "skills", mcp_dir=ROOT / "mcp" / "servers")
    rt.load_all_skills()
    res = rt.execute_skill("kline-analysis", {"code": "sh600000"})
    assert res.get("ok") is True, f"无 LLM 时未降级：{res}"
    assert res.get("fallback_from") == "kline-analysis"
    assert res.get("skill") == "indicator-match"
    # 降级路径必须产出 indicator-match 的结果，而不是把降级推回编排层再跑一次
    assert "llm_output" not in res  # 明确：这是纯工具降级，不是 LLM 输出
