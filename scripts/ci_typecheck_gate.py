#!/usr/bin/env python
"""M-6 门禁纯化 ④：mypy 对新增模块阻断（存量冻结为基线）。

与 `ci_violation_gate.py` 同一套思路：
  - 判定单位 = (文件 × 错误码) 配额
  - 存量错误冻结进 `config/mypy_baseline.json`（不阻断）
  - 新增错误 = 净增长 → 退出码 1，CI 红
  - 基线只降不升；上升须 `--allow-growth --reason`（30 天审批，到期自动失败）

目标模块：skill_router/、mcp/、tdxfeed/execution/（M-6 定义的新增模块）。

用法
  venv/bin/python scripts/ci_typecheck_gate.py check
  venv/bin/python scripts/ci_typecheck_gate.py update
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "config" / "mypy_baseline.json"
TARGETS = ("skill_router/", "mcp/", "tdxfeed/execution/")
GROWTH_APPROVAL_MAX_DAYS = 30

# 守卫自检：路径换算失败必须计数，不能静默吞掉
PATH_OUTSIDE_FAILS = 0

ERR_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)?:?\s*error:\s*(?P<msg>.*?)\s*\[(?P<code>[\w-]+)\]$")


def run_mypy(timeout: int = 600) -> list[tuple[str, str, str]]:
    """跑 mypy，返回 [(相对路径, 错误码, 消息)]。"""
    env = dict(os.environ)
    env["MYPYPATH"] = str(ROOT)
    cmd = [sys.executable, "-m", "mypy", *TARGETS,
           "--explicit-package-bases", "--ignore-missing-imports",
           "--no-error-summary", "--no-color"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                          env=env, timeout=timeout)
    out: list[tuple[str, str, str]] = []
    global PATH_OUTSIDE_FAILS
    for line in (proc.stdout or "").splitlines():
        m = ERR_RE.match(line.strip())
        if not m:
            continue
        f = m.group("file")
        if Path(f).is_absolute():
            try:
                rel = str(Path(f).relative_to(ROOT))
            except ValueError:
                PATH_OUTSIDE_FAILS += 1
                rel = f
        else:
            rel = f
        out.append((rel, m.group("code"), m.group("msg")[:120]))
    return out


def quotas_from(errors: list[tuple[str, str, str]]) -> dict[str, dict[str, int]]:
    by: dict[str, dict[str, int]] = {}
    for f, code, _ in errors:
        by.setdefault(f, {})
        by[f][code] = by[f].get(code, 0) + 1
    return {k: dict(sorted(v.items())) for k, v in sorted(by.items())}


def load_baseline() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {"version": 1, "total": 0, "by_file_code": {}}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def save_baseline(data: dict[str, Any]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BASELINE_PATH.with_suffix(BASELINE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, BASELINE_PATH)


def cmd_check(args: argparse.Namespace) -> int:
    base = load_baseline()
    errors = run_mypy()
    cur = quotas_from(errors)
    old = base.get("by_file_code", {})
    fails: list[str] = []
    for f, codes in cur.items():
        for code, n in codes.items():
            old_n = int((old.get(f) or {}).get(code, 0))
            if n > old_n:
                fails.append(f"[mypy 净增长] {f} · {code}: {old_n} → {n} (+{n - old_n})")
    print("=" * 72)
    print("mypy 增量阻断门禁 · ci_typecheck_gate")
    print("=" * 72)
    print(f"目标模块：{' '.join(TARGETS)}")
    print(f"实测错误 {len(errors)} 条（{len(cur)} 文件） · 基线 {base.get('total', 0)} 条")
    if PATH_OUTSIDE_FAILS:
        print(f"⚠️  路径换算失败 {PATH_OUTSIDE_FAILS} 次（已计数，未静默吞掉）")
    growth = base.get("growth_approved")
    if growth:
        exp = growth.get("expires")
        if exp and datetime.strptime(str(exp), "%Y-%m-%d").date() < date.today():
            fails.append(f"[mypy 基线完整性] 增长审批已于 {exp} 到期，须复审或偿还")
        else:
            print(f"⚠️  基线增长审批：{growth.get('reason')}（至 {exp}）")
    if base.get("total", 0) < len(errors):
        fails.append(f"[mypy 基线完整性] 基线 {base.get('total')} < 实测 {len(errors)}")
    print()
    if not fails:
        print("✅ 通过：mypy 无新增类型错误。")
        return 0
    print(f"❌ 失败 {len(fails)} 项：")
    for f in fails[:30]:
        print("  " + f)
    print()
    print("处理：① 修掉新增类型错误；② 或把该模块移出 mypy 门禁范围（需登记理由）。")
    return 1


def cmd_update(args: argparse.Namespace) -> int:
    base = load_baseline()
    errors = run_mypy()
    cur = quotas_from(errors)
    old = base.get("by_file_code", {})
    grown = [(f, code, int((old.get(f) or {}).get(code, 0)), n)
             for f, codes in cur.items() for code, n in codes.items()
             if n > int((old.get(f) or {}).get(code, 0))]
    dropped = sum(1 for f, codes in old.items() for code, n in codes.items()
                  if n > int((cur.get(f) or {}).get(code, 0)))
    bootstrap = not BASELINE_PATH.exists()
    if bootstrap:
        print(f"ℹ️  mypy 基线不存在 → 按当前实测创建初始基线"
              f"（{len(errors)} 条，{len(cur)} 文件）")
    elif grown and not args.allow_growth:
        print("❌ 拒绝更新 mypy 基线：以下 (文件,错误码) 错误数上升，基线只降不升。")
        for f, code, o, n in grown[:20]:
            print(f"   {f} · {code}: {o} → {n}")
        print("\n处理：① 先修掉新增类型错误；② 或显式 `--allow-growth --reason \"...\"`。")
        return 1
    data: dict[str, Any] = {
        "version": 1,
        "created": base.get("created", date.today().isoformat()),
        "updated": date.today().isoformat(),
        "measured_at": date.today().isoformat(),
        "targets": list(TARGETS),
        "total": len(errors),
        "files": len(cur),
        "by_file_code": cur,
    }
    if grown and not bootstrap:
        if not args.reason:
            print("❌ --allow-growth 必须同时给 --reason")
            return 2
        data["growth_approved"] = {
            "reason": args.reason,
            "approved_at": date.today().isoformat(),
            "expires": (date.today() + timedelta(days=GROWTH_APPROVAL_MAX_DAYS)).isoformat(),
        }
    save_baseline(data)
    print(f"✅ mypy 基线已更新：总量 {base.get('total', 0)} → {len(errors)}"
          f"（收紧 {dropped} 格，放宽 {len(grown)} 格）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mypy 增量阻断门禁")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("check", help="执行检查")
    p1.set_defaults(func=cmd_check)
    p2 = sub.add_parser("update", help="按实测收紧基线")
    p2.add_argument("--allow-growth", action="store_true")
    p2.add_argument("--reason", default="")
    p2.set_defaults(func=cmd_update)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
