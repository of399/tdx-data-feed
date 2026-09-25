"""D2.4 策略选择器（M2 W10）

公式复杂度评估 + sympy/ask/numeric/llm 自动路由。

路由策略（按总分 S）：
    S < 30      → sympy   （直接 _cached_compute）
    30 ≤ S < 60 → ask     （prove_equiv_with_precondition / solve_inequality）
    60 ≤ S < 80 → numeric （math_numeric_solve，数值 fallback）
    S ≥ 80      → llm     （Ollama deepseek-r1:14b 兜底）

Fallback 链：sympy → ask → numeric → llm

依据：roadmap §13.4 W10 D2.4 + v5-implementation-plan.md 任务卡 D2.4
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ============ 评分权重（可被 env 覆盖）============

WEIGHT_LENGTH = 0.5  # 每字符
WEIGHT_DEPTH = 10  # 每层嵌套
WEIGHT_FREE_VAR = 5  # 每个自由变量
WEIGHT_INTEGRAL = 35  # 含 \int（积分复杂）
WEIGHT_SUM_PROD = 40  # 含 \sum / \prod
WEIGHT_LIMIT = 25  # 含 \lim
WEIGHT_MATRIX = 18  # 矩阵
WEIGHT_PIECEWISE = 28  # 分段函数
WEIGHT_SPECIAL_FUNC = 20  # 特殊函数（Gamma/Zeta/erf）
WEIGHT_HIGH_POWER = 10  # 高阶幂（>= 5）
WEIGHT_INFINITY = 12  # 含 \infty

# 复合信号加成：多个重型符号叠加 → 适度加分（避免单项漏判）
# 但避免过度（3 重型直接跳 llm）：每多 1 个重型 +8 分
COMPLEX_MULTI_BONUS = 8  # 每个重型信号叠加（≥ 2 时启用）

# 重型信号定义（含这些中任一即视为"重型"）
HEAVY_SIGNALS = (
    "integral",
    "sum_product",
    "limit",
    "matrix",
    "piecewise",
    "special_func",
    "infinity",
)

THRESHOLD_ASK = 30
THRESHOLD_NUMERIC = 55
THRESHOLD_LLM = 90


@dataclass
class ComplexityReport:
    """公式复杂度评估结果"""

    latex: str
    action: str
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    free_vars: list[str] = field(default_factory=list)
    has_integral: bool = False
    has_sum_product: bool = False
    has_limit: bool = False
    has_matrix: bool = False
    has_piecewise: bool = False
    has_special_func: bool = False
    has_high_power: bool = False
    has_infinity: bool = False
    max_depth: int = 0
    length: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _latex_depth(latex: str) -> int:
    """粗略估计 LaTeX 嵌套深度

    计：'^{...}' / '_{...}' / '\\frac{...}{...}' / '\\sqrt{...}'
    """
    # 简化算法：统计 ^ / _ 后跟 { 的次数 + sqrt frac
    cur = 0
    # 用栈追踪花括号深度
    stack = []
    for i, ch in enumerate(latex):
        if ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                stack.pop()
            cur = max(cur, len(stack))
    # 含 sqrt/frac 加权
    if "\\sqrt" in latex:
        cur += 1
    if "\\frac" in latex:
        cur += 1
    if "\\dfrac" in latex:
        cur += 1
    return cur


def _free_vars(latex: str) -> set[str]:
    """粗略提取自由变量（单字母 a-z，跳过已知命令）"""
    # 排除常见 LaTeX 命令后
    candidates = re.findall(r"(?<![\\a-zA-Z])([a-zA-Z])(?![a-zA-Z])", latex)
    # 排除纯常量 e/i（也可能是变量，但概率低）
    # 数学常量
    BLACKLIST = {"e", "i", "d"}  # d 多来自 dx/dy
    return {c for c in candidates if c not in BLACKLIST}


def _has_high_power(latex: str) -> bool:
    """检测高阶幂：x^10+ 或 ^{x^...}"""
    # 显式高指数（>= 10，避免误判 x^2/x^3）
    if re.search(r"\^\{?\s*1\d\b|\^\s*\d{2,}", latex):
        return True
    # 嵌套指数（x^{x^...} 形式）
    return bool(re.search(r"\^\{[^}]*\^\{", latex))


def assess_complexity(latex: str, action: str = "simplify") -> ComplexityReport:
    """评估 LaTeX 公式复杂度，返回 ComplexityReport

    Args:
        latex: 公式 LaTeX 串
        action: 计划执行的动作（simplify/solve/integrate/...）

    Returns:
        ComplexityReport，含 score + 各分项
    """
    signals: dict[str, float] = {}
    length = len(latex)
    depth = _latex_depth(latex)
    free = _free_vars(latex)
    has_integral = bool(re.search(r"\\int(?![a-zA-Z])", latex))
    has_sum_product = bool(re.search(r"\\(sum|prod)(?![a-zA-Z])", latex))
    has_limit = bool(re.search(r"\\lim(?![a-zA-Z])", latex))
    has_matrix = bool(re.search(r"\\(matrix|bm|pmatrix|vmatrix)(?![a-zA-Z])", latex))
    has_piecewise = "\\begin{cases}" in latex or "\\text{if}" in latex
    has_special_func = bool(
        re.search(
            r"\\(Gamma|Zeta|erf|Erf|Si|Ci|LambertW)\b",
            latex,
        )
    )
    has_high_power = _has_high_power(latex)
    has_infinity = bool(re.search(r"\\(infty|inf)\b", latex))

    # 计分
    signals["length"] = length * WEIGHT_LENGTH
    signals["depth"] = depth * WEIGHT_DEPTH
    signals["free_vars"] = len(free) * WEIGHT_FREE_VAR
    signals["integral"] = WEIGHT_INTEGRAL if has_integral else 0
    signals["sum_product"] = WEIGHT_SUM_PROD if has_sum_product else 0
    signals["limit"] = WEIGHT_LIMIT if has_limit else 0
    signals["matrix"] = WEIGHT_MATRIX if has_matrix else 0
    signals["piecewise"] = WEIGHT_PIECEWISE if has_piecewise else 0
    signals["special_func"] = WEIGHT_SPECIAL_FUNC if has_special_func else 0
    signals["high_power"] = WEIGHT_HIGH_POWER if has_high_power else 0
    signals["infinity"] = WEIGHT_INFINITY if has_infinity else 0

    # action 调整：integrate/solve 更重
    action_bonus = {
        "integrate": 12,
        "solve": 6,
        "limit": 10,
        "simplify": 0,
        "factor": 2,
        "expand": 3,
        "diff": 0,
        "det": 5,
    }.get(action, 0)
    signals["action_bonus"] = float(action_bonus)

    # 复合信号加成：多个重型符号叠加 → 加分
    heavy_count = sum(1 for k in HEAVY_SIGNALS if signals.get(k, 0) > 0)
    if heavy_count >= 2:
        signals["multi_heavy_bonus"] = float(heavy_count * COMPLEX_MULTI_BONUS)

    score = sum(signals.values())

    return ComplexityReport(
        latex=latex,
        action=action,
        score=score,
        signals=signals,
        free_vars=sorted(free),
        has_integral=has_integral,
        has_sum_product=has_sum_product,
        has_limit=has_limit,
        has_matrix=has_matrix,
        has_piecewise=has_piecewise,
        has_special_func=has_special_func,
        has_high_power=has_high_power,
        has_infinity=has_infinity,
        max_depth=depth,
        length=length,
    )


def select_strategy(
    latex: str,
    action: str = "simplify",
    timeout_history: dict | None = None,
) -> dict:
    """根据复杂度选策略，返回详细推理 + fallback 链

    Args:
        latex: 公式 LaTeX
        action: sympy 动作
        timeout_history: 之前调用此公式的耗时历史（key=action, value=avg_seconds）

    Returns:
        {
            "strategy": "sympy" | "ask" | "numeric" | "llm",
            "score": float,
            "reasoning": str,
            "fallback_chain": ["sympy", "ask", "numeric", "llm"],
            "estimated_timeout_s": float,
            "complexity": ComplexityReport.to_dict(),
        }
    """
    report = assess_complexity(latex, action)
    score = report.score

    # 考虑历史超时：之前平均 > 3s 直接走 numeric
    if timeout_history and timeout_history.get(action, 0) > 3.0:
        score = max(score, THRESHOLD_NUMERIC + 1)

    if score < THRESHOLD_ASK:
        strategy = "sympy"
        reasoning = f"score={score:.1f} < {THRESHOLD_ASK}，公式简单，直接 sympy 计算"
        est_timeout = 0.5
    elif score < THRESHOLD_NUMERIC:
        strategy = "ask"
        reasoning = f"score={score:.1f} ∈ [{THRESHOLD_ASK}, {THRESHOLD_NUMERIC})，需要条件推理，走 ask + assumptions"
        est_timeout = 2.0
    elif score < THRESHOLD_LLM:
        strategy = "numeric"
        reasoning = f"score={score:.1f} ∈ [{THRESHOLD_NUMERIC}, {THRESHOLD_LLM})，符号路径风险高，降级到数值解"
        est_timeout = 5.0
    else:
        strategy = "llm"
        reasoning = (
            f"score={score:.1f} ≥ {THRESHOLD_LLM}，极端复杂，走 LLM 兜底（Ollama deepseek-r1:14b）"
        )
        est_timeout = 60.0

    return {
        "strategy": strategy,
        "score": score,
        "reasoning": reasoning,
        "fallback_chain": ["sympy", "ask", "numeric", "llm"],
        "estimated_timeout_s": est_timeout,
        "complexity": report.to_dict(),
    }


def _run_async(coro):
    """从 sync 上下文跑 async 协程（避免嵌套 event loop 报错）"""
    import asyncio

    try:
        asyncio.get_running_loop()
        # 当前已在 event loop 中（如 MCP stdio handler），不能再 asyncio.run
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def execute_with_fallback(
    latex: str,
    action: str = "simplify",
    var: str = "x",
    timeout_history: dict | None = None,
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "deepseek-r1:14b",
    force_strategy: str | None = None,  # M3 W5: 灰度路由，强制走某层
    canary_ratio: float = 0.0,  # M3 W5: canary 流量比例 (0.0~1.0)
    canary_target: str = "llm",  # M3 W5: canary 走的目标策略
    canary_key: str | None = None,  # M3 W5: 自定义分组键（默认 hash(latex+action)）
) -> dict:
    """同步版 fallback 链执行（内嵌 asyncio.run 跑 async 内部函数）"""
    """按 fallback 链执行：sympy → ask → numeric → llm

    每层失败自动下一层，最终返回首个成功结果 + 实际策略 + 各层尝试记录。

    Args:
        ollama_url/ollama_model: LLM 兜底层配置（vLLM 启动后可替换）
        force_strategy: M3 W5 灰度路由参数。若指定且为 ['sympy','ask','numeric','llm'] 之一，
            则跳过评分 + 自动降级，强制只走该层。None 则按常规逻辑。
        canary_ratio: M3 W5 灰度比例。0.0~1.0，hash(canary_key) % 100 < ratio*100 走 canary。
        canary_target: canary 流量走的目标策略（默认 "llm" 用于 LLM 兜底 A/B 测试）。
        canary_key: 自定义分组键，用于稳定分桶（默认 hash(latex+action+var)）。

    Returns:
        {
            "ok": bool,
            "result": <首个成功的结果>,
            "strategy_used": str,
            "attempts": [...],
            "canary": {"group": "control"|"canary", "key_hash": "...", "ratio": 0.1},
        }
    """
    chain = ["sympy", "ask", "numeric", "llm"]

    # M3 W5: canary 分组判定（hash 稳定分桶）
    if canary_key is None:
        canary_key = f"{latex}|{action}|{var}"
    key_hash = hashlib.md5(canary_key.encode("utf-8")).hexdigest()[:8]
    bucket = int(key_hash, 16) % 100  # 0~99
    is_canary = bucket < int(canary_ratio * 100) if canary_ratio > 0 else False
    canary_info = {
        "group": "canary" if is_canary else "control",
        "key_hash": key_hash,
        "ratio": canary_ratio,
        "canary_target": canary_target if is_canary else None,
    }

    # M3 W5: canary 流量强制走 canary_target
    effective_force = canary_target if is_canary and canary_target in chain else force_strategy

    if effective_force and effective_force in chain:
        # M3 W5: 灰度强制模式，跳过评分 + 降级
        primary = effective_force
        active_chain = [effective_force]
        reason_suffix = []
        if force_strategy:
            reason_suffix.append(f"force_strategy={force_strategy}")
        if is_canary:
            reason_suffix.append(f"canary→{canary_target} (ratio={canary_ratio})")
        plan = {
            "strategy": effective_force,
            "score": -1,
            "reasoning": "M3 W5 灰度模式 (" + " + ".join(reason_suffix) + ")",
            "fallback_chain": chain,
            "estimated_timeout_s": 5.0,
            "complexity": {},
        }
    else:
        plan = select_strategy(latex, action, timeout_history)
        primary = plan["strategy"]
        # 从 primary 开始尝试，但所有更后的策略也作为 fallback
        start_idx = chain.index(primary)
        active_chain = chain[start_idx:]

    attempts: list[dict] = []
    result: dict | None = None
    success_strategy: str | None = None

    for strat in active_chain:
        t0 = time.time()
        attempt: dict = {"strategy": strat}
        try:
            if strat == "sympy":
                from mcp_servers.math_sympy import _cached_compute

                out = _run_async(_cached_compute(latex, action, var))
                ok = bool(out.get("ok"))
                attempt["ok"] = ok
                if not ok:
                    attempt["error"] = out.get("error", "(no error detail)")[:120]
                if ok:
                    result = out
                    success_strategy = strat
            elif strat == "ask":
                # ask 路径用于证明 / 不等式（无 proof 请求时退化为 numeric）
                from mcp_servers.math_sympy import _solve_inequality

                if action in ("solve", "solve_inequality"):
                    out = _solve_inequality(
                        latex, var, relational=True, timeout_s=plan["estimated_timeout_s"]
                    )
                    attempt["ok"] = bool(out.get("ok"))
                    if attempt["ok"]:
                        result = out
                        success_strategy = strat
                else:
                    attempt["ok"] = False
                    attempt["error"] = "ask 仅用于 solve 路径"
            elif strat == "numeric":
                from mcp_servers.math_sympy import _numeric_fallback_compute

                out = _run_async(_numeric_fallback_compute(latex, action, var))
                ok = bool(out.get("ok"))
                attempt["ok"] = ok
                if ok:
                    result = out
                    success_strategy = strat
            elif strat == "llm":
                out = _llm_fallback_call(latex, action, var, ollama_url, ollama_model)
                ok = bool(out.get("ok"))
                attempt["ok"] = ok
                if ok:
                    result = out
                    success_strategy = strat
        except Exception as e:
            attempt["ok"] = False
            attempt["error"] = f"{type(e).__name__}: {e}"

        attempt["elapsed_s"] = round(time.time() - t0, 3)
        attempts.append(attempt)

        if result is not None:
            break

    if result is None:
        ret = {
            "ok": False,
            "error": "所有 fallback 策略均失败",
            "strategy_used": None,
            "attempts": attempts,
            "fallback_chain": active_chain,
            "canary": canary_info,
            "total_elapsed_s": round(sum(a.get("elapsed_s", 0) for a in attempts), 3),
        }
        _audit_fallback(
            tool="math_strategy_route_and_execute",
            latex=latex,
            action=action,
            var=var,
            group=canary_info["group"],
            key_hash=key_hash,
            canary_ratio=canary_ratio,
            canary_target=canary_target,
            primary=primary,
            chain=active_chain,
            attempts=attempts,
            success_strategy=None,
            total_elapsed_s=ret["total_elapsed_s"],
            ok=False,
        )
        return ret

    ret = {
        "ok": True,
        "result": result,
        "strategy_used": success_strategy,
        "attempts": attempts,
        "fallback_chain": active_chain,
        "plan": plan,
        "canary": canary_info,
        "total_elapsed_s": round(sum(a.get("elapsed_s", 0) for a in attempts), 3),
    }
    _audit_fallback(
        tool="math_strategy_route_and_execute",
        latex=latex,
        action=action,
        var=var,
        group=canary_info["group"],
        key_hash=key_hash,
        canary_ratio=canary_ratio,
        canary_target=canary_target,
        primary=primary,
        chain=active_chain,
        attempts=attempts,
        success_strategy=success_strategy,
        total_elapsed_s=ret["total_elapsed_s"],
        ok=True,
    )
    return ret


# ============================================================================
# M3 W5: audit log 落点（fallback-chain.jsonl + fallback-canary.jsonl）
# ============================================================================

_AUDIT_DIR = Path(os.environ.get("MATH_AUDIT_DIR", "/home/jiuben/tdx-data-feed/v5/audit"))
_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
_AUDIT_FALLBACK = _AUDIT_DIR / "fallback-chain.jsonl"
_AUDIT_CANARY = _AUDIT_DIR / "fallback-canary.jsonl"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _audit_fallback(
    tool: str,
    latex: str,
    action: str,
    var: str,
    group: str,
    key_hash: str,
    canary_ratio: float,
    canary_target: str,
    primary: str,
    chain: list,
    attempts: list,
    success_strategy: str | None,
    total_elapsed_s: float,
    ok: bool,
):
    """M3 W5: 落 audit log 到 fallback-chain.jsonl（所有调用）+ fallback-canary.jsonl（仅 canary 流量）

    fallback-chain.jsonl 字段:
      ts, tool, latex, action, var, key_hash, group, canary_ratio, canary_target,
      primary, chain, attempts, success_strategy, total_elapsed_s, ok
    """
    entry = {
        "ts": _utc_now(),
        "tool": tool,
        "latex": latex[:500],  # 截断避免巨大输入
        "action": action,
        "var": var,
        "key_hash": key_hash,
        "group": group,
        "canary_ratio": canary_ratio,
        "canary_target": canary_target if group == "canary" else None,
        "primary": primary,
        "chain": chain,
        "attempts": attempts,
        "success_strategy": success_strategy,
        "total_elapsed_s": total_elapsed_s,
        "ok": ok,
    }
    # 落 fallback-chain.jsonl（所有流量）
    try:
        with _AUDIT_FALLBACK.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit 落盘失败不应影响主流程

    # 仅 canary 流量落 fallback-canary.jsonl（用于 A/B 比较）
    if group == "canary":
        try:
            with _AUDIT_CANARY.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _llm_fallback_call(
    latex: str,
    action: str,
    var: str,
    ollama_url: str,
    ollama_model: str,
    timeout_s: float = 30.0,
) -> dict:
    """LLM 兜底层调用（Ollama deepseek-r1:14b via HTTP API）

    D2.5 stub：构造 prompt + 调 Ollama /api/generate，超时回 None。
    """
    import json
    import urllib.error
    import urllib.request

    # 构造简洁 prompt（避免 R1 啰嗦推理，强制最终答案）
    prompt = (
        f"You are a math assistant. Perform '{action}' on the LaTeX expression.\n\n"
        f"LaTeX: {latex}\n"
        f"Variable: {var}\n\n"
        'Reply STRICTLY in JSON: {"ok": true|false, "result": "<LaTeX result>", '
        '"explanation": "<one-line rationale>"}. '
        "No prose, no <think> tags."
    )

    body = json.dumps(
        {
            "model": ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 512,
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("response", "").strip()
            # 尝试解析 JSON 响应（含 R1 常用 markdown ```json ... ``` 包裹）
            parsed = None
            for pattern in (r"```(?:json)?\s*(\{.*?\})\s*```", r"(\{[^{}]*\"ok\"[^{}]*\})"):
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(1))
                        break
                    except json.JSONDecodeError:
                        continue
            if parsed is None:
                # 整段就是 JSON
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(text)
            if parsed and "ok" in parsed:
                return {
                    "ok": True,
                    "result": parsed.get("result"),
                    "explanation": parsed.get("explanation", ""),
                    "raw_text": text[:300],
                    "model": ollama_model,
                }
            # 解析失败：返回原文（让上游理解）
            return {
                "ok": True,
                "result": text[:500],
                "explanation": "LLM 返回非 JSON，已截断",
                "raw_text": text[:300],
                "model": ollama_model,
            }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "ok": False,
            "error": f"Ollama call failed: {e}",
            "model": ollama_model,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"unexpected: {type(e).__name__}: {e}",
            "model": ollama_model,
        }
