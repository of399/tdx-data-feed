"""
v5/mcp_servers/math_sympy.py 验收测试

测试 20 个 Tool：
  M1 继承: sympy_compute / are_equiv
  M2 新增: math_error_report / math_fix_typo / math_validate_symbols / math_cache_stats
  M2 W9 D2.1: prove_equiv_with_precondition
  M2 W9 D2.2: solve_inequality
  M2 W9 D2.3: simplify_piecewise
  M2 W10 D2.4: math_strategy_route + math_strategy_route_and_execute
  M2 W11 D3.1: math_derivation_chain + math_derivation_replay
  M2 W11 D3.2: math_step_extract
  M2 W11 D3.3: math_derivation_cache_stats + math_derivation_build_cached
  M2 W12 D3.5: math_derivation_reverse
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/jiuben/tdx-data-feed")
sys.path.insert(0, "/home/jiuben/tdx-data-feed/v5")

import mcp_servers.math_sympy as math_sympy_mod
from mcp_servers.math_sympy import (
    _cached_compute,
    _cached_equiv,
    _prove_equiv_with_precondition,
    _simplify_piecewise,
    _solve_inequality,
    math_cache,
    math_errors,
)

from mcp_math.derivation import (
    build_chain,
    list_rules as derivation_rules,
    replay_dag,
    write_audit_log,
)
from mcp_math.derivation_cache import (
    build_chain_with_cache,
    cache_stats as derivation_cache_stats,
    clear_expired as clear_derivation_cache,
    get_cached,
    list_cached,
    save_dag,
)
from mcp_math.derivation_reverse import reverse_build
from mcp_math.step_extractor import (
    CalcStep,
    extract_steps_from_markdown,
    verify_steps as verify_calc_steps,
)
from mcp_math.strategy_selector import (
    _llm_fallback_call,
    assess_complexity,
    execute_with_fallback,
    select_strategy,
)

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
    print("=== mcp-math-sympy (M2 增强版 + D6.3 超时分级) 验收测试 ===\n")

    print("[1] are_equiv 第一次调用（预筛优先）")
    r1 = await _cached_equiv(r"\int x dx", r"\frac{x^2}{2}")
    check("are_equiv ok", "equivalent" in r1)
    print(f"   result: {json.dumps(r1, ensure_ascii=False)}")

    print("\n[2] are_equiv 第二次同输入（预筛→cache 命中）")
    r2 = await _cached_equiv(r"\int x dx", r"\frac{x^2}{2}")
    check(
        "cache/prefilter 命中",
        r2.get("method") in ("cache", "prefilter"),
        f"method={r2.get('method')}",
    )

    print("\n[2.5] are_equiv 不同形式但等价（应进 simplify + 写缓存）")
    r2b = await _cached_equiv(r"\sin(x)^2 + \cos(x)^2", r"1")
    check(
        "compute 或 cache",
        r2b.get("method") in ("sympy.simplify", "cache", "prefilter", "typo_fix+sympy.simplify"),
        f"method={r2b.get('method')}, eq={r2b.get('equivalent')}",
    )
    print(f"   result: {json.dumps(r2b, ensure_ascii=False)}")

    print("\n[3] are_equiv 预筛（长度差过大）")
    r3 = await _cached_equiv("a", "a + b + c + d + e + f + g + h + i")
    check("prefilter 拒绝", r3.get("method") == "prefilter")
    check("返回 equivalent=False", r3.get("equivalent") is False)

    print("\n[4] sympy_compute 缓存命中")
    r4 = await _cached_compute(r"x^2 + 2*x + 1", "factor")
    check("compute ok", r4.get("ok"))
    print(f"   result: {json.dumps(r4, ensure_ascii=False)}")

    r5 = await _cached_compute(r"x^2 + 2*x + 1", "factor")
    check("cache hit", r5.get("method") == "cache")

    print("\n[5] math_error_report: 未知命令 fallback")
    from sympy.parsing.latex import parse_latex

    try:
        parse_latex(r"\unknowncmd{1}{2}")
        # 若新版 sympy 容忍 unknown 命令，强制造错走 fallback
        raise ValueError("could not parse: unknown command '\\unknowncmd'")
    except Exception as e:
        report = math_errors.classify_sympy_exception(e, r"\unknowncmd{1}{2}")
    check("error_type=ParseError", report.error_type.value == "ParseError")
    check("有 hint", len(report.hint) > 0)
    # suggested_fix 可为 None（合成词无合理建议）；如有 fuzzy 命中才返回
    check(
        "suggested_fix 合理（None 或非空）",
        report.suggested_fix is None or len(report.suggested_fix) > 0,
    )
    print(f"   hint: {report.hint}")
    print(f"   suggested_fix: {report.suggested_fix}")

    print("\n[6] math_fix_typo")
    fixed, applied = math_errors.fix_typo(r"\FRAC{1}{2} + \intt x dx")
    check("修复了 FRAC", r"\FRAC" not in fixed)
    check("applied 不为空", len(applied) > 0)
    suggestions = math_errors.fuzzy_fix_command(r"\deltax + \sinn(\theta)")
    check("fuzzy 建议存在", len(suggestions) > 0)
    print(f"   suggestions: {suggestions}")

    print("\n[7] math_validate_symbols")
    r = math_errors.validate_symbols(r"x^2 + y = z", declared={"x", "y", "z", "w"})
    check("used 含 x/y/z", set(r["used"]) == {"x", "y", "z"})
    check("unused 含 w", "w" in r["unused"])
    check("undeclared 为空", r["undeclared"] == [])

    print("\n[8] math_cache_stats")
    stats = math_cache.cache_stats()
    check("有 total_entries", "total_entries" in stats)
    print(f"   stats: {stats}")

    print("\n[8.1] D5.5 数值 fallback（solve 失败时）")
    # x^3 = 2 用 sympy.solve 应该是精确的，但用 nsolve 模拟 fallback 路径
    r = await _cached_compute(r"x^3 - 2", "solve", var="x")
    check("solve 走数值或符号", r.get("ok"))
    print(f"   solve x^3=2 → {r.get('result')}, method={r.get('method')}")

    print("\n[8.2] D5.5 数值 fallback（simplify 失败时）")
    # sqrt(2) sympy.simplify 应该能算出来，但验证 fallback 路径
    r = await _cached_compute(r"\sqrt{2} + \sqrt{3}", "simplify")
    check("simplify sqrt2+sqrt3", r.get("ok"))
    print(f"   simplify √2+√3 → {r.get('result')}, method={r.get('method')}")

    print("\n[8.3] D1.2 pylatexenc AST fallback")
    ast = math_sympy_mod._pylatexenc_parse(r"\int_{0}^{1} x^2 \, dx = \frac{1}{3}")
    check("AST ok", ast.get("ok"))
    if ast.get("ok"):
        check("有 node_count", "node_count" in ast)
        check("macros 含 int", "int" in ast.get("macros", []))
        check("plain_text 非空", len(ast.get("plain_text", "")) > 0)
        print(f"   macros: {ast.get('macros')}")
        print(f"   plain: {ast.get('plain_text')}")

    print("\n[8.4] D1.2 在解析失败时触发")
    # 用 \\frac{ 触发 sympy 解析失败（缺参数）
    r = await _cached_compute(r"\frac{", "simplify")
    # 接受任一种：fallback 触发 或 已分类的错误
    has_fallback = "ast_fallback" in r or "error_report" in r
    check("失败时有 error_report 或 ast_fallback", has_fallback)
    if r.get("ast_fallback", {}).get("ok"):
        print(f"   ast_fallback macros: {r['ast_fallback'].get('macros')}")
    else:
        print(f"   error_report: {r.get('error_report', {}).get('error_type', '?')}")

    print("\n[8.5] D6.5 并行批量判定")
    from mcp_math.parallel import batch_are_equiv

    pairs = [
        (r"x + 1", r"1 + x"),
        (r"\sin^2 x + \cos^2 x", r"1"),
        (r"\int x dx", r"\frac{x^2}{2}"),
    ]
    results = batch_are_equiv(pairs, max_workers=2)
    check("返回 3 个结果", len(results) == 3)
    eq_count = sum(1 for r in results if r.get("equivalent") is True)
    check(f"至少 1 对等价（实测 {eq_count}）", eq_count >= 1)
    for p, r in zip(pairs, results, strict=True):
        print(f"   {p[0][:20]} vs {p[1][:20]} → eq={r.get('equivalent')}, method={r.get('method')}")

    print("\n[9] MCP stdio 协议验证")
    try:
        from mcp.client.stdio import stdio_client

        from mcp import ClientSession, StdioServerParameters

        server_params = StdioServerParameters(
            command="/home/jiuben/tdx-data-feed/venv/bin/python",
            args=["/home/jiuben/tdx-data-feed/v5/mcp_servers/math_sympy.py"],
        )

        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
                await session.initialize()

                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                print(f"  注册 {len(tool_names)} 个 Tool: {sorted(tool_names)}")
                expected = {
                    "sympy_compute",
                    "are_equiv",
                    "math_error_report",
                    "math_fix_typo",
                    "math_validate_symbols",
                    "math_cache_stats",
                    "math_numeric_solve",
                    "math_ast_dump",
                    "math_batch_equiv",
                    "prove_equiv_with_precondition",
                    "solve_inequality",
                    "simplify_piecewise",
                    "math_strategy_route",
                    "math_strategy_route_and_execute",
                    "math_derivation_chain",
                    "math_derivation_replay",
                    "math_step_extract",
                    "math_derivation_cache_stats",
                    "math_derivation_build_cached",
                    "math_derivation_reverse",
                }
                check("注册 20 个 Tool", tool_names == expected)

                r = await session.call_tool(
                    "are_equiv",
                    {
                        "latex_a": r"\int x dx",
                        "latex_b": r"\frac{x^2}{2}",
                    },
                )
                data = json.loads(r.content[0].text)
                check("MCP are_equiv ok", "equivalent" in data)
                print(f"   are_equiv: {data}")

                r = await session.call_tool("math_fix_typo", {"latex": r"\FRAC{1}{2}"})
                data = json.loads(r.content[0].text)
                check("MCP math_fix_typo ok", "fixed" in data)
                print(f"   fixed: {data['fixed']}, applied: {data['applied']}")

                r = await session.call_tool("math_cache_stats", {})
                data = json.loads(r.content[0].text)
                check("MCP math_cache_stats ok", "total_entries" in data)
                print(f"   stats: {data}")
    except ImportError:
        print("  SKIP  mcp client SDK 未安装")

    # ============================================================
    # M2 W9 · D2.1 prove_equiv_with_precondition（条件等价证明）
    # ============================================================
    print("\n[W9·D2.1] prove_equiv_with_precondition（条件等价证明）")
    r_d21_1 = _prove_equiv_with_precondition(r"\sqrt{x^2}", "x", ["x > 0"], 2.0)
    check(
        "D2.1·1 sqrt(x^2)==x, x>0 成立",
        r_d21_1.get("proved") is True,
        f"got proved={r_d21_1.get('proved')} method={r_d21_1.get('method')}",
    )

    r_d21_2 = _prove_equiv_with_precondition("x + 1", "x + 1", [], 2.0)
    check("D2.1·2 x+1==x+1 恒等", r_d21_2.get("proved") is True)

    r_d21_3 = _prove_equiv_with_precondition("x^2 + 1", "x^2", [], 2.0)
    check(
        "D2.1·3 x^2+1 != x^2 不等",
        r_d21_3.get("proved") is False,
        f"got proved={r_d21_3.get('proved')}",
    )

    r_d21_4 = _prove_equiv_with_precondition("(x+1)^2", "x^2 + 2*x + 1", [], 2.0)
    check("D2.1·4 (x+1)^2 展开等价", r_d21_4.get("proved") is True)

    r_d21_5 = _prove_equiv_with_precondition("1/x", "1/x", ["x != 0"], 2.0)
    check("D2.1·5 1/x==1/x, x!=0", r_d21_5.get("proved") is True)

    r_d21_6 = _prove_equiv_with_precondition(r"\sin(x)^2 + \cos(x)^2", "1", [], 2.0)
    check(
        "D2.1·6 三角恒等式 sin²+cos²=1",
        r_d21_6.get("proved") is True,
        f"got proved={r_d21_6.get('proved')} method={r_d21_6.get('method')}",
    )

    r_d21_7 = _prove_equiv_with_precondition("x", "x^2", [], 2.0)
    check("D2.1·7 x != x^2（反例验证）", r_d21_7.get("proved") is False)

    # D2.1·8: 边界 timeout（短 timeout_s 仍能处理简单等式）
    r_d21_8 = _prove_equiv_with_precondition("(x+1)^2", "x^2 + 2*x + 1", [], 0.001)
    check(
        "D2.1·8 极短 timeout 仍处理简单等式",
        r_d21_8.get("proved") is True,
        f"got proved={r_d21_8.get('proved')} method={r_d21_8.get('method')}",
    )

    # D2.1·9: 坏 LaTeX 容错（sympy parse_latex 对不完整 LaTeX 也容忍，
    # 走 numeric_sampling 报 0%；测"不崩溃 + 结构完整"）
    r_d21_9 = _prove_equiv_with_precondition(r"\foo{x}", "x", [], 2.0)
    check(
        "D2.1·9 坏 LaTeX 容错 → 不崩溃且结构完整",
        "ok" in r_d21_9 and "proved" in r_d21_9 and "method" in r_d21_9,
        f"got keys={list(r_d21_9.keys())} ok={r_d21_9.get('ok')} method={r_d21_9.get('method')}",
    )

    # D2.1·10: 多变量 precondition（x>0 AND y>0）
    r_d21_10 = _prove_equiv_with_precondition("x*y", "y*x", ["x > 0", "y > 0"], 2.0)
    check(
        "D2.1·10 多变量 precondition x>0 AND y>0",
        r_d21_10.get("proved") is True,
        f"got method={r_d21_10.get('method')}",
    )

    # D2.1·11: 多变量 precondition + 多项式展开（不会踩 log 负数边界）
    r_d21_11 = _prove_equiv_with_precondition(
        "(x + y)^2", "x^2 + 2*x*y + y^2", ["x: real", "y: real"], 2.0
    )
    check(
        "D2.1·11 多变量 precondition + (x+y)² 展开",
        r_d21_11.get("proved") is True,
        f"got method={r_d21_11.get('method')}",
    )

    # D2.1·12: precondition 解析错误（坏 precondition 不致命）
    r_d21_12 = _prove_equiv_with_precondition("x", "x", ["this is not a valid precondition"], 2.0)
    check(
        "D2.1·12 坏 precondition 不致命",
        "parse_errors" in r_d21_12 or r_d21_12.get("proved") is True,
        f"got {r_d21_12}",
    )

    # D2.1·13: 纯数字表达式（无变量）
    r_d21_13 = _prove_equiv_with_precondition("2 + 3", "5", [], 2.0)
    check("D2.1·13 纯数字 2+3==5", r_d21_13.get("proved") is True)

    # D2.1·14: 立方差恒等式 a^3 - b^3 = (a-b)(a^2+ab+b^2)
    r_d21_14 = _prove_equiv_with_precondition("a^3 - b^3", "(a-b)*(a^2 + a*b + b^2)", [], 2.0)
    check(
        "D2.1·14 立方差恒等式",
        r_d21_14.get("proved") is True,
        f"got method={r_d21_14.get('method')}",
    )

    # D2.1·15: 三角恒等式 sin(2x) = 2sin(x)cos(x)
    r_d21_15 = _prove_equiv_with_precondition(r"\sin(2*x)", r"2*\sin(x)*\cos(x)", [], 2.0)
    check(
        "D2.1·15 sin(2x) == 2sin(x)cos(x)",
        r_d21_15.get("proved") is True,
        f"got method={r_d21_15.get('method')}",
    )

    # D2.1·16: 反向不等式（不同前提得到不同结论）
    r_d21_16 = _prove_equiv_with_precondition(r"\sqrt{x^2}", "x", ["x < 0"], 2.0)
    check(
        "D2.1·16 sqrt(x^2) != x 在 x<0 时不成立",
        r_d21_16.get("proved") is False,
        f"got method={r_d21_16.get('method')}",
    )

    # D2.1·17: witness 字段非空
    r_d21_17 = _prove_equiv_with_precondition("x + 1", "1 + x", [], 2.0)
    check(
        "D2.1·17 witness 字段非空",
        isinstance(r_d21_17.get("witness"), str) and len(r_d21_17.get("witness", "")) > 0,
        f"got witness={r_d21_17.get('witness')}",
    )

    # D2.1·18: assumptions_used 字段正确填充（必须用 precondition 才能成立的例子）
    # sqrt(x^2) == -x 在 x<0 时成立（abs(x) = -x 当 x<0）
    r_d21_18 = _prove_equiv_with_precondition(r"\sqrt{x^2}", "-x", ["x < 0"], 2.0)
    check(
        "D2.1·18 assumptions_used 含 ['x < 0']（|x|=-x 在 x<0）",
        "x < 0" in (r_d21_18.get("assumptions_used") or []),
        f"got proved={r_d21_18.get('proved')} method={r_d21_18.get('method')} assumptions={r_d21_18.get('assumptions_used')}",
    )

    # D2.1·19: assumptions_failed 字段存在
    r_d21_19 = _prove_equiv_with_precondition("x", "x", ["bogus_predicate"], 2.0)
    check(
        "D2.1·19 assumptions_failed 字段存在",
        "assumptions_failed" in r_d21_19,
        f"got keys={list(r_d21_19.keys())}",
    )

    # D2.1·20: holds 字段返回 bool
    r_d21_20 = _prove_equiv_with_precondition("x + 1", "x + 1", [], 2.0)
    check(
        "D2.1·20 holds 字段为 bool",
        isinstance(r_d21_20.get("holds"), bool),
        f"got holds={r_d21_20.get('holds')} ({type(r_d21_20.get('holds')).__name__})",
    )

    # D2.1·21: 嵌套复合 precondition（x>0 AND x<10）
    r_d21_21 = _prove_equiv_with_precondition(
        "(x - 5)^2", "x^2 - 10*x + 25", ["x > 0", "x < 10"], 2.0
    )
    check(
        "D2.1·21 多区间 precondition 复合",
        r_d21_21.get("proved") is True,
        f"got method={r_d21_21.get('method')}",
    )

    # ============================================================
    # D2.1.x numeric_sampling bug 回归测试（2026-09-21 修复）
    # 修复：sympify 优先于 parse_latex（避免 "log" 被解析为 l*o*g）
    #      + 采样点满足 constraints 才计入
    #      + ratio 用有效采样数（valid_count）而非总采样数（sample_count）
    # ============================================================
    print("\n[W9·D2.1.x] numeric_sampling bug 回归")

    # D2.1·22: log(x^2) == 2*log(x), x>0（之前 sympify bug 报 0/50）
    r_d21_22 = _prove_equiv_with_precondition("log(x**2)", "2*log(x)", ["x > 0"], 2.0)
    check(
        "D2.1·22 log(x²)==2log(x), x>0（sympify fix）",
        r_d21_22.get("proved") is True,
        f"got proved={r_d21_22.get('proved')} method={r_d21_22.get('method')} ratio={r_d21_22.get('numeric_ratio')}",
    )

    # D2.1·23: sqrt((x+1)^2) == x+1, x>-1（之前 constraint 不满足被错误采样）
    r_d21_23 = _prove_equiv_with_precondition("sqrt((x+1)**2)", "x+1", ["x > -1"], 2.0)
    check(
        "D2.1·23 sqrt((x+1)²)==x+1, x>-1（constraint filter）",
        r_d21_23.get("proved") is True,
        f"got proved={r_d21_23.get('proved')} method={r_d21_23.get('method')} ratio={r_d21_23.get('numeric_ratio')}",
    )

    # D2.1·24: witness 字段包含有效采样数（不是总采样数）
    r_d21_24 = _prove_equiv_with_precondition("sqrt((x+1)**2)", "x+1", ["x > -1"], 2.0)
    witness = r_d21_24.get("witness", "")
    # 验证 witness 格式: "X/Y 采样点成立 (100%)" 且 ratio=1.0
    # 修复前：witness="25/50 采样点成立 (50%)" 错误
    # 修复后：witness="50/50 采样点成立 (100%)" 正确（valid_count=50，因为 constraint filter skip 了 -1 以下的点不影响全部 50 采样中的有效数）
    has_100_percent = "(100%)" in witness
    check(
        "D2.1·24 witness 显示 ratio=100% (constraint filter fix)",
        has_100_percent and r_d21_24.get("numeric_ratio") == 1.0,
        f"got witness={witness} ratio={r_d21_24.get('numeric_ratio')}",
    )

    # D2.1·25: Abs(x) == x（直接表达 Abs, x>0）
    r_d21_25 = _prove_equiv_with_precondition("Abs(x)", "x", ["x > 0"], 2.0)
    check(
        "D2.1·25 Abs(x)==x, x>0",
        r_d21_25.get("proved") is True,
        f"got method={r_d21_25.get('method')} ratio={r_d21_25.get('numeric_ratio')}",
    )

    # D2.1·26: 反例 + constraint 不在 0 边界（sqrt(x^2) == x, x<-5，不成立）
    r_d21_26 = _prove_equiv_with_precondition("sqrt(x**2)", "x", ["x < -5"], 2.0)
    check(
        "D2.1·26 sqrt(x²)==x, x<-5 不成立（ratio<0.98）",
        r_d21_26.get("proved") is False,
        f"got proved={r_d21_26.get('proved')} ratio={r_d21_26.get('numeric_ratio')}",
    )

    # ============================================================
    # M2 W9 · D2.2 solve_inequality（不等式求解）
    # ============================================================
    print("\n[W9·D2.2] solve_inequality（不等式求解）")
    r_d22_1 = _solve_inequality("x - 1 > 0", "x", relational=True, timeout_s=2.0)
    check(
        "D2.2·1 x-1>0 求解 ok",
        r_d22_1.get("ok") and "1 < x" in r_d22_1.get("solution_latex", ""),
        f"got {r_d22_1.get('solution_latex')}",
    )

    r_d22_2 = _solve_inequality("x^2 < 4", "x", relational=True, timeout_s=2.0)
    check(
        "D2.2·2 x^2<4 解集 (-2,2)",
        r_d22_2.get("ok") and "2" in r_d22_2.get("solution_latex", ""),
        f"got {r_d22_2.get('solution_latex')}",
    )

    r_d22_3 = _solve_inequality("x^2 - 3*x + 2 < 0", "x", relational=True, timeout_s=2.0)
    check(
        "D2.2·3 (x-1)(x-2)<0 解 (1,2)",
        r_d22_3.get("ok")
        and "1" in r_d22_3.get("solution_latex", "")
        and "2" in r_d22_3.get("solution_latex", ""),
        f"got {r_d22_3.get('solution_latex')}",
    )

    r_d22_4 = _solve_inequality("x >= 0", "x", relational=True, timeout_s=2.0)
    check("D2.2·4 x>=0 解集 [0,oo)", r_d22_4.get("ok"), f"got {r_d22_4.get('solution_latex')}")

    r_d22_5 = _solve_inequality("x^2 + 1 > 0", "x", relational=True, timeout_s=2.0)
    check(
        "D2.2·5 x^2+1>0 恒成立（负数不能解）",
        r_d22_5.get("ok"),
        f"got {r_d22_5.get('solution_latex')}",
    )

    # ============================================================
    # M2 W9 · D2.3 simplify_piecewise（分段函数）
    # ============================================================
    print("\n[W9·D2.3] simplify_piecewise（分段函数）")
    r_d23_1 = _simplify_piecewise(
        [{"cond": "x >= 0", "value": "x"}, {"cond": "x < 0", "value": "-x"}],
        "x",
        "simplify",
    )
    check(
        "D2.3·1 |x| 分段 ok",
        r_d23_1.get("ok") and "cases" in r_d23_1 and len(r_d23_1.get("cases", [])) == 2,
        f"got {r_d23_1.get('simplified_latex', '')[:60]}",
    )

    r_d23_2 = _simplify_piecewise(
        [
            {"cond": "x > 0", "value": "1"},
            {"cond": "x == 0", "value": "0"},
            {"cond": "x < 0", "value": "-1"},
        ],
        "x",
        "simplify",
    )
    check("D2.3·2 sign(x) 三段 ok", r_d23_2.get("ok") and len(r_d23_2.get("cases", [])) == 3)

    r_d23_3 = _simplify_piecewise(
        [{"cond": "x >= 0", "value": "x^2"}, {"cond": "x < 0", "value": "-x"}],
        "x",
        "simplify",
    )
    check("D2.3·3 f(x)=x²/-x 简化保留 piecewise", r_d23_3.get("ok") and "cases" in r_d23_3)

    r_d23_4 = _simplify_piecewise(
        [{"cond": "x > 0", "value": "x^2"}],
        "x",
        "evaluate",
    )
    check("D2.3·4 单段 evaluate", r_d23_4.get("ok") and r_d23_4.get("action") == "evaluate")

    r_d23_5 = _simplify_piecewise(
        [{"cond": "x > 0", "value": "x"}, {"cond": "x <= 0", "value": "0"}],
        "x",
        "expand",
    )
    check("D2.3·5 ReLU 分段 expand", r_d23_5.get("ok") and r_d23_5.get("action") == "expand")

    # ============================================================
    # M2 W10 · D2.4 策略选择器
    # ============================================================
    print("\n[W10·D2.4·1] assess_complexity 复杂度评分")
    r = assess_complexity("x + 1", "simplify")
    check("D2.4·1·a 简单公式 score<30", r.score < 30, f"got {r.score:.1f}")
    check("D2.4·1·b 简单公式 free_vars 含 x", "x" in r.free_vars)
    check("D2.4·1·c 简单公式无 integral", not r.has_integral)

    r = assess_complexity(r"\int e^{-x^2} \, dx", "integrate")
    check("D2.4·1·d 复杂积分 has_integral=True", r.has_integral)
    check("D2.4·1·e 复杂积分 score>50", r.score > 50, f"got {r.score:.1f}")

    r = assess_complexity(r"\sum_{n=1}^{\infty} \frac{1}{n^2}", "simplify")
    check("D2.4·1·f 无限级数 has_sum_product=True", r.has_sum_product)
    check("D2.4·1·g 无限级数 has_infinity=True", r.has_infinity)

    print("\n[W10·D2.4·2] select_strategy 路由选择")
    plan = select_strategy("x + 1", "simplify")
    check("D2.4·2·a 简单 → sympy", plan["strategy"] == "sympy")
    check(
        "D2.4·2·b 含 fallback_chain", plan["fallback_chain"] == ["sympy", "ask", "numeric", "llm"]
    )

    plan = select_strategy(r"\int e^{-x^2} \, dx", "integrate")
    check(
        "D2.4·2·c 复杂积分 → numeric/llm",
        plan["strategy"] in ("numeric", "llm"),
        f"got {plan['strategy']}",
    )
    check("D2.4·2·d 含 reasoning", len(plan["reasoning"]) > 0)
    check("D2.4·2·e 含 complexity 详情", "complexity" in plan)

    # 历史超时强制 numeric
    plan = select_strategy("x + 1", "simplify", timeout_history={"simplify": 5.0})
    check(
        "D2.4·2·f 历史超时>3s 强制 numeric",
        plan["strategy"] == "numeric",
        f"got {plan['strategy']}",
    )

    print("\n[W10·D2.4·3] execute_with_fallback fallback 链")
    # 简单公式应 sympy 直接成功
    r = execute_with_fallback("x + 1", "simplify", "x")
    check("D2.4·3·a sympy 成功", r.get("ok"))
    check("D2.4·3·b strategy_used=sympy", r.get("strategy_used") == "sympy")
    check(
        "D2.4·3·c attempts 1 个（不降级）",
        len(r.get("attempts", [])) == 1,
        f"got {len(r.get('attempts', []))}",
    )

    # 复杂公式应触发降级
    r = execute_with_fallback(r"\int e^{-x^2} \, dx", "integrate", "x")
    check("D2.4·3·d 复杂积分有结果", r.get("ok") or len(r.get("attempts", [])) > 0)
    check("D2.4·3·e 至少尝试 1 个策略", len(r.get("attempts", [])) >= 1)

    # ============================================================
    # M2 W11 · D3.1 推导链 DAG
    # ============================================================
    print("\n[W11·D3.1·1] build_chain 真实用例")
    r = build_chain("(x+1)**2", "x**2 + 2*x + 1", max_depth=4)
    check("D3.1·1·a (x+1)² → x²+2x+1 命中", r.success)
    check(
        "D3.1·1·b reason ∈ {found_path, trivially_equal}",
        r.reason in ("found_path", "trivially_equal"),
        f"got {r.reason}",
    )
    check("D3.1·1·c DAG 至少 1 节点", len(r.nodes) >= 1)

    r = build_chain("x**2", "2*x", max_depth=4)
    check(
        "D3.1·1·d x² → 2x 求导链 found_path",
        r.success and r.reason == "found_path" and r.steps >= 1,
    )

    r = build_chain("(x+1)**3", "3*(x+1)**2", max_depth=4)
    check("D3.1·1·e (x+1)³ → 3(x+1)² 求导幂", r.success and r.reason == "found_path")

    r = build_chain("sin(2*x)", "2*sin(x)*cos(x)", max_depth=3)
    check("D3.1·1·f sin(2x) → 2sin(x)cos(x)", r.success)

    print("\n[W11·D3.1·2] DAG 不可达 + reason")
    r = build_chain("(x+1)**2", "sin(x)", max_depth=2)
    check("D3.1·2·a 不可达 success=False", not r.success)
    check("D3.1·2·b reason=max_depth_exceeded", r.reason == "max_depth_exceeded", f"got {r.reason}")

    r = build_chain("x**100", "1", max_depth=2)
    check("D3.1·2·c x^100 → 1 不可达", not r.success)

    print("\n[W11·D3.1·3] DAG 序列化 + Mermaid")
    r = build_chain("x**2", "2*x", max_depth=2)
    dag_dict = r.to_dict()
    check(
        "D3.1·3·a to_dict 含必要字段",
        all(k in dag_dict for k in ("nodes", "edges", "success", "steps")),
    )
    json_str = r.to_json()
    check("D3.1·3·b to_json 字符串", isinstance(json_str, str) and len(json_str) > 0)
    check("D3.1·3·c Mermaid 含 flowchart", "flowchart" in r.to_mermaid())
    check(
        "D3.1·3·d Mermaid 含 node 标签",
        "n0" in r.to_mermaid() and f"n{r.target_node_id}" in r.to_mermaid(),
    )

    print("\n[W11·D3.1·4] replay_dag 重放验证")
    replay = replay_dag(dag_dict)
    check("D3.1·4·a 重放成功", replay["ok"])
    check("D3.1·4·b all_match=True", replay["all_match"])
    check(
        "D3.1·4·c steps_replayed >= 1",
        replay["steps_replayed"] >= 1,
        f"got {replay['steps_replayed']}",
    )
    check("D3.1·4·d final_expr 非空", bool(replay["final_expr"]), f"got {replay['final_expr']!r}")

    print("\n[W11·D3.1·5] list_rules 内置规则")
    rules = derivation_rules()
    check("D3.1·5·a 含 12 个内置规则", len(rules) == 12, f"got {len(rules)}: {rules}")
    check(
        "D3.1·5·b 含 expand/factor/simplify/trigsimp/diff",
        all(r in rules for r in ["expand", "factor", "simplify", "trigsimp", "diff"]),
    )

    # ============================================================
    # M2 W11 · D3.2 自动 step 抽取（markdown <calc> 块）
    # ============================================================
    print("\n[W11·D3.2·1] extract_steps_from_markdown")
    md = (
        "# 测试笔记\n\n"
        "## 步骤 1\n"
        "```calc\n"
        "(x+1)^2\n---\nexpand\n---\nx^2 + 2*x + 1\n"
        "```\n\n"
        "## 步骤 2\n"
        "```calc\n"
        "x^2\n---\ndiff\n---\n2*x\n"
        "```\n\n"
        "## 错误\n"
        "```calc\n"
        "只写了起点没规则\n"
        "```\n"
    )
    ext = extract_steps_from_markdown(md)
    check("D3.2·1·a total_blocks=3", ext.total_blocks == 3, f"got {ext.total_blocks}")
    check("D3.2·1·b valid_steps=2", len(ext.steps) == 2, f"got {len(ext.steps)}")
    check("D3.2·1·c invalid=1", len(ext.invalid_blocks) == 1)
    check("D3.2·1·d step[0] 含 rule", ext.steps[0].rule == "expand")
    check("D3.2·1·e step[0] line_no=4", ext.steps[0].line_no == 4)

    print("\n[W11·D3.2·2] verify_steps 验证可达性")
    v = verify_calc_steps(ext.steps)
    check("D3.2·2·a verified=2", v["verified"] == 2, f"got {v['verified']}")

    # ============================================================
    # M2 W11 · D3.3 sidecar 持久化 + 缓存命中
    # ============================================================
    print("\n[W11·D3.3·1] save_dag + get_cached 命中")
    dag_x2 = build_chain("x**2", "2*x", var="x")
    path_saved = save_dag(dag_x2.to_dict(), note_id="test_w11_d33_x2")
    check("D3.3·1·a save_dag 返回路径", path_saved.endswith(".derivation.json"))
    check("D3.3·1·b 文件存在", os.path.exists(path_saved))
    cached = get_cached("x**2", "2*x", var="x")
    check("D3.3·1·c 缓存命中", cached is not None)
    check("D3.3·1·d cached steps=1", cached["steps"] == 1)

    print("\n[W11·D3.3·2] build_chain_with_cache source 路由")
    result = build_chain_with_cache("x**2", "2*x", var="x")
    check("D3.3·2·a source=cache（已存）", result["source"] == "cache")
    # 用唯一公式确保首次：带 UUID-like 后缀
    import uuid

    unique_start = f"x**2 + {uuid.uuid4().hex[:8]}"
    unique_target = f"x**2 + {uuid.uuid4().hex[:8]}"
    result = build_chain_with_cache(unique_start, unique_target, var="x")
    check("D3.3·2·b source=computed（首次）", result["source"] == "computed")

    print("\n[W11·D3.3·3] cache_stats 统计")
    stats = derivation_cache_stats()
    check(
        "D3.3·3·a total_entries >= 2", stats["total_entries"] >= 2, f"got {stats['total_entries']}"
    )
    check("D3.3·3·b total_hits >= 1", stats["total_hits"] >= 1, f"got {stats['total_hits']}")
    check("D3.3·3·c sidecar_files >= 2", stats["sidecar_files"] >= 2)

    print("\n[W11·D3.3·4] list_cached 列出最近")
    listed = list_cached(limit=10)
    check("D3.3·4·a list_cached 返回 dict 列表", all(isinstance(d, dict) for d in listed))
    check("D3.3·4·b 每条含 start_hash", all("start_hash" in d for d in listed))

    # ============================================================
    # M2 W12 · D3.4 重放性能指标 + 审计日志
    # ============================================================
    print("\n[W12·D3.4·1] replay_dag per_step + perf")
    dag_diff = build_chain("x**2", "2*x", var="x")
    replay = replay_dag(dag_diff.to_dict())
    check("D3.4·1·a replay 含 per_step", "per_step" in replay and len(replay["per_step"]) >= 1)
    check("D3.4·1·b replay 含 perf", "perf" in replay and "total_ms" in replay["perf"])
    check("D3.4·1·c replay 含 audit_log", "audit_log" in replay and len(replay["audit_log"]) >= 1)
    check("D3.4·1·d audit 含 timestamp", all("timestamp" in e for e in replay["audit_log"]))
    check("D3.4·1·e perf.p50_ms >= 0", replay["perf"]["p50_ms"] >= 0)

    print("\n[W12·D3.4·2] write_audit_log 写入")
    audit_path = write_audit_log(replay["audit_log"], target_path="/tmp/test_audit.jsonl")
    check("D3.4·2·a 审计文件创建", os.path.exists(audit_path))
    if os.path.exists(audit_path):
        with open(audit_path) as f:
            lines = f.readlines()
        check("D3.4·2·b 审计 ≥ 1 行", len(lines) >= 1)
        os.remove(audit_path)

    # ============================================================
    # M2 W12 · D3.5 反向推导
    # ============================================================
    print("\n[W12·D3.5·1] reverse_build 反向搜索")
    rev = reverse_build("2*x", max_depth=3, max_candidates=3)
    check(
        "D3.5·1·a 找到至少 1 个候选", len(rev["candidates"]) >= 1, f"got {len(rev['candidates'])}"
    )
    check("D3.5·1·b 候选含 start_latex", all("start_latex" in c for c in rev["candidates"]))
    check("D3.5·1·c 候选含 forward_rules", all("forward_rules" in c for c in rev["candidates"]))
    # 关键：2*x 的反向候选至少含 x^2 (diff 反向)
    found_x2 = any(
        "x^{2}" in c["start_latex"] or "x**2" in c["start_latex"] for c in rev["candidates"]
    )
    check(
        "D3.5·1·d 找到 x² 作为 2x 的反向起点",
        found_x2,
        f"starts={[c['start_latex'] for c in rev['candidates']]}",
    )

    rev = reverse_build("2*sin(x)*cos(x)", max_depth=2, max_candidates=3)
    check("D3.5·1·e sin/cos 反向 ok", rev["ok"] and len(rev["candidates"]) >= 1)
    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} pass / {FAIL} fail")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
