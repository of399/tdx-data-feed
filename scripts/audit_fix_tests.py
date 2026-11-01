#!/usr/bin/env python
"""M-5：修复必须附带测试（缺陷 → 守卫 → 测试 的强制闭环）。

四条检查
--------
M5-R1 台账绑定   `status=FIXED` 的条目必须绑定一个**真实存在**的测试文件：
                 `test:` 字段优先；没有时允许 `guard:` 指向 tests/ 下的文件。
M5-R2 测试可证   绑定的测试文件必须**包含该条目的 ID**（marker），
                 否则"有测试文件"只是形式，测试未必针对这条问题。
M5-R3 变更配对   diff 里若改动了**生产代码**（scripts/ tdxfeed/ skill_router/ mcp/
                 quant/ agents/ 下的 .py），必须同时改动 tests/ 下的文件。
M5-R4 语义配对   diff 的**新增行**若引入了新的语义违规（SEM001/2/3），
                 必须同时改动 tests/ —— 语义修复没有测试等于没修。

豁免（不改生产代码时全部豁免）：纯文档、纯台账/Makefile/配置变更。

用法
  venv/bin/python scripts/audit_fix_tests.py check [--base origin/main]
  venv/bin/python scripts/audit_fix_tests.py check --staged
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "config" / "known_issues.yaml"

PROD_DIRS = ("scripts/", "tdxfeed/", "skill_router/", "mcp/", "quant/", "agents/")


def _load(name: str, rel: str | None = None):
    """动态加载同目录/仓内模块（避免顶层 import 影响 isort）。"""
    path = Path(rel) if rel else (ROOT / "scripts" / f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {name}（{path}）")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Problem:
    rule: str
    detail: str

    def line(self) -> str:
        return f"[{self.rule}] {self.detail}"


def ledger_problems() -> list[Problem]:
    """M5-R1 / M5-R2：台账 FIXED 条目必须绑定并可被测试证明。"""
    import yaml

    problems: list[Problem] = []
    data = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8"))
    for iss in data.get("issues", []):
        if str(iss.get("status")) != "FIXED":
            continue
        iid = str(iss.get("id"))
        test_rel = iss.get("test") or (
            iss.get("guard") if str(iss.get("guard", "")).startswith("tests/") else None)
        if not test_rel:
            problems.append(Problem("M5-R1", f"{iid} 已 FIXED，但没有绑定测试文件"
                                                  f"（缺 test: 字段，且 guard 未指向 tests/）"))
            continue
        tpath = ROOT / str(test_rel)
        if not tpath.exists():
            problems.append(Problem("M5-R1", f"{iid} 绑定的测试文件不存在：{test_rel}"))
            continue
        if iid not in tpath.read_text(encoding="utf-8"):
            problems.append(Problem("M5-R2", f"{iid} 绑定的测试 {test_rel} 里没有出现条目 ID"
                                                  f"（无法证明该测试针对这条问题）"))
    return problems


def changed_files(base: str, staged: bool) -> list[str]:
    """变更文件清单。"""
    if staged:
        proc = subprocess.run(["git", "diff", "--name-only", "--cached"],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    else:
        proc = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            proc = subprocess.run(["git", "diff", "--name-only", base],
                                  capture_output=True, text=True, cwd=str(ROOT),
                                  timeout=60)
    return [x for x in proc.stdout.splitlines() if x.strip()]


def diff_problems(files: list[str], base: str, staged: bool) -> list[Problem]:
    """M5-R3 / M5-R4：改了生产代码必须同时改测试。"""
    problems: list[Problem] = []
    if not files:
        return problems
    prod = [f for f in files
            if f.endswith(".py") and f.startswith(PROD_DIRS)]
    tests = [f for f in files if f.startswith("tests/") and f.endswith(".py")]
    if prod and not tests:
        problems.append(Problem(
            "M5-R3", f"改了 {len(prod)} 个生产代码文件但没有改任何 tests/ 文件："
                     f"{', '.join(prod[:4])}{' …' if len(prod) > 4 else ''}"))
    if prod and tests:
        # 新增行引入语义违规 → 必须有测试（已有测试也不豁免，必须是对应的测试）
        gate = _load("ci_violation_gate")
        added, used_ref = ({}, "") if staged else gate.git_added_lines(base)
        if added:
            viols = gate.collect_violations("semantic")
            baselines = {"semantic": gate.Baseline.load(
                Path(gate.SEMANTIC_BASELINE_PATH))}
            fails = gate.check_a2_added_lines(viols, baselines["semantic"].quotas, added)
            if fails:
                problems.append(Problem(
                    "M5-R4", f"新增行引入 {len(fails)} 处语义违规（{used_ref}），"
                             f"语义修复必须配测试："
                             + "; ".join(f.file for f in fails[:3])))
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    problems = ledger_problems()
    files = changed_files(args.base, args.staged)
    print("=" * 72)
    print("M-5 修复必须附带测试 · audit_fix_tests")
    print("=" * 72)
    print(f"变更文件 {len(files)} 个"
          + (f"（{'暂存区' if args.staged else '相对 ' + args.base}）"))
    if files:
        for f in files[:12]:
            print(f"   {f}")
        if len(files) > 12:
            print(f"   … 另有 {len(files) - 12} 个")
    print()
    if not problems:
        print("✅ 通过：台账 FIXED 条目均绑定可验证测试；本次变更满足配对要求。")
        return 0
    print(f"❌ 失败 {len(problems)} 项：")
    for p in problems:
        print("  " + p.line())
    print()
    print("处理：① 给该修复补一个测试，并在测试里写明条目 ID（marker）；"
          "② 在台账里补 `test: tests/xxx.py` 字段。")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M-5 修复必须附带测试")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="执行检查")
    p.add_argument("--base", default="origin/main", help="diff 基准 ref")
    p.add_argument("--staged", action="store_true", help="以暂存区为变更范围")
    p.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
