"""
math_sympy_http.py · v5-VE1-M2 D6.6 持久化 SymPy server

FastAPI 替代 stdio MCP 通信：
  - HTTP/JSON 端点暴露 9 个 Tool
  - 进程常驻，避免 stdio 每次启动 ~500ms 开销
  - 单次调用 < 50ms（消除 sympy 进程启动成本）

端点（按 Tool 名）：
  POST /tools/are_equiv                 {"latex_a": str, "latex_b": str}
  POST /tools/sympy_compute             {"latex": str, "action": str, "var": str?}
  POST /tools/math_error_report         {"latex": str, "error_message": str?}
  POST /tools/math_fix_typo             {"latex": str}
  POST /tools/math_validate_symbols     {"latex": str, "declared": list?}
  POST /tools/math_cache_stats          {}
  POST /tools/math_numeric_solve        {"latex": str, "action": str, "var": str?}
  POST /tools/math_ast_dump             {"latex": str}
  POST /tools/math_batch_equiv          {"pairs": [[str, str], ...], "max_workers": int?}

健康检查：
  GET  /health                          → {"status": "ok", "tool_count": int}
  GET  /                                → 服务信息 + 端点清单

启动：
  python -m mcp_servers.math_sympy_http [--host 127.0.0.1] [--port 8002]
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

# 让 standalone 跑时能找到同目录模块 + v5 上层（for `mcp_servers.xxx`）
_PKG_DIR = Path(__file__).parent
_V5_DIR = _PKG_DIR.parent
for p in (str(_PKG_DIR), str(_V5_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, HTTPException, Request  # noqa: E402

# 复用 stdio MCP server 的所有 Tool 实现（不重复写）
from mcp_servers.math_sympy import (  # noqa: E402
    _cached_compute,
    _cached_equiv,
    _numeric_fallback_compute,
    _prove_equiv_with_precondition,
    _pylatexenc_parse,
    _simplify_piecewise,
    _solve_inequality,
    math_cache,
    math_errors,
)

from mcp_math.derivation import build_chain, replay_dag  # noqa: E402
from mcp_math.derivation_cache import (  # noqa: E402
    build_chain_with_cache,
    cache_stats as derivation_cache_stats,
)
from mcp_math.derivation_reverse import reverse_build  # noqa: E402
from mcp_math.ontology import (  # noqa: E402
    bilingual_translate,
    link_concepts,
    load_ontology,
)
from mcp_math.parallel import batch_are_equiv  # noqa: E402
from mcp_math.step_extractor import (  # noqa: E402
    extract_steps_from_markdown,
    verify_steps,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("math-sympy-http")

app = FastAPI(
    title="v5-VE1 Math SymPy HTTP API",
    description="持久化 SymPy server（D6.6），HTTP/JSON 替代 stdio MCP",
    version="0.2.0",
)

# 启动时间（用于 /health）
START_TIME = time.time()


# ============================================================================
# 健康 / 信息
# ============================================================================

# 注册所有端点（先定义，便于 root 引用）
TOOL_NAMES = [
    "are_equiv",
    "sympy_compute",
    "math_error_report",
    "math_fix_typo",
    "math_validate_symbols",
    "math_cache_stats",
    "math_numeric_solve",
    "math_ast_dump",
    "math_batch_equiv",
    "math_concept_link",
    "math_bilingual_translate",
    "math_ontology_stats",
    # M2 W9 D2.1：条件等价证明
    "prove_equiv_with_precondition",
    # M2 W11 D3.1：推导链 DAG
    "math_derivation_chain",
    "math_derivation_replay",
    # M2 W10 D2.4 / W11 D2.5：策略路由 + fallback 链执行（含 LLM 兜底）
    "math_strategy_route",
    "math_strategy_route_and_execute",
    # M2 W9 D2.2/D2.3：不等式求解 + 分段函数
    "solve_inequality",
    "simplify_piecewise",
    # M2 W11 D3.2/D3.3/D3.5：步骤抽取 + sidecar 缓存 + 反向推导
    "math_step_extract",
    "math_derivation_cache_stats",
    "math_derivation_build_cached",
    "math_derivation_reverse",
]


# ============================================================================
# M3 W3：监控指标（in-memory counter + Prometheus /metrics 端点）
# ============================================================================

_metrics_lock = threading.Lock()
_tool_calls_total: dict = {}  # tool_name -> {status: count}
_tool_latencies: dict = {}  # tool_name -> [latency_seconds, ...]
_tool_errors: dict = {}  # tool_name -> {error_class: count}
_fallback_strategy_used: dict = {}  # strategy -> count
_fallback_attempts_total: dict = {}  # strategy -> total attempt count（含失败）


def _record_call(name: str, status: str, elapsed: float, result: dict | None = None):
    """埋点：每次 tool 调用记录指标（线程安全）"""
    with _metrics_lock:
        # 用 if not in + 显式赋值（避免 setdefault + 后置 .get 的 KeyError）
        if name not in _tool_calls_total:
            _tool_calls_total[name] = {"ok": 0, "fail": 0, "exception": 0}
        _tool_calls_total[name][status] = _tool_calls_total[name].get(status, 0) + 1
        # 限制 latency 列表长度（防 OOM）
        if name not in _tool_latencies:
            _tool_latencies[name] = []
        _tool_latencies[name].append(elapsed)
        if len(_tool_latencies[name]) > 5000:
            del _tool_latencies[name][:1000]
        # 记录 fallback 策略分布
        if isinstance(result, dict):
            strat = result.get("strategy_used")
            if strat:
                _fallback_strategy_used[strat] = _fallback_strategy_used.get(strat, 0) + 1
            # 记录每层尝试（sympy/ask/numeric/llm）
            for att in result.get("attempts", []):
                s = att.get("strategy")
                if s:
                    _fallback_attempts_total[s] = _fallback_attempts_total.get(s, 0) + 1


@app.get("/")
async def root():
    return {
        "service": "math-sympy-http",
        "version": "0.2.0",
        "uptime_s": round(time.time() - START_TIME, 2),
        "tools": TOOL_NAMES,
    }


@app.get("/health")
async def health():
    """健康检查（M3 W3 增强）：含 cache + fallback + error_rate"""
    with _metrics_lock:
        total_calls = sum(sum(v.values()) for v in _tool_calls_total.values())
        total_ok = sum(v.get("ok", 0) for v in _tool_calls_total.values())
        # math_cache（注意字段名是 total_entries 不是 total）
        try:
            m_cache = math_cache.cache_stats()
            m_total = m_cache.get("total_entries", 0)
            m_hits = m_cache.get("total_hits", 0)
        except Exception:
            m_total = m_hits = 0
        # derivation_cache
        try:
            d_cache = derivation_cache_stats()
            d_total = d_cache.get("total", 0)
            d_hits = d_cache.get("total_hits", 0)
        except Exception:
            d_total = d_hits = 0
        return {
            "status": "ok",
            "tool_count": len(TOOL_NAMES),
            "uptime_s": round(time.time() - START_TIME, 2),
            "total_calls": total_calls,
            "total_ok": total_ok,
            "error_rate": round(1 - (total_ok / total_calls if total_calls else 0), 3),
            "cache": {
                "math_cache": {
                    "total_entries": m_total,
                    "total_hits": m_hits,
                    "hit_ratio": round(m_hits / max(m_total, 1), 3),
                },
                "derivation_cache": {
                    "total": d_total,
                    "total_hits": d_hits,
                    "hit_ratio": round(d_hits / max(d_total, 1), 3),
                },
            },
            "fallback": {
                "primary_strategy_distribution": dict(_fallback_strategy_used),
                "total_attempts_by_strategy": dict(_fallback_attempts_total),
            },
            "errors": {tool: dict(errs) for tool, errs in _tool_errors.items()},
        }


@app.get("/metrics")
async def metrics():
    """Prometheus 标准指标端点（M3 W3）

    输出格式：text/plain; version=0.0.4
    metrics:
      - tool_calls_total{tool,status} (counter)
      - tool_latency_seconds_avg{tool} (gauge)
      - cache_hit_ratio{cache} (gauge)
      - fallback_strategy_used{strategy} (counter)
      - fallback_attempts_total{strategy} (counter)
    """
    lines = []
    with _metrics_lock:
        # tool_calls_total
        lines.append("# HELP tool_calls_total Total tool calls since server start")
        lines.append("# TYPE tool_calls_total counter")
        for tool, stats in _tool_calls_total.items():
            for status, count in stats.items():
                lines.append(f'tool_calls_total{{tool="{tool}",status="{status}"}} {count}')

        # 平均延迟（gauge 简化版）
        lines.append("# HELP tool_latency_seconds_avg Average tool latency in seconds")
        lines.append("# TYPE tool_latency_seconds_avg gauge")
        for tool, lats in _tool_latencies.items():
            if lats:
                avg = sum(lats) / len(lats)
                lines.append(f'tool_latency_seconds_avg{{tool="{tool}"}} {round(avg, 4)}')
                # p95 估算
                sorted_lats = sorted(lats)
                p95_idx = max(0, int(len(sorted_lats) * 0.95) - 1)
                p95 = sorted_lats[p95_idx]
                lines.append(f'tool_latency_seconds_p95{{tool="{tool}"}} {round(p95, 4)}')

        # fallback_strategy_used
        lines.append("# HELP fallback_strategy_used Calls by primary fallback strategy used")
        lines.append("# TYPE fallback_strategy_used counter")
        for strat, count in _fallback_strategy_used.items():
            lines.append(f'fallback_strategy_used{{strategy="{strat}"}} {count}')

        # fallback_attempts_total（含失败）
        lines.append(
            "# HELP fallback_attempts_total Total fallback layer attempts (including failures)"
        )
        lines.append("# TYPE fallback_attempts_total counter")
        for strat, count in _fallback_attempts_total.items():
            lines.append(f'fallback_attempts_total{{strategy="{strat}"}} {count}')

    # cache_hit_ratio（从 math_cache + derivation_cache 读取）
    try:
        m_cache = math_cache.cache_stats()
        m_total = max(m_cache.get("total_entries", 0), 1)
        m_hits = m_cache.get("total_hits", 0)
        lines.append("# HELP cache_hit_ratio Cache hit ratio (0-1)")
        lines.append("# TYPE cache_hit_ratio gauge")
        lines.append(f'cache_hit_ratio{{cache="math"}} {round(m_hits / m_total, 4)}')
    except Exception:
        pass
    try:
        d_cache = derivation_cache_stats()
        d_total = max(d_cache.get("total", 0), 1)
        d_hits = d_cache.get("total_hits", 0)
        if d_total > 1:  # 避免全部为 0 时显示
            lines.append(f'cache_hit_ratio{{cache="derivation"}} {round(d_hits / d_total, 4)}')
    except Exception:
        pass

    # M3 W5: Canary 灰度 metrics（从 canary_state.json + audit 读）
    try:
        import json as _json
        from pathlib import Path as _Path

        state_file = _Path("/home/jiuben/tdx-data-feed/v5/audit/canary_state.json")
        if state_file.exists():
            state = _json.loads(state_file.read_text())
            lines.append("# HELP canary_ratio Current canary traffic ratio (0-1)")
            lines.append("# TYPE canary_ratio gauge")
            lines.append(f"canary_ratio {state.get('ratio', 0.0):.4f}")
            lines.append("# HELP canary_decisions_total Total canary ratio decisions")
            lines.append("# TYPE canary_decisions_total counter")
            lines.append(f"canary_decisions_total {len(state.get('history', []))}")
        today_log = _Path("/home/jiuben/tdx-data-feed/v5/audit/fallback-chain.jsonl")
        if today_log.exists():
            counts = {
                "control": 0,
                "canary": 0,
                "control_ok": 0,
                "canary_ok": 0,
                "control_total_s": 0.0,
                "canary_total_s": 0.0,
            }
            for line in today_log.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = _json.loads(line)
                    g = e.get("group", "control")
                    counts[g] = counts.get(g, 0) + 1
                    if e.get("ok"):
                        counts[f"{g}_ok"] = counts.get(f"{g}_ok", 0) + 1
                    counts[f"{g}_total_s"] = counts.get(f"{g}_total_s", 0.0) + float(
                        e.get("total_elapsed_s", 0)
                    )
                except Exception:
                    pass
            lines.append("# HELP canary_calls_total Total math-sympy-http calls by canary group")
            lines.append("# TYPE canary_calls_total counter")
            lines.append(f'canary_calls_total{{group="control"}} {counts.get("control", 0)}')
            lines.append(f'canary_calls_total{{group="canary"}} {counts.get("canary", 0)}')
            lines.append("# HELP canary_calls_success_total Successful calls by group")
            lines.append("# TYPE canary_calls_success_total counter")
            lines.append(
                f'canary_calls_success_total{{group="control"}} {counts.get("control_ok", 0)}'
            )
            lines.append(
                f'canary_calls_success_total{{group="canary"}} {counts.get("canary_ok", 0)}'
            )
            lines.append("# HELP canary_success_rate Success rate by group (0-1)")
            lines.append("# TYPE canary_success_rate gauge")
            for g in ("control", "canary"):
                total = counts.get(g, 0)
                ok = counts.get(f"{g}_ok", 0)
                rate = (ok / total) if total > 0 else 1.0
                lines.append(f'canary_success_rate{{group="{g}"}} {rate:.4f}')
            lines.append("# HELP canary_avg_duration_seconds Average call duration by group")
            lines.append("# TYPE canary_avg_duration_seconds gauge")
            for g in ("control", "canary"):
                total = counts.get(g, 0)
                total_s = counts.get(f"{g}_total_s", 0.0)
                avg = (total_s / total) if total > 0 else 0.0
                lines.append(f'canary_avg_duration_seconds{{group="{g}"}} {avg:.4f}')
    except Exception:
        pass

    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )


# ============================================================================
# Tool 端点（每个 = 1 个 async handler）
# ============================================================================


async def _run_tool(name: str, arguments: dict) -> dict:
    """统一 dispatch + 异常包装 + 计时。"""
    t0 = time.time()
    try:
        if name == "are_equiv":
            res = await _cached_equiv(arguments["latex_a"], arguments["latex_b"])
        elif name == "sympy_compute":
            # M3 W5: 强制走兜底层（force_strategy=llm）或 canary 灰度
            force_strategy = arguments.get("force_strategy")
            canary_ratio = float(arguments.get("canary_ratio", 0.0))
            # M3 W5: 若调用方没指定 canary_ratio，自动从 canary_state.json 读取
            if "canary_ratio" not in arguments:
                try:
                    state_file = Path("/home/jiuben/tdx-data-feed/v5/audit/canary_state.json")
                    if state_file.exists():
                        import json as _json

                        state = _json.loads(state_file.read_text())
                        canary_ratio = float(state.get("ratio", 0.0))
                except Exception:
                    pass
            if force_strategy or canary_ratio > 0:
                # 走 fallback chain + LLM 兜底（execute_with_fallback 是 sync）
                from mcp_math.strategy_selector import execute_with_fallback

                res = execute_with_fallback(
                    arguments["latex"],
                    action=arguments.get("action", "simplify"),
                    var=arguments.get("var", "x"),
                    ollama_url=arguments.get("ollama_url", "http://127.0.0.1:11434"),
                    ollama_model=arguments.get("ollama_model", "qwen3:14b"),
                    force_strategy=force_strategy,
                    canary_ratio=canary_ratio,
                    canary_target=arguments.get("canary_target", "llm"),
                    canary_key=arguments.get("canary_key"),
                )
                res["_tool"] = "sympy_compute"
            else:
                # 默认 sympy 直跑（保留 cache 加速）
                res = await _cached_compute(
                    arguments["latex"],
                    action=arguments.get("action", "simplify"),
                    var=arguments.get("var", "x"),
                )
        elif name == "math_error_report":
            latex = arguments["latex"]
            try:
                raise Exception(arguments.get("error_message", "unknown"))
            except Exception as exc:
                res = math_errors.classify_sympy_exception(exc, latex).to_dict()
        elif name == "math_fix_typo":
            latex = arguments["latex"]
            fixed, applied = math_errors.fix_typo(latex)
            res = {
                "fixed": fixed,
                "applied": applied,
                "fuzzy_suggestions": math_errors.fuzzy_fix_command(latex),
            }
        elif name == "math_validate_symbols":
            declared = arguments.get("declared")
            res = math_errors.validate_symbols(
                arguments["latex"], set(declared) if declared else None
            )
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
            raw_pairs = arguments.get("pairs", [])
            pairs = [(p[0], p[1]) for p in raw_pairs if len(p) >= 2]
            t_compute = time.time()
            results = batch_are_equiv(pairs, max_workers=arguments.get("max_workers"))
            res = {
                "count": len(pairs),
                "compute_elapsed_s": round(time.time() - t_compute, 3),
                "results": results,
            }
        elif name == "math_concept_link":
            # D4: 给定一段文字/LaTeX，找最相关的 ontology 概念
            query = arguments["query"]
            top_k = arguments.get("top_k", 5)
            use_embed = arguments.get("use_embed", True)
            linked = link_concepts(query, top_k=top_k, use_embed=use_embed)
            res = {
                "query": query,
                "linked": [
                    {
                        "concept_id": l.concept_id,
                        "name": l.name,
                        "name_en": l.name_en,
                        "category": l.category,
                        "score": l.score,
                        "matched_keywords": l.matched_keywords,
                        "vector_sim": l.vector_sim,
                    }
                    for l in linked
                ],
            }
        elif name == "math_bilingual_translate":
            # D4.2: 双语翻译
            text = arguments["text"]
            src = arguments.get("src", "auto")
            res = {"translations": bilingual_translate(text, src)}
        elif name == "math_ontology_stats":
            # D4.1: ontology 统计
            concepts = load_ontology()
            categories = {}
            for c in concepts:
                categories[c.category] = categories.get(c.category, 0) + 1
            res = {
                "total_concepts": len(concepts),
                "by_category": categories,
            }
        elif name == "prove_equiv_with_precondition":
            # M2 W9 D2.1: 条件等价证明（HTTP 通道）
            res = _prove_equiv_with_precondition(
                latex_a=arguments["latex_a"],
                latex_b=arguments["latex_b"],
                preconditions=arguments.get("preconditions") or [],
                timeout_s=arguments.get("timeout_s", 2.0),
            )
        elif name == "math_derivation_chain":
            # M2 W11 D3.1: BFS 推导链 DAG 构建
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
            # M2 W11 D3.1: 从序列化 DAG 重放验证
            res = replay_dag(
                dag_dict=arguments["dag_json"],
                var=arguments.get("var", "x"),
            )
        elif name == "math_strategy_route":
            # M2 W10 D2.4: 策略路由（不执行，只评估）
            from mcp_math.strategy_selector import select_strategy

            res = select_strategy(
                latex=arguments["latex"],
                action=arguments.get("action", "simplify"),
                timeout_history=arguments.get("timeout_history"),
            )
        elif name == "math_strategy_route_and_execute":
            # M2 W10 D2.4 + W11 D2.5 + M3 W5 灰度 + canary
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
        elif name == "solve_inequality":
            # M2 W9 D2.2: 不等式求解
            res = _solve_inequality(
                latex=arguments["latex"],
                var=arguments.get("var", "x"),
                relational=arguments.get("relational", True),
                timeout_s=arguments.get("timeout_s", 2.0),
            )
        elif name == "simplify_piecewise":
            # M2 W9 D2.3: 分段函数简化 / 评估
            res = _simplify_piecewise(
                cases=arguments["cases"],
                var=arguments.get("var", "x"),
                action=arguments.get("action", "simplify"),
            )
        elif name == "math_step_extract":
            # M2 W11 D3.2: 从 markdown 抽取推导步骤
            ext = extract_steps_from_markdown(
                markdown_text=arguments["markdown"],
                source_path=arguments.get("source_path", "<inline>"),
            )
            res = ext.to_dict()
            if arguments.get("verify"):
                res["verification"] = verify_steps(
                    ext.steps,
                    var=arguments.get("var", "x"),
                )
        elif name == "math_derivation_cache_stats":
            # M2 W11 D3.3: sidecar 缓存统计
            res = derivation_cache_stats()
        elif name == "math_derivation_build_cached":
            # M2 W11 D3.3: 带 sidecar 缓存的推导链构建
            res = build_chain_with_cache(
                start_latex=arguments["start_latex"],
                target_latex=arguments["target_latex"],
                var=arguments.get("var", "x"),
                max_depth=arguments.get("max_depth", 4),
                note_id=arguments.get("note_id"),
            )
        elif name == "math_derivation_reverse":
            # M2 W12 D3.5: 反向推导
            res = reverse_build(
                target_latex=arguments["target_latex"],
                var=arguments.get("var", "x"),
                max_depth=arguments.get("max_depth", 3),
                max_candidates=arguments.get("max_candidates", 5),
                timeout_s=arguments.get("timeout_s", 5.0),
            )
        else:
            raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"tool {name} failed")
        elapsed = round(time.time() - t0, 4)
        err_res = {"ok": False, "error": str(e), "_tool": name, "_elapsed_s": elapsed}
        _record_call(name, "exception", elapsed, err_res)
        raise HTTPException(status_code=500, detail=str(e)) from e

    elapsed = round(time.time() - t0, 4)
    if isinstance(res, dict):
        res.setdefault("_elapsed_s", elapsed)
        res.setdefault("_tool", name)
    # M3 W3 埋点：成功路径（ok / fail 二元）
    status = "ok" if (isinstance(res, dict) and res.get("ok", True)) else "fail"
    _record_call(name, status, elapsed, res if isinstance(res, dict) else None)
    return res


# 注册所有 12 个端点
@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, request: Request):
    arguments = await request.json()
    return await _run_tool(tool_name, arguments)


# ============================================================================
# OpenAPI 兼容的 batch 端点（一次调用多个 Tool）
# ============================================================================


@app.post("/batch")
async def batch_call(request: Request):
    """一次发多个 Tool 调用。请求: {"calls": [{"tool": str, "arguments": dict}]}"""
    body = await request.json()
    calls = body.get("calls", [])
    if not calls:
        raise HTTPException(status_code=400, detail="calls 不能为空")
    results = []
    for c in calls:
        name = c.get("tool")
        args = c.get("arguments", {})
        if name not in TOOL_NAMES:
            results.append({"_tool": name, "error": f"unknown tool: {name}"})
            continue
        try:
            r = await _run_tool(name, args)
            results.append(r)
        except HTTPException as e:
            results.append({"_tool": name, "error": str(e.detail)})
    return {"count": len(results), "results": results}


# ============================================================================
# 启动入口
# ============================================================================


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    import uvicorn

    log.info(f"math-sympy-http starting on http://{args.host}:{args.port}")
    log.info("  9 个 Tool 端点: POST /tools/{name}")
    log.info("  健康检查: GET /health")
    uvicorn.run(app, host=args.host, port=args.port, workers=args.workers, log_level="info")


if __name__ == "__main__":
    main()
