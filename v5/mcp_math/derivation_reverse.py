"""D3.5 反向推导（M2 W12）

依据：roadmap §13.4 W12 D3.5

从 target 倒推可能的 start（反向 BFS）：
- 正向规则 ↔ 反向规则配对：
    expand ↔ factor
    factor ↔ expand
    diff ↔ integrate
    apart ↔ cancel
    trigsimp ↔ expand_trig
- 反向 BFS 搜索：从 target 出发，应用反向规则扩展，直到找到任意"起点候选"
- 返回候选集 + 每个候选的 DAG
"""

from __future__ import annotations

import time
from collections import deque

from sympy import (
    Symbol,
    sympify,
)

from mcp_math.derivation import BUILTIN_RULES, _expr_to_latex

# ============ 反向规则定义 ============

# 正向规则 → 反向规则
REVERSE_RULES: dict[str, str | None] = {
    "expand": "factor",
    "factor": "expand",
    "diff": None,  # 反向是 integrate（特殊处理）
    "trigsimp": "expand_trig",
    "expand_trig": "trigsimp",
    "apart": "cancel",
    "cancel": "apart",
    # 其他规则无简单反向
    "simplify": None,
    "powsimp": None,
    "radsimp": None,
    "collect_x": None,
    "identity": None,
}


def _reverse_apply(forward_rule: str, expr, var):
    """对 expr 应用 forward_rule 的反向变换"""
    rev = REVERSE_RULES.get(forward_rule)
    if rev is None:
        if forward_rule == "diff":
            # integrate 是 diff 的反向
            from sympy import integrate

            try:
                return integrate(expr, var)
            except Exception:
                return None
        return None
    if rev not in BUILTIN_RULES:
        return None
    try:
        return BUILTIN_RULES[rev](expr, var)
    except Exception:
        return None


def reverse_build(
    target_latex: str,
    var: str = "x",
    max_depth: int = 3,
    timeout_s: float = 5.0,
    max_candidates: int = 5,
) -> dict:
    """从 target 反向搜索可能的 start"""
    t0 = time.time()
    try:
        target_expr = sympify(target_latex, locals={"Symbol": Symbol})
    except Exception:
        try:
            target_expr = sympify(target_latex)
        except Exception as e:
            return {
                "ok": False,
                "error": f"parse failed: {e}",
                "candidates": [],
                "explored": 0,
                "elapsed_s": time.time() - t0,
            }

    sym_var = Symbol(var)
    candidates = []
    visited = set()
    queue = deque()
    explored = 0

    visited.add(str(target_expr))
    queue.append((target_expr, []))

    while queue and len(candidates) < max_candidates:
        if time.time() - t0 > timeout_s:
            break
        current, reverse_path = queue.popleft()
        if len(reverse_path) >= max_depth:
            continue

        # 把 current 作为一个候选 start
        if len(reverse_path) > 0:
            candidates.append(
                {
                    "start_latex": _expr_to_latex(current),
                    "reverse_path": list(reverse_path),
                    "forward_rules": list(reversed(reverse_path)),
                    "depth": len(reverse_path),
                }
            )
            if len(candidates) >= max_candidates:
                break

        # 尝试所有反向规则
        for fwd_rule in ["diff", "expand", "trigsimp", "apart"]:
            rev_expr = _reverse_apply(fwd_rule, current, sym_var)
            if rev_expr is None:
                continue
            explored += 1
            expr_str = str(rev_expr)
            if expr_str in visited:
                continue
            visited.add(expr_str)
            queue.append((rev_expr, [*reverse_path, fwd_rule]))

    return {
        "ok": True,
        "target_latex": target_latex,
        "candidates": candidates,
        "explored": explored,
        "elapsed_s": round(time.time() - t0, 3),
    }
