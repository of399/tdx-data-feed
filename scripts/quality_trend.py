#!/usr/bin/env python
"""质量债务趋势追踪：29 条历史遗留问题的下降曲线。

三个度量层次（互相不可替代）：
  L1 台账层   29 条 guard=NONE 的遗留问题 → 有多少条已被自动守卫接管（guard != NONE）
  L2 代码层   语义债务池（SEM001/2/3 命中条数）+ ruff 债务池 → 逐条修复可见的下降
  L3 维度层   ruff / semantic 两个基线的额度总数 → 门禁实际承认的存量

用法
  venv/bin/python scripts/quality_trend.py snapshot   # 记录今日快照
  venv/bin/python scripts/quality_trend.py report    # 打印趋势（含与上次的差值）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# 快照放在 config/ 下（data/ 被 .gitignore 忽略 → 曲线必须进版本库才能被 review）
SNAP_DIR = ROOT / "config" / "quality_trend"

# 语义守卫 → 台账条目的覆盖关系（守卫接管了哪些遗留问题）
GUARD_COVERAGE: dict[str, tuple[str, str]] = {
    "KI-ATOMIC-001": ("SEM001", "scripts/plan_e_backtest.py"),
    "KI-ATOMIC-002": ("SEM001", "scripts/plan_e_journal.py"),
    "KI-ATOMIC-003": ("SEM001", "scripts/plan_e_journal.py"),
    "KI-ATOMIC-004": ("SEM001", "scripts/train_daban_model.py"),
    "KI-SILENT-001": ("SEM002", "scripts"),
    "KI-SILENT-002": ("SEM002", "tdxfeed"),
    "KI-MCP-001": ("SEM003", "skill_router/runtime.py"),
    "KI-RULE-001": ("SEM002", "scripts"),
}

GUARD_NOTE = {
    "KI-RULE-001": "ruff 的 BLE001 目录级豁免已被 SEM002 覆盖：新增行的盲吞错不再被豁免",
}


def _load(name: str):
    """动态加载同目录模块（避免顶层 import 影响 isort）。"""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def measure() -> dict[str, Any]:
    """采集当前所有度量。"""
    semantic = _load("ci_semantic_guard")
    findings = semantic.scan_paths()
    sem_by_rule = Counter(f.code for f in findings)
    sem_by_file = Counter(f.file for f in findings)

    ledger_mod = _load("audit_ledger")
    ledger = ledger_mod.Ledger.load()
    issues = ledger.issues
    no_guard = [i for i in issues if str(i.guard) == "NONE"]
    guarded = [i for i in issues if str(i.guard) != "NONE"]
    still_present = 0
    for iss in issues:
        state, _ = ledger_mod.scan_issue(iss)
        if state == "STILL_PRESENT":
            still_present += 1

    coverage: dict[str, dict[str, Any]] = {}
    for iid, (rule, scope) in GUARD_COVERAGE.items():
        exact = scope.endswith((".py", ".toml"))
        hits_rule = sum(1 for f in findings if f.code == rule and (
            f.file == scope if exact else f.file.startswith(scope)))
        coverage[iid] = {"rule": rule, "scope": scope, "hits": hits_rule,
                         "note": GUARD_NOTE.get(iid, "")}

    def baseline_total(name: str) -> int:
        p = ROOT / "config" / name
        if not p.exists():
            return 0
        return int(json.loads(p.read_text(encoding="utf-8")).get("total", 0))

    return {
        "date": date.today().isoformat(),
        "ledger": {
            "total": len(issues),
            "no_guard": len(no_guard),
            "guarded": len(guarded),
            "still_present": still_present,
            "no_guard_ids": sorted(i.id for i in no_guard),
        },
        "semantic_pool": {
            "total": len(findings),
            "by_rule": dict(sorted(sem_by_rule.items())),
            "files": len(sem_by_file),
        },
        "ruff_pool": {"total": baseline_total("ruff_baseline.json")},
        "baselines": {
            "ruff": baseline_total("ruff_baseline.json"),
            "semantic": baseline_total("semantic_baseline.json"),
        },
        "coverage": coverage,
    }


def snapshots() -> list[Path]:
    if not SNAP_DIR.exists():
        return []
    return sorted(SNAP_DIR.glob("trend_*.json"))


def save_snapshot(data: dict[str, Any]) -> Path:
    """原子写：先写 .tmp 再 os.replace（快照是趋势依据，不能出现半截文件）。"""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"trend_{data['date'].replace('-', '')}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_previous() -> dict[str, Any] | None:
    """上一次快照（不含今日）。"""
    today = date.today().strftime("%Y%m%d")
    prev = [p for p in snapshots() if p.stem != f"trend_{today}"]
    if not prev:
        return None
    return json.loads(prev[-1].read_text(encoding="utf-8"))


def cmd_snapshot(args: argparse.Namespace) -> int:
    data = measure()
    path = save_snapshot(data)
    print(f"✅ 快照已记录：{path}")
    print(f"   台账：无守卫 {data['ledger']['no_guard']} · 已接管 {data['ledger']['guarded']}"
          f" · 仍复现 {data['ledger']['still_present']}")
    print(f"   语义债务池：{data['semantic_pool']['total']} 条 {data['semantic_pool']['by_rule']}")
    print(f"   ruff 债务池：{data['ruff_pool']['total']} 条")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    data = measure()
    prev = load_previous()
    if args.json:
        print(json.dumps({"current": data, "previous": prev}, ensure_ascii=False,
                         indent=2))
        return 0

    print("=" * 72)
    print(f"质量债务趋势 · {data['date']}")
    print("=" * 72)
    print("\n【L1 台账层】29 条历史遗留问题的接管进度")
    print(f"  无守卫（会复发）：{data['ledger']['no_guard']} / {data['ledger']['total']}")
    print(f"  已被守卫接管：    {data['ledger']['guarded']}")
    print(f"  仍复现：          {data['ledger']['still_present']}")
    print("\n【L1b 语义守卫接管的台账条目】")
    for iid, info in sorted(data["coverage"].items()):
        print(f"  {iid:<16} {info['rule']}  {info['scope']:<32} 代码点 {info['hits']:>3} 个")
        if info.get("note"):
            print(f"  {'':<16} └─ {info['note']}")

    print("\n【L2 代码层】债务池")
    print(f"  语义债务池：{data['semantic_pool']['total']} 条 "
          f"{data['semantic_pool']['by_rule']}")
    print(f"  ruff 债务池：{data['ruff_pool']['total']} 条")

    if prev:
        print("\n【与上次快照的差值】")
        rows = (
            ("语义债务池", data["semantic_pool"]["total"], prev["semantic_pool"]["total"]),
            ("ruff 债务池", data["ruff_pool"]["total"], prev["ruff_pool"]["total"]),
            ("台账无守卫条数", data["ledger"]["no_guard"], prev["ledger"]["no_guard"]),
            ("台账仍复现条数", data["ledger"]["still_present"],
             prev["ledger"]["still_present"]),
        )
        net = 0
        for label, cur, old in rows:
            delta = cur - old
            net += delta
            arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "＝")
            print(f"  {label:<14} {old} → {cur}   {arrow} {delta:+d}")
        print(f"  → 净变化 {net:+d}（负数 = 净下降，正数 = 净增长）")
    else:
        print("\n（尚无历史快照，本次为首个基线点；执行 "
              "`make quality-trend-snapshot` 开始积累曲线）")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="质量债务趋势追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("snapshot", help="记录今日快照")
    p1.set_defaults(func=cmd_snapshot)
    p2 = sub.add_parser("report", help="打印趋势")
    p2.add_argument("--json", action="store_true")
    p2.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
