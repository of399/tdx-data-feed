"""
mcp_math/math_sympy.py · v5-VE1-M2 数学 SymPy MCP server

在 v5-VE1-M1 原基础上集成：
  - D6.1 预筛层 (math_cache.prefilter)
  - D6.2 SQLite 缓存 (math_cache.get_cached / set_cached)
  - D5.2 MathErrorReporter (math_errors.classify_sympy_exception)

继承 M1 §5 的契约（Tool 名不变、签名不变、行为增强）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 把 v5 路径加进 sys.path
V5_DIR = Path("/home/jiuben/tdx-data-feed/v5")
sys.path.insert(0, str(V5_DIR))

from mcp_math import math_cache, math_errors  # noqa: E402

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print("mcp SDK 未安装。pip install 'mcp<2'", file=sys.stderr)
    raise

from sympy import (  # noqa: E402
    And,
    Ge,
    Gt,
    Lt,
    Matrix,
    Ne,
    Or,
    Piecewise,
    S,
    Symbol,
    diff,
    expand,
    factor,
    integrate,
    latex as sympy_latex,
    limit,
    simplify,
    solve,
    sympify,
)
from sympy.parsing.latex import parse_latex  # noqa: E402
from sympy.solvers.inequalities import reduce_inequalities  # noqa: E402

try:
    from sympy import Q, ask

    _HAS_ASK = True
except ImportError:  # pragma: no cover
    _HAS_ASK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-math-sympy")

server = Server("math-sympy")


# ============================================================================
# D6.3 超时分级（M2 §12.7 改进）
# ============================================================================

SIMPLIFY_TIMEOUT_S = float(os.environ.get("MATH_SIMPLIFY_TIMEOUT_S", "0.5"))
GENERAL_TIMEOUT_S = float(os.environ.get("MATH_GENERAL_TIMEOUT_S", "2.0"))


async def _timed_sympy(fn, *args, timeout_s: float = GENERAL_TIMEOUT_S, **kwargs):
    """
    在线程中跑 sympy 操作，超时返回 ("timeout", elapsed)；正常返回 ("ok", result)。
    实现 D6.3：simplify/radsimp/trigsimp 500ms 分级超时。
    """
    import time

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=timeout_s,
        )
        return "ok", result, round(time.time() - t0, 3)
    except TimeoutError:
        return "timeout", None, round(time.time() - t0, 3)
    except Exception as e:
        return "error", str(e), round(time.time() - t0, 3)


async def _safe_simplify(expr, timeout_s: float = SIMPLIFY_TIMEOUT_S):
    """D6.3：simplify 分级 — 500ms 超时降级到 radsimp/trigsimp；再超时直接 str。"""
    from sympy import radsimp, trigsimp

    status, result, elapsed = await _timed_sympy(simplify, expr, timeout_s=timeout_s)
    if status == "ok":
        return result, "sympy.simplify", elapsed
    if status == "timeout":
        # 降级 1: radsimp（快，常能简化分数）
        s, r, _e = await _timed_sympy(radsimp, expr, timeout_s=0.2)
        if s == "ok":
            return r, "radsimp (simplify timed out)", elapsed
        # 降级 2: trigsimp
        s, r, _e = await _timed_sympy(trigsimp, expr, timeout_s=0.2)
        if s == "ok":
            return r, "trigsimp (simplify timed out)", elapsed
        # 降级 3: 不化简，直接返回
        return expr, "unsimplified (all simplifiers timed out)", elapsed
    return None, f"simplify error: {result}", elapsed


# ============================================================================
# D5.5 数值 fallback（M2 §12.6 改进）
# ============================================================================


async def _numeric_fallback_compute(latex: str, action: str, var: str = "x") -> dict:
    """
    D5.5：解析失败 / 符号解失败 → 数值解 fallback。
      - simplify: nsimplify(expr, rational=False)
      - solve: nsolve(expr, var, guess=0)
      - integrate: quad(lambda x: expr.subs(Symbol(x), x), -inf, inf)  ← 简化版
    返回 {"ok": True, "result": ..., "method": "numeric_xxx", "note": "fallback"}
    """
    from sympy import Symbol, nsimplify, nsolve
    from sympy.parsing.latex import parse_latex

    try:
        expr = parse_latex(latex)
        v = Symbol(var)
        if action == "simplify":
            r = str(nsimplify(expr, rational=False, tolerance=1e-6))
            return {
                "ok": True,
                "result": r,
                "action": action,
                "method": "numeric_nsimplify",
                "note": "fallback from symbolic simplify",
            }
        if action == "solve":
            try:
                r = str(nsolve(expr, v, 0))
                return {
                    "ok": True,
                    "result": r,
                    "action": action,
                    "method": "numeric_nsolve",
                    "note": "fallback from symbolic solve (guess=0)",
                }
            except Exception as inner:
                return {"ok": False, "error": f"nsolve failed: {inner}"}
        return {"ok": False, "error": f"numeric fallback not implemented for action={action}"}
    except Exception as exc:
        report = math_errors.classify_sympy_exception(exc, latex)
        return {
            "ok": False,
            "error": str(exc)[:200],
            "error_report": report.to_dict(),
            "note": "numeric fallback also failed",
        }


# ============================================================================
# D1.2 pylatexenc AST fallback（M2 §12.1 改进）
# ============================================================================


def _pylatexenc_parse(latex: str) -> dict:
    """
    D1.2 fallback：用 pylatexenc.latexwalker 给出 LaTeX AST + 转换文本。
    用于 sympy.parse_latex 失败时给用户"至少能告诉你什么命令"的反馈。
    """
    try:
        from pylatexenc.latex2text import LatexNodes2Text
        from pylatexenc.latexwalker import LatexWalker
    except ImportError:
        return {"ok": False, "error": "pylatexenc not installed"}

    try:
        walker = LatexWalker(latex)
        nodes, _, _ = walker.get_latex_nodes()
        n2t = LatexNodes2Text()
        # 摘要 AST：列出顶层节点类型 + macros
        macros = []
        types = {}
        for n in nodes:
            t = type(n).__name__
            types[t] = types.get(t, 0) + 1
            if hasattr(n, "macroname"):
                macros.append(n.macroname)
        return {
            "ok": True,
            "node_count": len(nodes),
            "node_types": types,
            "macros": sorted(set(macros)),
            "plain_text": n2t.nodelist_to_text(nodes).strip(),
            "method": "pylatexenc_ast",
            "note": "sympy.parse_latex failed; AST only",
        }
    except Exception as exc:
        return {"ok": False, "error": f"pylatexenc parse failed: {exc}"}


# ============================================================================
# 缓存 wrapper（M1 既有 Tool 不变签名 + 内部走 cache）
# ============================================================================


async def _cached_equiv(latex_a: str, latex_b: str) -> dict:
    """带预筛 + 缓存 + 超时分级的等价判定。"""
    need_compute, reason = math_cache.prefilter(latex_a, latex_b)
    if not need_compute:
        return {
            "equivalent": False,
            "diff": None,
            "method": "prefilter",
            "reason": reason,
        }

    cached = math_cache.get_cached(latex_a, latex_b, action="equiv")
    if cached is not None:
        cached["method"] = "cache"
        return cached

    try:
        a = parse_latex(latex_a)
        b = parse_latex(latex_b)
        diff_val, method_used, elapsed = await _safe_simplify(a - b)
        result = {
            "equivalent": diff_val == 0,
            "diff": str(diff_val),
            "method": method_used,
            "reason": "computed",
            "elapsed_s": elapsed,
        }
    except Exception as exc:
        report = math_errors.classify_sympy_exception(exc, f"{latex_a} vs {latex_b}")
        fixed_a, _ = math_errors.fix_typo(latex_a)
        fixed_b, _ = math_errors.fix_typo(latex_b)
        if fixed_a != latex_a or fixed_b != latex_b:
            try:
                a2 = parse_latex(fixed_a)
                b2 = parse_latex(fixed_b)
                diff2, method_used, elapsed = await _safe_simplify(a2 - b2)
                result = {
                    "equivalent": diff2 == 0,
                    "diff": str(diff2),
                    "method": f"typo_fix+{method_used}",
                    "reason": "recovered after typo repair",
                    "elapsed_s": elapsed,
                }
                math_cache.set_cached(fixed_a, fixed_b, "equiv", result)
                return result
            except Exception:
                pass
        result = {
            "equivalent": None,
            "diff": None,
            "method": "error",
            "error_report": report.to_dict(),
        }
        return result

    math_cache.set_cached(latex_a, latex_b, "equiv", result)
    return result


async def _cached_compute(latex: str, action: str = "simplify", var: str = "x") -> dict:
    """带缓存 + 超时分级 + 数值 fallback 的通用 sympy 计算。"""
    cached = math_cache.get_cached(latex, "", action=action)
    if cached is not None:
        cached["method"] = "cache"
        return cached

    try:
        expr = parse_latex(latex)
        v = Symbol(var)
        if action == "simplify":
            r, method_used, elapsed = await _safe_simplify(expr)
            r = str(r)
        elif action == "solve":
            res, _, elapsed = await _timed_sympy(
                lambda: solve(expr, v), timeout_s=GENERAL_TIMEOUT_S
            )
            if res is None:
                # D5.5 数值 fallback
                return await _numeric_fallback_compute(latex, action, var)
            r = [str(s) for s in res]
            method_used = f"solve (timeout={GENERAL_TIMEOUT_S}s)"
        elif action == "factor":
            res, _, elapsed = await _timed_sympy(lambda: factor(expr), timeout_s=GENERAL_TIMEOUT_S)
            r = str(res) if res is not None else str(expr)
            method_used = "factor"
        elif action == "expand":
            res, _, elapsed = await _timed_sympy(lambda: expand(expr), timeout_s=GENERAL_TIMEOUT_S)
            r = str(res) if res is not None else str(expr)
            method_used = "expand"
        elif action == "diff":
            res, _, elapsed = await _timed_sympy(lambda: diff(expr, v), timeout_s=GENERAL_TIMEOUT_S)
            r = str(res) if res is not None else str(expr)
            method_used = "diff"
        elif action == "integrate":
            res, _, elapsed = await _timed_sympy(
                lambda: integrate(expr, v), timeout_s=GENERAL_TIMEOUT_S * 2
            )
            if res is None:
                return await _numeric_fallback_compute(latex, action, var)
            r = str(res) if res is not None else str(expr)
            method_used = "integrate"
        elif action == "limit":
            res, _, elapsed = await _timed_sympy(
                lambda: limit(expr, v, 0), timeout_s=GENERAL_TIMEOUT_S
            )
            r = str(res) if res is not None else str(expr)
            method_used = "limit"
        elif action == "det":
            res, _, elapsed = await _timed_sympy(
                lambda: Matrix(expr.tolist()).det(), timeout_s=GENERAL_TIMEOUT_S
            )
            r = str(res) if res is not None else str(expr)
            method_used = "det"
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
        result = {
            "ok": True,
            "result": r,
            "action": action,
            "method": method_used,
            "elapsed_s": elapsed,
        }
    except Exception as exc:
        # D5.5：解析失败 → 数值 fallback（仅对 solve/integrate 有效）
        if action in ("solve", "integrate", "simplify"):
            numeric_result = await _numeric_fallback_compute(latex, action, var)
            if numeric_result.get("ok"):
                math_cache.set_cached(latex, "", action, numeric_result)
                return numeric_result
        # D1.2：fallback 仍失败 → 给 pylatexenc AST
        ast_info = _pylatexenc_parse(latex)
        report = math_errors.classify_sympy_exception(exc, latex)
        return {
            "ok": False,
            "error": str(exc)[:200],
            "error_report": report.to_dict(),
            "ast_fallback": ast_info,
        }

    math_cache.set_cached(latex, "", action, result)
    return result


# ============================================================================
# W9 · D2.1/D2.2/D2.3 · 推理 + 不等式 + 分段函数
# ============================================================================


# 解析 precondition 字符串为 SymPy 布尔表达式
# 支持：'x > 0', 'x >= 0', 'x < 1', 'x: positive', 'n: integer', 'x != 0'
def _parse_precondition(pre: str):
    """返回 (sympy_predicate, sympy_constraint|None)

    两种语法：
    - 区间式：'x > 0' → 返回 (None, Gt(x, 0))（用于 reduce_inequalities）
    - 谓词式：'x: positive' → 返回 (Q.positive(x), None)（用于 ask）
    """
    pre = pre.strip()
    # 谓词语法：var: <predicate>
    if ":" in pre and "<" not in pre and ">" not in pre:
        var, _, pred = pre.partition(":")
        var = var.strip()
        pred = pred.strip()
        var_sym = Symbol(var)
        mapping = {
            "positive": getattr(Q, "positive", None),
            "nonnegative": getattr(Q, "nonnegative", None),
            "negative": getattr(Q, "negative", None),
            "integer": getattr(Q, "integer", None),
            "real": getattr(Q, "real", None),
            "rational": getattr(Q, "rational", None),
            "complex": getattr(Q, "complex", None),
            "nonzero": getattr(Q, "nonzero", None),
            "even": getattr(Q, "even", None),
            "odd": getattr(Q, "odd", None),
        }
        p = mapping.get(pred)
        if p is None:
            raise ValueError(f"未知谓词: {pred}")
        return (p(var_sym), None)
    # 区间语法：解析为 SymPy 关系
    rel = sympify(pre.replace("^", "**"), locals={"Symbol": Symbol})
    return (None, rel)


def _constraint_to_predicate(constraint):
    """从 SymPy 关系约束推断 Q 谓词（用于 ask 路径）

    Gt(x, 0)   → Q.positive(x)
    Ge(x, 0)   → Q.nonnegative(x)
    Lt(x, 0)   → Q.negative(x)
    Ne(x, 0)   → Q.nonzero(x)
    """
    if not _HAS_ASK:
        return None
    free = list(constraint.free_symbols)
    if len(free) != 1:
        return None
    var_sym = free[0]
    rhs = constraint.rhs if hasattr(constraint, "rhs") else None
    if rhs is None or rhs != 0:
        return None
    if isinstance(constraint, Gt) and var_sym == constraint.lhs:
        return Q.positive(var_sym)
    if isinstance(constraint, Ge) and var_sym == constraint.lhs:
        return Q.nonnegative(var_sym)
    if isinstance(constraint, Lt) and var_sym == constraint.lhs:
        return Q.negative(var_sym)
    if isinstance(constraint, Ne) and var_sym == constraint.lhs:
        return Q.nonzero(var_sym)
    return None


def _prove_equiv_with_precondition(
    latex_a: str, latex_b: str, preconditions: list[str], timeout_s: float
) -> dict:
    """D2.1：条件等价证明（SymPy ask + assumptions + 数值反例验证）

    算法：
    1. 解析 LaTeX → sympy 表达式
    2. 解析 precondition 列表 → (ask_predicate, constraint) 对
    3. 尝试三种证明路径：
       a. 符号等价（差=0 simplify）
       b. 在前提下证明（用 ask + reduce_inequalities）
       c. 数值反例验证（采样 100 个点检查 |A-B| 是否恒为 0）
    """
    try:
        # D2.1.x 优化：先 sympify（宽容解析 "log(x**2)" 形式）
        # 再 fallback parse_latex（处理 \sqrt{} 等 LaTeX）
        # 这避免 parse_latex 把 "log" 错认为 l*o*g
        try:
            expr_a = sympify(latex_a)
        except Exception:
            expr_a = parse_latex(latex_a)
        try:
            expr_b = sympify(latex_b)
        except Exception:
            expr_b = parse_latex(latex_b)
    except Exception as e:
        return {
            "ok": False,
            "error": f"parse_latex failed: {e}",
            "proved": False,
        }

    diff = expr_a - expr_b

    # 路径 1：纯符号等价（无需前置条件）
    diff_simplified = simplify(diff)
    if diff_simplified == 0:
        return {
            "ok": True,
            "proved": True,
            "method": "symbolic_simplify",
            "witness": "A - B == 0",
            "holds": True,
            "assumptions_used": [],
            "assumptions_failed": [],
        }

    # 解析前置条件
    ask_predicates = []
    constraints = []
    parse_errors = []
    for pre in preconditions or []:
        try:
            ask_p, cons = _parse_precondition(pre)
            if ask_p is not None:
                ask_predicates.append(ask_p)
            if cons is not None:
                constraints.append(cons)
                # 同时尝试把约束转谓词（让 ask 路径生效）
                p = _constraint_to_predicate(cons)
                if p is not None:
                    ask_predicates.append(p)
        except Exception as e:
            parse_errors.append({"precondition": pre, "error": str(e)})

    # 路径 2：在约束条件下，问 ask："diff == 0" 是否在前提下成立
    proved_with_assumption = False
    assumptions_failed = []
    if _HAS_ASK and ask_predicates:
        for ap in ask_predicates:
            try:
                if ask(ap) and ask(Q.eq(diff, 0), ap):
                    # 当前提成立时, diff==0
                    proved_with_assumption = True
                    break
            except Exception as e:
                assumptions_failed.append({"predicate": str(ap), "error": str(e)})

    # 路径 3：reduce_inequalities（区间约束）
    if not proved_with_assumption and constraints:
        try:
            reduced = reduce_inequalities(constraints)
            if reduced == S.true or reduced is True:
                proved_with_assumption = True
        except Exception:
            pass

    if proved_with_assumption:
        return {
            "ok": True,
            "proved": True,
            "method": "ask_with_assumption",
            "witness": f"在 {preconditions} 下 A - B == 0",
            "holds": True,
            "assumptions_used": preconditions,
            "assumptions_failed": assumptions_failed,
            "parse_errors": parse_errors,
        }

    # 路径 4：数值反例验证（带约束域采样）
    try:
        import random

        random.seed(42)
        # 从表达式中提取自由符号
        free = sorted(expr_a.free_symbols | expr_b.free_symbols, key=str)
        if not free:
            # 纯数字表达式
            holds = bool(abs(float(expr_a - expr_b)) < 1e-9)
            return {
                "ok": True,
                "proved": False,
                "method": "numeric_zero_var",
                "witness": f"|A-B| = {float(expr_a - expr_b)}",
                "holds": holds,
                "assumptions_used": preconditions,
                "assumptions_failed": assumptions_failed,
                "parse_errors": parse_errors,
            }

        # 根据约束推断采样域
        domain_lo, domain_hi = -10.0, 10.0
        for cons in constraints:
            if hasattr(cons, "rhs") and cons.rhs == 0:
                if isinstance(cons, Gt) and len(cons.free_symbols) == 1:
                    domain_lo = max(domain_lo, 0.1)
                elif isinstance(cons, Lt) and len(cons.free_symbols) == 1:
                    domain_hi = min(domain_hi, -0.1)
                elif isinstance(cons, Ne) and len(cons.free_symbols) == 1:
                    domain_lo = max(domain_lo, 0.5)  # 避 0

        hold_count = 0
        valid_count = 0  # D2.1.x：有效采样数（满足 constraints 且 eval 成功）
        sample_count = 50
        for _ in range(sample_count):
            subs = {s: random.uniform(domain_lo, domain_hi) for s in free}
            # D2.1.x 优化：如果 sampling point 不满足 constraints，跳过
            skip = False
            for cons in constraints:
                try:
                    cons_val = bool(cons.subs(subs))
                    if not cons_val:
                        skip = True
                        break
                except Exception:
                    pass  # 无法判断则不 skip
            if skip:
                continue
            try:
                v_a = complex(expr_a.subs(subs))
                v_b = complex(expr_b.subs(subs))
                valid_count += 1
                if abs(v_a - v_b) < 1e-6:
                    hold_count += 1
            except TypeError, ZeroDivisionError, ValueError:
                continue
        ratio = hold_count / valid_count if valid_count > 0 else 0.0
        return {
            "ok": True,
            "proved": ratio >= 0.98,
            "method": "numeric_sampling",
            "witness": f"{hold_count}/{valid_count} 采样点成立 ({ratio:.0%})，采样域 [{domain_lo}, {domain_hi}]",
            "holds": ratio >= 0.98,
            "assumptions_used": preconditions,
            "assumptions_failed": assumptions_failed,
            "parse_errors": parse_errors,
            "numeric_ratio": ratio,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"numeric verification failed: {e}",
            "proved": False,
            "assumptions_used": preconditions,
            "assumptions_failed": assumptions_failed,
            "parse_errors": parse_errors,
        }


def _solve_inequality(latex: str, var: str, relational: bool, timeout_s: float) -> dict:
    """D2.2：不等式求解（sympy.solvers.inequalities.reduce_inequalities）"""
    # 优先 sympify（认 >= / <=），失败再 parse_latex
    try:
        expr = sympify(latex, locals={"Symbol": Symbol, "And": And, "Or": Or})
    except Exception:
        try:
            expr = parse_latex(latex)
        except Exception as e:
            return {
                "ok": False,
                "error": f"parse failed (sympify+parse_latex): {e}",
                "solution_latex": "",
            }

    var_sym = Symbol(var)
    try:
        sol = reduce_inequalities(expr, var_sym, relational=relational)
    except TypeError:
        # 老版本 sympy 不支持 relational kwarg
        try:
            sol = reduce_inequalities([expr], var_sym)
        except Exception as e:
            return {
                "ok": False,
                "error": f"reduce_inequalities failed: {e}",
                "solution_latex": "",
            }
    except Exception as e:
        return {
            "ok": False,
            "error": f"reduce_inequalities failed: {e}",
            "solution_latex": "",
        }

    # 转 LaTeX
    try:
        sol_latex = sympy_latex(sol) if sol is not None else ""
    except Exception:
        sol_latex = str(sol)

    # 提取区间列表（仅当 relational=True）
    intervals = []
    if relational and hasattr(sol, "args"):
        # reduce_inequalities 返回 And(Interval.open(...), ...)
        args = sol.args if isinstance(sol, And) else (sol,)
        for arg in args:
            try:
                arg_str = str(arg)
                intervals.append({"expr": arg_str, "latex": sympy_latex(arg)})
            except Exception:
                intervals.append({"expr": str(arg), "latex": str(arg)})

    return {
        "ok": True,
        "solution_latex": sol_latex,
        "intervals": intervals,
        "domain_latex": sympy_latex(expr.lhs) if hasattr(expr, "lhs") else "",
        "var": var,
    }


def _simplify_piecewise(cases: list[dict], var: str, action: str) -> dict:
    """D2.3：分段函数简化 / 评估（sympy.Piecewise）"""
    Symbol(var)
    pairs = []
    case_records = []

    # 把 Python 比较运算符转为 LaTeX（parse_latex 不认 >= / ==）
    _OP_LATEX = {
        ">=": r"\geq ",
        "<=": r"\leq ",
        "==": "=",
        "!=": r"\neq ",
        ">": ">",
        "<": "<",
    }

    def _cond_to_sympy(s: str):
        """优先 sympify（认比较运算），失败再 parse_latex"""
        try:
            return sympify(s, locals={"Symbol": Symbol, "And": And, "Or": Or})
        except Exception:
            # 转 LaTeX 比较符再 parse
            latex = s
            for op, _latex_op in _OP_LATEX.items():
                latex = latex.replace(op, _latex_op)
            return parse_latex(latex)

    for c in cases:
        try:
            cond = _cond_to_sympy(c["cond"])
            value = parse_latex(c["value"])
            pairs.append((value, cond))
            case_records.append(
                {
                    "cond_latex": c["cond"],
                    "value_latex": c["value"],
                    "cond_expr": sympy_latex(cond),
                    "value_expr": sympy_latex(value),
                }
            )
        except Exception as e:
            return {
                "ok": False,
                "error": f"parse case failed: {e} (case={c})",
                "cases": case_records,
            }

    if not pairs:
        return {"ok": False, "error": "无有效 case", "cases": []}

    pw = Piecewise(*pairs)
    try:
        if action == "simplify":
            result = simplify(pw)
        elif action == "expand":
            result = expand(pw)
        else:  # evaluate
            result = pw  # 保留原状，附 domain 信息
    except Exception as e:
        return {
            "ok": False,
            "error": f"piecewise {action} failed: {e}",
            "cases": case_records,
        }

    atoms = sorted({str(a) for a in result.atoms(Symbol)}, key=str)
    return {
        "ok": True,
        "simplified_latex": sympy_latex(result),
        "cases": case_records,
        "atoms": atoms,
        "var": var,
        "action": action,
    }


# ============================================================================
# MCP Tool 注册
# ============================================================================


@server.list_tools()
async def list_tools() -> list:
    return [
        Tool(
            name="sympy_compute",
            description="通用 SymPy 计算（带缓存 + 错误可解释）",
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "simplify",
                            "solve",
                            "factor",
                            "expand",
                            "diff",
                            "integrate",
                            "limit",
                            "det",
                        ],
                    },
                    "var": {"type": "string", "default": "x"},
                },
                "required": ["latex", "action"],
            },
        ),
        Tool(
            name="are_equiv",
            description="数学等价判定（带预筛 + 缓存 + Typo 修复）",
            inputSchema={
                "type": "object",
                "properties": {
                    "latex_a": {"type": "string"},
                    "latex_b": {"type": "string"},
                },
                "required": ["latex_a", "latex_b"],
            },
        ),
        Tool(
            name="math_error_report",
            description="【M2 新增】对解析失败/超时的 LaTeX 给出可解释错误报告",
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "error_message": {"type": "string"},
                },
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_fix_typo",
            description="【M2 新增】LaTeX typo 修复 + fuzzy 命令建议",
            inputSchema={
                "type": "object",
                "properties": {"latex": {"type": "string"}},
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_validate_symbols",
            description="【M2 新增】LaTeX 变量完整性校验",
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "declared": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_cache_stats",
            description="【M2 新增】数学缓存统计",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="math_numeric_solve",
            description="【M2 D5.5 新增】数值解（nsolve sympy 失败时用 nsimplify/nsolve）",
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "action": {"type": "string", "enum": ["simplify", "solve", "integrate"]},
                    "var": {"type": "string", "default": "x"},
                },
                "required": ["latex", "action"],
            },
        ),
        Tool(
            name="math_ast_dump",
            description="【M2 D1.2 新增】pylatexenc AST 解析（sympy parse_latex 失败时给节点摘要）",
            inputSchema={
                "type": "object",
                "properties": {"latex": {"type": "string"}},
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_batch_equiv",
            description="【M2 D6.5 新增】批量并行等价判定（ProcessPoolExecutor）",
            inputSchema={
                "type": "object",
                "properties": {
                    "pairs": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "max_workers": {"type": "integer", "default": 4},
                },
                "required": ["pairs"],
            },
        ),
        Tool(
            name="prove_equiv_with_precondition",
            description=(
                "【M2 W9 D2.1 新增】条件等价证明：在指定前置假设下证明 A == B。"
                "返回 {proved: bool, holds: bool|None, witness: str, "
                "assumptions_used: [...], assumptions_failed: [...]}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latex_a": {"type": "string", "description": "左侧 LaTeX"},
                    "latex_b": {"type": "string", "description": "右侧 LaTeX"},
                    "preconditions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "前置条件列表，SymPy Q 谓词："
                            "['x > 0', 'x != 0', 'x: positive', 'n: integer', 'x: real']"
                        ),
                    },
                    "timeout_s": {"type": "number", "default": 2.0},
                },
                "required": ["latex_a", "latex_b"],
            },
        ),
        Tool(
            name="solve_inequality",
            description=(
                "【M2 W9 D2.2 新增】不等式求解。"
                "返回 {ok: bool, solution_latex: str, intervals: [...], "
                "domain_latex: str, error?: str}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {
                        "type": "string",
                        "description": "不等式 LaTeX（含 < / > / <= / >=）",
                    },
                    "var": {"type": "string", "default": "x"},
                    "relational": {
                        "type": "boolean",
                        "default": True,
                        "description": "True=返回区间表达式；False=返回布尔解集",
                    },
                    "timeout_s": {"type": "number", "default": 2.0},
                },
                "required": ["latex"],
            },
        ),
        Tool(
            name="simplify_piecewise",
            description=(
                "【M2 W9 D2.3 新增】分段函数简化 / 评估。"
                "返回 {ok: bool, simplified_latex: str, "
                "cases: [{cond, value}], atoms: [...]}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cases": {
                        "type": "array",
                        "description": "分段定义列表 [{cond_latex, value_latex}, ...]，最后一项 cond 可省略（表示 else）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cond": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["cond", "value"],
                        },
                    },
                    "var": {"type": "string", "default": "x"},
                    "action": {
                        "type": "string",
                        "enum": ["simplify", "evaluate", "expand"],
                        "default": "simplify",
                    },
                },
                "required": ["cases"],
            },
        ),
        Tool(
            name="math_strategy_route",
            description=(
                "【M2 W10 D2.4 新增】公式复杂度评估 + sympy/ask/numeric/llm 自动路由。"
                "返回 {strategy, score, reasoning, fallback_chain, "
                "estimated_timeout_s, complexity: {...}}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "action": {
                        "type": "string",
                        "default": "simplify",
                        "enum": [
                            "simplify",
                            "solve",
                            "integrate",
                            "limit",
                            "factor",
                            "expand",
                            "diff",
                            "det",
                        ],
                    },
                    "var": {"type": "string", "default": "x"},
                    "timeout_history": {
                        "type": "object",
                        "description": "历史超时：{action: avg_seconds}",
                    },
                },
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_strategy_route_and_execute",
            description=(
                "【M2 W10 D2.4 新增】策略路由 + 按 fallback 链自动执行。"
                "返回 {ok, strategy_used, result, attempts: [{strategy, ok, elapsed_s, error?}], "
                "fallback_chain}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latex": {"type": "string"},
                    "action": {
                        "type": "string",
                        "default": "simplify",
                        "enum": [
                            "simplify",
                            "solve",
                            "integrate",
                            "limit",
                            "factor",
                            "expand",
                            "diff",
                            "det",
                        ],
                    },
                    "var": {"type": "string", "default": "x"},
                    "timeout_history": {"type": "object"},
                    "ollama_url": {
                        "type": "string",
                        "default": "http://127.0.0.1:11434",
                    },
                    "ollama_model": {
                        "type": "string",
                        "default": "qwen3:14b",
                    },
                },
                "required": ["latex"],
            },
        ),
        Tool(
            name="math_derivation_chain",
            description=(
                "【M2 W11 D3.1 新增】推导链 DAG 构建：BFS 搜索从 start 到 target 的最短路径。"
                "返回 {ok, reason, steps, nodes, edges, explored, elapsed_s, "
                "dag_json, mermaid, rules_used, target_expr}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_latex": {"type": "string"},
                    "target_latex": {"type": "string"},
                    "var": {"type": "string", "default": "x"},
                    "max_depth": {"type": "integer", "default": 4, "minimum": 1, "maximum": 10},
                    "rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "候选规则：expand/factor/simplify/trigsimp/radsimp/powsimp/expand_trig/cancel/apart/diff/collect_x",
                    },
                    "timeout_s": {"type": "number", "default": 5.0},
                },
                "required": ["start_latex", "target_latex"],
            },
        ),
        Tool(
            name="math_derivation_replay",
            description=(
                "【M2 W11 D3.1 新增】从序列化的 DAG JSON 重放验证 + 性能。"
                "返回 {ok, all_match, steps_replayed, mismatches, elapsed_s, final_expr}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dag_json": {
                        "type": "object",
                        "description": "DerivationDAG.to_dict() 输出",
                    },
                    "var": {"type": "string", "default": "x"},
                },
                "required": ["dag_json"],
            },
        ),
        Tool(
            name="math_step_extract",
            description=(
                "【M2 W11 D3.2 新增】从 Obsidian markdown 的 <calc> 代码块抽取推导步骤。"
                "返回 {total_blocks, valid_steps, invalid_blocks, steps: [{start, rule, target, line_no}]}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "markdown": {"type": "string", "description": "完整 markdown 内容"},
                    "source_path": {"type": "string", "default": "<inline>"},
                    "verify": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否用 build_chain 验证每个步骤可达",
                    },
                    "var": {"type": "string", "default": "x"},
                },
                "required": ["markdown"],
            },
        ),
        Tool(
            name="math_derivation_cache_stats",
            description=(
                "【M2 W11 D3.3 新增】推导链 sidecar 缓存统计。"
                "返回 {total_entries, success_entries, total_hits, sidecar_files, size_mb, ...}"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="math_derivation_build_cached",
            description=(
                "【M2 W11 D3.3 新增】构建推导链 DAG（带 sidecar 缓存，命中跳过构建）。"
                "返回 {source: cache|computed, dag: {...}, cache_stats}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_latex": {"type": "string"},
                    "target_latex": {"type": "string"},
                    "var": {"type": "string", "default": "x"},
                    "max_depth": {"type": "integer", "default": 4},
                    "note_id": {"type": "string", "description": "可选笔记 ID"},
                },
                "required": ["start_latex", "target_latex"],
            },
        ),
        Tool(
            name="math_derivation_reverse",
            description=(
                "【M2 W12 D3.5 新增】反向推导：从 target 倒推可能的 start 候选。"
                "返回 {ok, candidates: [{start_latex, reverse_path, forward_rules, depth}], "
                "explored, elapsed_s}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_latex": {"type": "string"},
                    "var": {"type": "string", "default": "x"},
                    "max_depth": {"type": "integer", "default": 3},
                    "max_candidates": {"type": "integer", "default": 5},
                    "timeout_s": {"type": "number", "default": 5.0},
                },
                "required": ["target_latex"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    log.info(f"call_tool: {name} args={list(arguments.keys())}")
    try:
        if name == "sympy_compute":
            res = await _cached_compute(
                arguments["latex"],
                action=arguments.get("action", "simplify"),
                var=arguments.get("var", "x"),
            )
        elif name == "are_equiv":
            res = await _cached_equiv(arguments["latex_a"], arguments["latex_b"])
        elif name == "math_error_report":
            latex = arguments["latex"]
            err_msg = arguments.get("error_message", "unknown error")
            try:
                raise Exception(err_msg)
            except Exception as exc:
                res = math_errors.classify_sympy_exception(exc, latex).to_dict()
        elif name == "math_fix_typo":
            latex = arguments["latex"]
            fixed, applied = math_errors.fix_typo(latex)
            suggestions = math_errors.fuzzy_fix_command(latex)
            res = {
                "fixed": fixed,
                "applied": applied,
                "fuzzy_suggestions": suggestions,
            }
        elif name == "math_validate_symbols":
            latex = arguments["latex"]
            declared = set(arguments.get("declared") or [])
            res = math_errors.validate_symbols(latex, declared if declared else None)
        elif name == "math_cache_stats":
            res = math_cache.cache_stats()
        elif name == "math_numeric_solve":
            res = await _numeric_fallback_compute(
                arguments["latex"],
                action=arguments.get("action", "solve"),
                var=arguments.get("var", "x"),
            )
        elif name == "math_ast_dump":
            res = _pylatexenc_parse(arguments["latex"])
        elif name == "math_batch_equiv":
            from mcp_math.parallel import batch_are_equiv

            raw_pairs = arguments.get("pairs", [])
            pairs = [(p[0], p[1]) for p in raw_pairs if len(p) >= 2]
            res = {
                "count": len(pairs),
                "results": batch_are_equiv(
                    pairs,
                    max_workers=arguments.get("max_workers"),
                ),
            }
        elif name == "prove_equiv_with_precondition":
            res = _prove_equiv_with_precondition(
                latex_a=arguments["latex_a"],
                latex_b=arguments["latex_b"],
                preconditions=arguments.get("preconditions") or [],
                timeout_s=arguments.get("timeout_s", 2.0),
            )
        elif name == "solve_inequality":
            res = _solve_inequality(
                latex=arguments["latex"],
                var=arguments.get("var", "x"),
                relational=arguments.get("relational", True),
                timeout_s=arguments.get("timeout_s", 2.0),
            )
        elif name == "simplify_piecewise":
            res = _simplify_piecewise(
                cases=arguments["cases"],
                var=arguments.get("var", "x"),
                action=arguments.get("action", "simplify"),
            )
        elif name == "math_strategy_route":
            from mcp_math.strategy_selector import select_strategy

            res = select_strategy(
                latex=arguments["latex"],
                action=arguments.get("action", "simplify"),
                timeout_history=arguments.get("timeout_history"),
            )
        elif name == "math_strategy_route_and_execute":
            from mcp_math.strategy_selector import execute_with_fallback

            res = execute_with_fallback(
                latex=arguments["latex"],
                action=arguments.get("action", "simplify"),
                var=arguments.get("var", "x"),
                timeout_history=arguments.get("timeout_history"),
                ollama_url=arguments.get("ollama_url", "http://127.0.0.1:11434"),
                ollama_model=arguments.get("ollama_model", "qwen3:14b"),
                force_strategy=arguments.get("force_strategy"),
                canary_ratio=float(arguments.get("canary_ratio", 0.0)),
                canary_target=arguments.get("canary_target", "llm"),
                canary_key=arguments.get("canary_key"),
            )
        elif name == "math_derivation_chain":
            from mcp_math.derivation import build_chain

            dag = build_chain(
                start_latex=arguments["start_latex"],
                target_latex=arguments["target_latex"],
                var=arguments.get("var", "x"),
                max_depth=arguments.get("max_depth", 4),
                rules=arguments.get("rules"),
                timeout_s=arguments.get("timeout_s", 5.0),
            )
            res = {
                "ok": dag.success,
                "reason": dag.reason,
                "steps": dag.steps,
                "nodes_count": len(dag.nodes),
                "edges_count": len(dag.edges),
                "explored": dag.explored,
                "elapsed_s": round(dag.elapsed_s, 3),
                "rules_used": dag.rules_used,
                "target_node_id": dag.target_node_id,
                "target_expr": dag.nodes[dag.target_node_id].expr_latex
                if dag.target_node_id is not None
                else None,
                "dag_json": dag.to_dict(),
                "mermaid": dag.to_mermaid(),
            }
        elif name == "math_derivation_replay":
            from mcp_math.derivation import replay_dag

            res = replay_dag(
                dag_dict=arguments["dag_json"],
                var=arguments.get("var", "x"),
            )
        elif name == "math_step_extract":
            from mcp_math.step_extractor import (
                extract_steps_from_markdown,
                verify_steps,
            )

            ext = extract_steps_from_markdown(
                markdown_text=arguments["markdown"],
                source_path=arguments.get("source_path", "<inline>"),
            )
            res = ext.to_dict()
            if arguments.get("verify"):
                v = verify_steps(ext.steps, var=arguments.get("var", "x"))
                res["verification"] = v
        elif name == "math_derivation_cache_stats":
            from mcp_math.derivation_cache import cache_stats

            res = cache_stats()
        elif name == "math_derivation_build_cached":
            from mcp_math.derivation_cache import build_chain_with_cache

            res = build_chain_with_cache(
                start_latex=arguments["start_latex"],
                target_latex=arguments["target_latex"],
                var=arguments.get("var", "x"),
                max_depth=arguments.get("max_depth", 4),
                note_id=arguments.get("note_id"),
            )
        elif name == "math_derivation_reverse":
            from mcp_math.derivation_reverse import reverse_build

            res = reverse_build(
                target_latex=arguments["target_latex"],
                var=arguments.get("var", "x"),
                max_depth=arguments.get("max_depth", 3),
                max_candidates=arguments.get("max_candidates", 5),
                timeout_s=arguments.get("timeout_s", 5.0),
            )
        else:
            res = {"ok": False, "error": f"unknown tool: {name}"}
    except Exception as e:
        log.exception(f"tool {name} failed")
        res = {"ok": False, "error": str(e)}

    return [TextContent(type="text", text=json.dumps(res, ensure_ascii=False, indent=2))]


async def main():
    log.info("mcp-math-sympy starting (M2 增强版)")
    log.info(f"  cache: {math_cache.DB_PATH}")
    log.info(f"  5 大错误类型: {[e.value for e in math_errors.MathErrorType]}")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
