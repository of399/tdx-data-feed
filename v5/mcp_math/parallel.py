"""
mcp_math/parallel.py · v5-VE1-M2 §12.7 D6.5

D6.5 并行执行：用 ProcessPoolExecutor 把大批量 are_equiv 调用并发化。

接口：
  batch_are_equiv(pairs, max_workers=None) → list[dict]

实现要点：
  - 每对 (latex_a, latex_b) 独立调用 _cached_equiv 逻辑
  - 进程隔离避免 GIL，N 进程并发 = N 倍速度
  - worker 内独立 sqlite 连接（多进程安全）
  - 单对失败不影响其他对（异常捕获 → result["error"]）
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# 让 standalone 执行时也能找到 mcp_math 同目录模块
_PKG_DIR = Path(__file__).parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

import math_cache  # noqa: E402


def _equiv_worker(args: tuple[str, str]) -> dict:
    """单对等价判定 worker（独立进程）。"""
    import math_errors  # noqa
    from sympy import simplify
    from sympy.parsing.latex import parse_latex

    latex_a, latex_b = args
    try:
        need_compute, reason = math_cache.prefilter(latex_a, latex_b)
        if not need_compute:
            return {
                "equivalent": False,
                "diff": None,
                "method": "prefilter",
                "reason": reason,
                "latex_a": latex_a[:80],
                "latex_b": latex_b[:80],
            }
        cached = math_cache.get_cached(latex_a, latex_b, action="equiv")
        if cached is not None:
            cached["method"] = "cache"
            cached["latex_a"] = latex_a[:80]
            cached["latex_b"] = latex_b[:80]
            return cached
        a = parse_latex(latex_a)
        b = parse_latex(latex_b)
        diff_val = simplify(a - b)
        result = {
            "equivalent": diff_val == 0,
            "diff": str(diff_val),
            "method": "sympy.simplify",
            "reason": "computed",
            "latex_a": latex_a[:80],
            "latex_b": latex_b[:80],
        }
        math_cache.set_cached(latex_a, latex_b, "equiv", result)
        return result
    except Exception as e:
        return {
            "equivalent": None,
            "diff": None,
            "method": "error",
            "error": str(e),
            "latex_a": latex_a[:80],
            "latex_b": latex_b[:80],
        }


def batch_are_equiv(
    pairs: list[tuple[str, str]],
    max_workers: int | None = None,
) -> list[dict]:
    """
    并行批量等价判定。返回与 pairs 等长的 result 列表。
    max_workers 默认 = CPU 核数 - 1。
    """
    if not pairs:
        return []
    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    results: list[dict | None] = [None] * len(pairs)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(_equiv_worker, pair): i for i, pair in enumerate(pairs)}
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                results[i] = future.result()
            except Exception as e:
                results[i] = {
                    "equivalent": None,
                    "diff": None,
                    "method": "worker_crash",
                    "error": str(e),
                }
    return results  # type: ignore[return-value]


def parallel_scan_notes(
    note_dir: str,
    pattern: str = "*.md",
    batch_size: int = 50,
    max_workers: int | None = None,
) -> dict:
    """
    全量扫描笔记目录，提取所有 LaTeX 公式对，做并行的等价判定。
    返回 {total, processed, equivalents, diffs, errors}。
    """
    from pathlib import Path

    note_path = Path(note_dir)
    if not note_path.exists():
        return {"error": f"dir not found: {note_dir}"}

    files = sorted(note_path.glob(pattern))
    pairs: list[tuple[str, str]] = []

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        import re

        formulas = re.findall(r"\$([^$]+)\$", content)
        if len(formulas) < 2:
            continue
        for i in range(len(formulas)):
            for j in range(i + 1, len(formulas)):
                pairs.append((formulas[i].strip(), formulas[j].strip()))

    if not pairs:
        return {"total": 0, "processed": 0, "equivalents": 0, "diffs": 0, "errors": 0}

    total_results = []
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        results = batch_are_equiv(batch, max_workers=max_workers)
        total_results.extend(results)

    eq = sum(1 for r in total_results if r.get("equivalent") is True)
    diff = sum(1 for r in total_results if r.get("equivalent") is False)
    errors = sum(1 for r in total_results if r.get("equivalent") is None)
    return {
        "total": len(pairs),
        "processed": len(total_results),
        "equivalents": eq,
        "diffs": diff,
        "errors": errors,
    }


if __name__ == "__main__":
    print("=== parallel 自检 ===")
    pairs = [
        (r"\int x dx", r"\frac{x^2}{2}"),
        (r"x + 1", r"1 + x"),
        (r"\sin^2 x + \cos^2 x", r"1"),
    ]
    results = batch_are_equiv(pairs)
    for p, r in zip(pairs, results, strict=False):
        print(f"  {p[0][:20]} vs {p[1][:20]} → eq={r.get('equivalent')}, method={r.get('method')}")
    print(f"\nCPU 核数: {os.cpu_count()}")
    print(f"实际并发: max_workers = {max(1, (os.cpu_count() or 2) - 1)}")
    print("\n=== 自检完毕 ===")
