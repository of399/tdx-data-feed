"""P1-1 / P1-3：skill_router 意图路由测试基座。

钉死 KI-SKILL-001 修复后的预期行为：
- 大小写不敏感（"PE"/"MACD" 大写也能路由到对应 skill）
- 裸 6 位代码可抽取并归一化（600519 → sh600519）
- 带代码 + 财务关键词 → financial-summary（不被 kline 抢走）
- theory-reference 仅在「无代码 + 概念提问」时触发
- 裸 6 位（无关键词）→ kline-analysis，而非退化到 indicator-match
"""
from pathlib import Path

from skill_router.orchestrator import Orchestrator, _normalize_code, CODE_RE

ROOT = Path(__file__).resolve().parents[1]


def _orch() -> Orchestrator:
    # route/extract_code 只依赖模块级正则，不触发 __init__ 的技能/MCP 加载
    return Orchestrator.__new__(Orchestrator)


def test_ignorecase_pe_routes_financial():
    assert _orch().route("sh600519 的 PE 是多少") == "financial-summary"


def test_ignorecase_macd_routes_indicator():
    assert _orch().route("sh600000 的 MACD 金叉吗") == "indicator-match"


def test_bare_six_digit_normalized():
    o = _orch()
    assert o.extract_code("600519 怎么看") == "sh600519"
    assert o.extract_code("000858 怎么看") == "sz000858"


def test_bare_six_digit_routes_to_kline():
    assert _orch().route("600519 怎么看") == "kline-analysis"


def test_code_plus_financial_keyword_routes_financial():
    # 带代码 + 财务词 → financial-summary，不被 kline 抢
    assert _orch().route("sh600519 营收近 4 季度") == "financial-summary"


def test_code_plus_daban_keyword_routes_daban():
    assert _orch().route("sh600519 次日涨停候选") == "daban"


def test_theory_only_without_code():
    o = _orch()
    assert o.route("什么是异方差") == "theory-reference"
    assert o.route("投资学里的有效市场假说定义") == "theory-reference"


def test_theory_not_hijacked_by_stock_question():
    # 个股问题（含代码）不应被 theory 抢走
    assert _orch().route("sh600519 的护城河是什么") != "theory-reference"


def test_normalize_code_helper():
    assert _normalize_code("600519") == "sh600519"
    assert _normalize_code("000858") == "sz000858"
    assert _normalize_code("SH600519") == "sh600519"
    assert _normalize_code("bj430017") == "bj430017"
    assert _normalize_code("sh600519") == "sh600519"
    # 非标输入原样返回，交给上层校验
    assert _normalize_code("abc") == "abc"
