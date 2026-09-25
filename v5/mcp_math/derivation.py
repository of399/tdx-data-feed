"""D3.1 推导链 DAG 建模（M2 W11）

依据：roadmap §13.4 W11 + v5-implementation-plan.md 任务卡 D3.1

把数学推导建模为有向无环图（DAG）：
  - 节点 = 表达式（sympy expr + LaTeX）
  - 边 = 应用的变换规则（rule name）
  - 路径 = 起点 → 终点的推导步骤链

能力：
  - build_chain: BFS 搜索从 start 到 target 的最短路径 DAG
  - dag_to_mermaid: Mermaid 渲染（用于前端可视化）
  - dag_to_json: 可序列化（用于 sidecar / D3.3 中间缓存）
  - replay_dag: 从 JSON 重放验证（确定性 + 性能）
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field

from sympy import (
    Symbol,
    apart,
    cancel,
    collect,
    diff,
    expand,
    expand_trig,
    factor,
    latex as sympy_latex,
    powsimp,
    radsimp,
    simplify,
    sympify,
    trigsimp,
)

# ============ 数据结构 ============


@dataclass
class DerivationNode:
    """DAG 节点 = 一个表达式状态"""

    id: int
    expr_latex: str  # LaTeX 渲染（人类可读）
    expr_str: str  # str(sympy_expr)（机器 hash 用）
    depth: int = 0
    parent_id: int | None = None
    rule_applied: str | None = None  # 应用到此节点的规则
    is_start: bool = False
    is_target: bool = False

    @property
    def expr_hash(self) -> str:
        return hashlib.md5(self.expr_str.encode("utf-8")).hexdigest()[:12]


@dataclass
class DerivationDAG:
    """推导链 DAG（含元信息 + 可序列化方法）"""

    start_latex: str
    target_latex: str
    nodes: list[DerivationNode] = field(default_factory=list)
    edges: list[tuple[int, int, str]] = field(default_factory=list)  # (from, to, rule)
    success: bool = False
    target_node_id: int | None = None
    steps: int = 0
    explored: int = 0
    elapsed_s: float = 0.0
    rules_used: list[str] = field(default_factory=list)
    reason: str = (
        ""  # "trivially_equal" | "found_path" | "max_depth_exceeded" | "timeout" | "no_rule_match"
    )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nodes"] = [asdict(n) for n in self.nodes]
        d["edges"] = list(self.edges)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_mermaid(self) -> str:
        return dag_to_mermaid(self)


# ============ 内置规则集 ============


def _identity(expr, var):
    return expr


BUILTIN_RULES: dict[str, callable] = {
    "expand": lambda e, v: expand(e),
    "factor": lambda e, v: factor(e),
    "simplify": lambda e, v: simplify(e),
    "trigsimp": lambda e, v: trigsimp(e),
    "radsimp": lambda e, v: radsimp(e),
    "powsimp": lambda e, v: powsimp(e),
    "expand_trig": lambda e, v: expand_trig(e),
    "cancel": lambda e, v: cancel(e),
    "apart": lambda e, v: apart(e),
    "diff": lambda e, v: diff(e, v) if e.free_symbols else e,
    "collect_x": lambda e, v: collect(e, v) if v in e.free_symbols else e,
    "identity": lambda e, v: e,
}


def list_rules() -> list[str]:
    """返回所有可用规则名"""
    return list(BUILTIN_RULES.keys())


# ============ 核心：BFS 构建 DAG ============


def _expr_to_latex(expr) -> str:
    """sympy → LaTeX（失败返回 str）"""
    try:
        return sympy_latex(expr)
    except Exception:
        return str(expr)


def build_chain(
    start_latex: str,
    target_latex: str,
    var: str = "x",
    max_depth: int = 4,
    rules: list[str] | None = None,
    timeout_s: float = 5.0,
) -> DerivationDAG:
    """BFS 构建推导 DAG

    Args:
        start_latex: 起点 LaTeX
        target_latex: 终点 LaTeX
        var: 主变量
        max_depth: 最大步数（防止无限展开）
        rules: 候选规则列表（None = 用全部内置）
        timeout_s: 总超时

    Returns:
        DerivationDAG（含 mermaid / json 序列化）
    """
    t0 = time.time()
    rules = rules or list_rules()
    sym_var = Symbol(var)

    # 解析起点 + 终点
    try:
        start_expr = sympify(start_latex, locals={"Symbol": Symbol})
        target_expr = sympify(target_latex, locals={"Symbol": Symbol})
    except Exception:
        try:
            start_expr = sympify(start_latex)
            target_expr = sympify(target_latex)
        except Exception:
            return DerivationDAG(
                start_latex=start_latex,
                target_latex=target_latex,
                success=False,
                elapsed_s=time.time() - t0,
                rules_used=rules,
            )

    dag = DerivationDAG(
        start_latex=start_latex,
        target_latex=target_latex,
        rules_used=rules,
    )

    start_node = DerivationNode(
        id=0,
        expr_latex=_expr_to_latex(start_expr),
        expr_str=str(start_expr),
        depth=0,
        is_start=True,
    )
    dag.nodes.append(start_node)

    # 起点 == 终点直接命中（无推导步骤，reason="trivially_equal"）
    if start_expr == target_expr or simplify(start_expr - target_expr) == 0:
        start_node.is_target = True
        dag.success = True
        dag.target_node_id = 0
        dag.steps = 0
        dag.elapsed_s = time.time() - t0
        dag.rules_used = []
        dag.reason = "trivially_equal"
        return dag

    # 如果 start 字符串本身 == target 字符串
    # → 算"无推导"（start 与 target 已是同一表达式）
    if str(start_expr) == str(target_expr):
        start_node.is_target = True
        dag.success = True
        dag.target_node_id = 0
        dag.steps = 0
        dag.elapsed_s = time.time() - t0
        dag.rules_used = []
        dag.reason = "trivially_equal"
        return dag

    # BFS
    visited: dict[str, int] = {start_node.expr_hash: 0}  # expr_hash → node_id
    queue: deque[DerivationNode] = deque([start_node])
    rules_used_actual: list[str] = []
    timed_out = False

    while queue:
        if time.time() - t0 > timeout_s:
            timed_out = True
            break
        current = queue.popleft()
        if current.depth >= max_depth:
            continue

        for rule_name in rules:
            if rule_name not in BUILTIN_RULES:
                continue
            try:
                rule_fn = BUILTIN_RULES[rule_name]
                new_expr = rule_fn(
                    current.expr_str and sympify(current.expr_str, locals={"Symbol": Symbol}),
                    sym_var,
                )
                # 优先用已解析对象（避免二次解析）
                if not isinstance(new_expr, type(start_expr)):
                    new_expr = rule_fn(sympify(current.expr_str), sym_var)
            except Exception:
                continue

            new_str = str(new_expr)
            new_hash = hashlib.md5(new_str.encode("utf-8")).hexdigest()[:12]

            # 环检测：已访问过的不再展开
            if new_hash in visited:
                continue

            dag.explored += 1
            new_id = len(dag.nodes)
            new_node = DerivationNode(
                id=new_id,
                expr_latex=_expr_to_latex(new_expr),
                expr_str=new_str,
                depth=current.depth + 1,
                parent_id=current.id,
                rule_applied=rule_name,
            )

            # 目标判定（应用规则后才算推导：要求表达式字符串 == target 字符串，
            # 或 simplify 后 == 0。避免 start == target 直接命中被误判为"无推导"）
            is_target = False
            try:
                target_str = str(target_expr)
                if new_str == target_str:
                    is_target = True
                else:
                    diff = simplify(new_expr - target_expr)
                    if diff == 0 and new_str != str(start_expr):
                        is_target = True
            except Exception:
                pass

            if is_target:
                new_node.is_target = True
                dag.nodes.append(new_node)
                dag.edges.append((current.id, new_id, rule_name))
                if rule_name not in rules_used_actual:
                    rules_used_actual.append(rule_name)
                dag.success = True
                dag.target_node_id = new_id
                dag.steps = new_node.depth
                dag.elapsed_s = time.time() - t0
                dag.rules_used = rules_used_actual
                dag.reason = "found_path"
                return dag

            visited[new_hash] = new_id
            dag.nodes.append(new_node)
            dag.edges.append((current.id, new_id, rule_name))
            if rule_name not in rules_used_actual:
                rules_used_actual.append(rule_name)
            queue.append(new_node)

    # 未找到
    dag.elapsed_s = time.time() - t0
    dag.rules_used = rules_used_actual
    if timed_out:
        dag.reason = "timeout"
    elif dag.explored == 0:
        dag.reason = "no_rule_match"
    else:
        dag.reason = "max_depth_exceeded"
    return dag


# ============ Mermaid 渲染 ============


def dag_to_mermaid(dag: DerivationDAG) -> str:
    """DAG → Mermaid flowchart 字符串（前端可视化用）"""
    lines = ["```mermaid", "flowchart TD"]

    # 节点
    for node in dag.nodes:
        # 简化 LaTeX（去掉反斜杠等 mermaid 关键字冲突字符）
        safe_label = (
            node.expr_latex.replace("\\", "\\\\")
            .replace('"', "'")
            .replace("[", "(")
            .replace("]", ")")
            .replace("\n", " ")
        )
        if len(safe_label) > 50:
            safe_label = safe_label[:47] + "..."
        shape_open, shape_close = ("((", "))") if node.is_start else ("[", "]")
        if node.is_target:
            shape_open, shape_close = ("{{", "}}")
        lines.append(f'    n{node.id}{shape_open}"{safe_label}"{shape_close}')

    # 边
    for from_id, to_id, rule in dag.edges:
        safe_rule = rule.replace('"', "'")
        lines.append(f'    n{from_id} -->|"{safe_rule}"| n{to_id}')

    # 目标高亮（subgraph）
    if dag.target_node_id is not None:
        lines.append("    classDef target fill:#90EE90,stroke:#006400,stroke-width:3px")
        lines.append("    classDef start fill:#87CEEB,stroke:#00008B,stroke-width:2px")
        lines.append("    class n0 start")
        lines.append(f"    class n{dag.target_node_id} target")

    lines.append("```")
    return "\n".join(lines)


# ============ 重放（从 JSON 验证 + 性能）============


def replay_dag(dag_dict: dict, var: str = "x") -> dict:
    """从序列化 DAG 重放：每步调 sympy 规则验证确定性 + 测耗时

    Args:
        dag_dict: DerivationDAG.to_dict() 输出
        var: 主变量

    Returns:
        {
            "ok": bool,
            "steps_replayed": int,
            "all_match": bool,        # 每步重放结果 == 记录的 expr
            "mismatches": [{step, expected, got}],
            "elapsed_s": float,
            "per_step": [{step, rule, elapsed_ms}],  # D3.4 性能指标
            "final_expr": str,         # 重放最终表达式
            "audit_log": [...],        # D3.4 审计记录
        }
    """
    t0 = time.time()
    sym_var = Symbol(var)
    nodes = dag_dict.get("nodes", [])
    edges = dag_dict.get("edges", [])
    node_map = {n["id"]: n for n in nodes}

    if not nodes:
        return {
            "ok": False,
            "error": "empty DAG",
            "steps_replayed": 0,
            "all_match": False,
            "mismatches": [],
            "elapsed_s": time.time() - t0,
        }

    # 重放：从根节点 DFS，每步验证 + D3.4 性能指标 + 审计日志
    steps_replayed = 0
    mismatches = []
    per_step: list[dict] = []  # D3.4 每步耗时
    audit_log: list[dict] = []  # D3.4 审计日志

    # 按 parent 关系拓扑序遍历（用 edges）
    replayed_exprs: dict[int, object] = {
        0: sympify(nodes[0]["expr_str"], locals={"Symbol": Symbol})
    }

    for from_id, to_id, rule in edges:
        if to_id not in node_map:
            continue
        target_node = node_map[to_id]
        t_step = time.time()
        try:
            parent_expr = replayed_exprs[from_id]
            rule_fn = BUILTIN_RULES.get(rule, _identity)
            new_expr = rule_fn(parent_expr, sym_var)
            step_ok = True
            step_error = None
        except Exception as e:
            new_expr = None
            step_ok = False
            step_error = f"{type(e).__name__}: {e}"

        elapsed_ms = round((time.time() - t_step) * 1000, 2)
        per_step.append(
            {
                "step": steps_replayed,
                "from_id": from_id,
                "to_id": to_id,
                "rule": rule,
                "elapsed_ms": elapsed_ms,
                "ok": step_ok,
            }
        )
        audit_log.append(
            {
                "step": steps_replayed,
                "rule": rule,
                "from_node": from_id,
                "to_node": to_id,
                "elapsed_ms": elapsed_ms,
                "ok": step_ok,
                "error": step_error,
                "timestamp": time.time(),
            }
        )

        if not step_ok:
            mismatches.append(
                {
                    "step": steps_replayed,
                    "from_id": from_id,
                    "to_id": to_id,
                    "rule": rule,
                    "expected": target_node.get("expr_str"),
                    "got": f"<exception: {step_error}>",
                }
            )
            steps_replayed += 1
            continue

        replayed_exprs[to_id] = new_expr
        expected_str = target_node.get("expr_str", "")
        try:
            expected_expr = sympify(expected_str, locals={"Symbol": Symbol})
            if simplify(new_expr - expected_expr) != 0:
                mismatches.append(
                    {
                        "step": steps_replayed,
                        "from_id": from_id,
                        "to_id": to_id,
                        "rule": rule,
                        "expected": expected_str,
                        "got": str(new_expr),
                    }
                )
        except Exception:
            pass
        steps_replayed += 1

    all_match = len(mismatches) == 0
    final_expr = ""
    if dag_dict.get("target_node_id") is not None:
        tid = dag_dict["target_node_id"]
        if tid in replayed_exprs:
            final_expr = str(replayed_exprs[tid])

    # D3.4 性能汇总
    total_ms = sum(s["elapsed_ms"] for s in per_step)
    p50_ms = sorted([s["elapsed_ms"] for s in per_step])[len(per_step) // 2] if per_step else 0
    p95_ms = (
        sorted([s["elapsed_ms"] for s in per_step])[int(len(per_step) * 0.95)] if per_step else 0
    )

    return {
        "ok": all_match,
        "steps_replayed": steps_replayed,
        "all_match": all_match,
        "mismatches": mismatches,
        "elapsed_s": round(time.time() - t0, 3),
        "final_expr": final_expr,
        "per_step": per_step,  # D3.4
        "perf": {  # D3.4 性能汇总
            "total_ms": round(total_ms, 2),
            "p50_ms": round(p50_ms, 2),
            "p95_ms": round(p95_ms, 2),
            "slowest_step": max(per_step, key=lambda s: s["elapsed_ms"]) if per_step else None,
        },
        "audit_log": audit_log,  # D3.4 审计
    }


def write_audit_log(audit_log: list[dict], target_path: str | None = None) -> str:
    """D3.4 审计日志写入（追加到 v5/audit/derivation_replay.jsonl）

    Args:
        audit_log: replay_dag 返回的 audit_log
        target_path: 自定义路径（默认 v5/audit/derivation_replay.jsonl）

    Returns:
        写入的文件路径
    """
    import json
    from pathlib import Path

    if target_path is None:
        audit_dir = Path("/home/jiuben/tdx-data-feed/v5/audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        target_path = audit_dir / "derivation_replay.jsonl"

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with open(target, "a", encoding="utf-8") as f:
        for entry in audit_log:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return str(target)
