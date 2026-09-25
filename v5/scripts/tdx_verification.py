#!/usr/bin/env python3
"""tdx_verification.py · M4 自动验证

跑 3 类测试，输出 JUnit XML + JSON 摘要：
1. math_sympy 单元测试（127 用例）
2. workbench backend 测试（如有）
3. 业务 smoke test（实际调 math-sympy-http /health + /tools/are_equiv）

journal 日志 + 退出码：
- exit 0：全部通过
- exit 1：有失败
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

MATH_HTTP_URL = os.environ.get("MATH_HTTP_URL", "http://127.0.0.1:8002")
WORKBENCH_URL = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8010")
V5_ROOT = Path("/home/jiuben/tdx-data-feed")
RESULT_DIR = V5_ROOT / "v5/audit/verification"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_math_sympy_tests() -> dict:
    """跑 math_sympy 测试"""
    t0 = time.time()
    try:
        result = subprocess.run(
            [str(V5_ROOT / "venv/bin/python"), str(V5_ROOT / "v5/tests/test_math_sympy.py")],
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = round(time.time() - t0, 1)
        # 解析输出（找 "X pass / Y fail"）
        output = result.stdout + result.stderr
        passed = failed = 0
        for line in output.split("\n"):
            if "测试结果" in line:
                # 格式：测试结果: 127 pass / 0 fail
                parts = line.split()
                try:
                    passed = int(parts[parts.index("pass") - 1])
                    failed = int(parts[parts.index("fail") - 1])
                except ValueError, IndexError:
                    pass
        return {
            "name": "math_sympy",
            "passed": passed,
            "failed": failed,
            "elapsed_s": elapsed,
            "exit_code": result.returncode,
            "ok": result.returncode == 0 and failed == 0,
        }
    except subprocess.TimeoutExpired:
        return {"name": "math_sympy", "ok": False, "error": "timeout 300s"}
    except Exception as e:
        return {"name": "math_sympy", "ok": False, "error": str(e)}


def run_smoke_test() -> dict:
    """业务 smoke test（实际调 math-sympy-http）"""
    t0 = time.time()
    try:
        import httpx

        cases = [
            ("are_equiv", {"latex_a": "x", "latex_b": "x"}, "equivalent", True),
            ("sympy_compute", {"latex": "x+1", "action": "simplify"}, "ok", True),
            (
                "prove_equiv_with_precondition",
                {"latex_a": "x+1", "latex_b": "1+x", "preconditions": []},
                "proved",
                True,
            ),
            ("math_strategy_route_and_execute", {"latex": "x^2", "action": "simplify"}, "ok", True),
        ]
        results = []
        ok = True
        with httpx.Client(timeout=5.0) as c:
            for tool, args, check_field, expected in cases:
                try:
                    r = c.post(f"{MATH_HTTP_URL}/tools/{tool}", json=args)
                    data = r.json() if r.status_code == 200 else {}
                    actual = data.get(check_field, False)
                    case_ok = actual == expected
                    results.append(
                        {
                            "tool": tool,
                            "ok": case_ok,
                            "check_field": check_field,
                            "expected": expected,
                            "actual": actual,
                        }
                    )
                    if not case_ok:
                        ok = False
                except Exception as e:
                    results.append({"tool": tool, "ok": False, "error": str(e)[:100]})
                    ok = False
        return {
            "name": "smoke_test",
            "elapsed_s": round(time.time() - t0, 2),
            "cases_total": len(cases),
            "cases_passed": sum(1 for r in results if r["ok"]),
            "results": results,
            "ok": ok,
        }
    except Exception as e:
        return {"name": "smoke_test", "ok": False, "error": str(e)}


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    summary = {
        "ts": _utc_now().isoformat(),
        "tests": [],
    }

    # 1. math_sympy
    print("[1/2] Running math_sympy tests...", file=sys.stderr)
    r1 = run_math_sympy_tests()
    summary["tests"].append(r1)
    print(
        f"  → {r1.get('passed', '?')} pass / {r1.get('failed', '?')} fail "
        f"({r1.get('elapsed_s', 0)}s) {'✓' if r1.get('ok') else '✗'}",
        file=sys.stderr,
    )

    # 2. smoke test
    print("[2/3] Running smoke test...", file=sys.stderr)
    r2 = run_smoke_test()
    summary["tests"].append(r2)
    if r2.get("ok"):
        print(
            f"  → {r2.get('cases_passed')}/{r2.get('cases_total')} pass ({r2.get('elapsed_s')}s) ✓",
            file=sys.stderr,
        )
    else:
        print(f"  → ✗ {r2.get('error', 'failed')}", file=sys.stderr)

    # 总结
    summary["all_ok"] = all(t.get("ok") for t in summary["tests"])
    print(
        f"\n===== Summary: {'✓ ALL PASS' if summary['all_ok'] else '✗ SOME FAILED'} =====",
        file=sys.stderr,
    )

    # 写 JSON 结果
    out_file = RESULT_DIR / f"verification-{_utc_now().strftime('%Y%m%d-%H%M%S')}.json"
    out_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # 同时写 latest 软链
    latest = RESULT_DIR / "latest.json"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out_file.name)

    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
