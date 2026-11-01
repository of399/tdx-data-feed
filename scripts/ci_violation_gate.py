#!/usr/bin/env python
"""增量阻断门禁：新增行不得引入新违规 + 存量基线只降不升。

设计（详见 docs/INCREMENTAL_LINT_GATE.md）
------------------------------------
判定单位 = **(文件 × 规则) 配额**。基线 `config/ruff_baseline.json` 记录当前实测的
每个 (文件, 规则) 违规条数，作为"已付过债"的额度。

三条断言
  A1 净增长断言   current(file,rule) <= baseline(file,rule)
                 —— 存量只降不升；任一格上升即失败（"新增违规"的净额形态）
  A2 新增行断言   added_lines_violations(file,rule) <= baseline(file,rule)
                 —— 新增/修改行上不得引入该 (文件,规则) 的**新**违规；
                    额度用于允许重写"本来就脏"的存量行，但不允许再添
  A3 基线自升断言 基线文件不得被手工抬高：脚本 update 默认只允许下降；
                 确需上升必须显式 --allow-growth 并写入 growth_approved
                 （带理由与到期日，过期即失败）

用法
  venv/bin/python scripts/ci_violation_gate.py check [--base <git-ref>] [--no-diff]
  venv/bin/python scripts/ci_violation_gate.py update [--allow-growth --reason "..."]
  venv/bin/python scripts/ci_violation_gate.py show

退出码：0 通过 / 1 断言失败 / 2 工具错误
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "config" / "ruff_baseline.json"
BASELINE_VERSION = 1
GROWTH_APPROVAL_MAX_DAYS = 30


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Violation:
    """一条 ruff 违规。"""

    file: str
    row: int
    col: int
    code: str
    message: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.file, self.code)


@dataclass
class Baseline:
    """基线快照。"""

    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = BASELINE_PATH) -> Baseline:
        if not path.exists():
            return cls({"version": BASELINE_VERSION, "by_file_rule": {}, "total": 0})
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path = BASELINE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n", encoding="utf-8")

    @property
    def quotas(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for fname, rules in (self.data.get("by_file_rule") or {}).items():
            for rule, cnt in rules.items():
                out[(fname, rule)] = int(cnt)
        return out

    @property
    def total(self) -> int:
        return int(self.data.get("total", 0))


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def run_ruff(ruff_cmd: list[str] | None = None,
             cwd: Path = ROOT) -> list[Violation]:
    """跑 ruff 取 JSON 输出。ruff 有违规时退出码为 1，属正常。"""
    cmd = list(ruff_cmd or [sys.executable, "-m", "ruff"])
    cmd += ["check", ".", "--output-format=json", "--no-cache"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    out = (proc.stdout or "").strip()
    if not out:
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"ruff 执行失败 rc={proc.returncode}: {proc.stderr[:400]}")
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ruff 输出解析失败：{e}") from e

    viols: list[Violation] = []
    for item in data:
        fn = item.get("filename", "")
        if fn.startswith(str(cwd)):
            fn = fn[len(str(cwd)) + 1:]
        loc = item.get("location") or {}
        viols.append(Violation(
            file=fn,
            row=int(loc.get("row", 0)),
            col=int(loc.get("column", 0)),
            code=str(item.get("code", "")),
            message=str(item.get("message", ""))[:120],
        ))
    return viols


def parse_added_lines(diff_text: str) -> dict[str, set[int]]:
    """从 `git diff -U0` 输出解析每个文件的新增行号集合。"""
    out: dict[str, set[int]] = defaultdict(set)
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            if line.startswith("diff --git"):
                current = None
            continue
        if line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2) or "1")
            if count:
                out[current].update(range(start, start + count))
    return dict(out)


def git_added_lines(base: str, cwd: Path = ROOT) -> tuple[dict[str, set[int]], str]:
    """取 base...HEAD 的新增行集合；base 不可用时返回空。"""
    for ref in (base, base + "...HEAD", "HEAD~1"):
        proc = subprocess.run(["git", "diff", "-U0", ref, "HEAD"],
                              capture_output=True, text=True, cwd=str(cwd))
        if proc.returncode == 0 and proc.stdout.strip():
            return parse_added_lines(proc.stdout), ref
    return {}, ""


def measure(viols: list[Violation]) -> dict[tuple[str, str], int]:
    return Counter(v.key for v in viols)


def build_baseline(viols: list[Violation], *, ruff_version: str = "",
                   measured_at: str = "", existing: dict[str, Any] | None = None,
                   ) -> dict[str, Any]:
    """由实测违规构造基线字典（by_file_rule 粒度）。"""
    by_fr: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for v in viols:
        by_fr[v.file][v.code] += 1
    out: dict[str, Any] = {
        "version": BASELINE_VERSION,
        "created": (existing or {}).get("created", date.today().isoformat()),
        "updated": date.today().isoformat(),
        "measured_at": measured_at,
        "ruff_version": ruff_version,
        "total": len(viols),
        "files": len(by_fr),
        "by_rule": dict(sorted(Counter(v.code for v in viols).items())),
        "by_file_rule": {f: dict(sorted(r.items())) for f, r in sorted(by_fr.items())},
    }
    growth = (existing or {}).get("growth_approved")
    if growth:
        out["growth_approved"] = growth
    return out


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #
@dataclass
class Failure:
    """一条断言失败。"""

    assertion: str
    file: str
    rule: str
    baseline: int
    current: int
    detail: str = ""

    def line(self) -> str:
        d = self.current - self.baseline
        sign = f"+{d}" if d > 0 else str(d)
        return (f"[{self.assertion}] {self.file} · {self.rule} · "
                f"基线 {self.baseline} → 当前 {self.current} ({sign}) {self.detail}")


def check_a1_net_growth(current: dict[tuple[str, str], int],
                        quotas: dict[tuple[str, str], int]) -> list[Failure]:
    """A1 净增长断言：任一 (文件,规则) 的违规数不得超过基线。"""
    fails: list[Failure] = []
    for key, cnt in sorted(current.items()):
        base = quotas.get(key, 0)
        if cnt > base:
            fails.append(Failure("A1 净增长", key[0], key[1], base, cnt,
                                 "存量只降不升：此格违规数上升"))
    return fails


def check_a2_added_lines(viols: list[Violation], quotas: dict[tuple[str, str], int],
                         added: dict[str, set[int]]) -> list[Failure]:
    """A2 新增行断言：新增/修改行上的违规数不得超过该 (文件,规则) 的基线额度。"""
    on_added: Counter = Counter()
    detail: dict[tuple[str, str], list[int]] = defaultdict(list)
    for v in viols:
        rows = added.get(v.file)
        if rows and v.row in rows:
            on_added[v.key] += 1
            detail[v.key].append(v.row)
    fails: list[Failure] = []
    for key, cnt in sorted(on_added.items()):
        base = quotas.get(key, 0)
        if cnt > base:
            rows = ",".join(str(r) for r in sorted(detail[key])[:8])
            fails.append(Failure("A2 新增行", key[0], key[1], base, cnt,
                                 f"新增行违规于 L{rows}"))
    return fails


def check_a3_baseline_integrity(baseline: Baseline, current_total: int) -> list[Failure]:
    """A3 基线自升断言：基线不得低于实测；增长审批不得过期。"""
    fails: list[Failure] = []
    if baseline.total < current_total:
        fails.append(Failure("A3 基线完整性", "(全仓)", "(总量)",
                             baseline.total, current_total,
                             "基线低于实测：存在未登记的新违规"))
    growth = baseline.data.get("growth_approved")
    if growth:
        expires = growth.get("expires")
        if expires:
            try:
                exp = datetime.strptime(str(expires), "%Y-%m-%d").date()
            except ValueError:
                fails.append(Failure("A3 基线完整性", "(全仓)", "(审批)",
                                     0, 0, f"growth_approved.expires 非法：{expires}"))
            else:
                if exp < date.today():
                    fails.append(Failure("A3 基线完整性", "(全仓)", "(审批)",
                                         0, 0,
                                         f"基线增长审批已于 {expires} 到期，须复审或偿还"))
    return fails


def evaluate(viols: list[Violation], baseline: Baseline,
             added: dict[str, set[int]] | None) -> tuple[list[Failure], dict[str, Any]]:
    """执行全部断言，返回 (失败列表, 统计)。"""
    current = measure(viols)
    quotas = baseline.quotas
    a1 = check_a1_net_growth(current, quotas)
    a2 = check_a2_added_lines(viols, quotas, added) if added is not None else []
    a3 = check_a3_baseline_integrity(baseline, len(viols))
    stats = {
        "total": len(viols),
        "baseline_total": baseline.total,
        "files": len({v.file for v in viols}),
        "rules": len({v.code for v in viols}),
        "added_files": len(added or {}),
        "on_added_lines": sum(1 for v in viols
                              if added and added.get(v.file)
                              and v.row in added[v.file]),
        "a1": len(a1), "a2": len(a2), "a3": len(a3),
    }
    return a1 + a2 + a3, stats


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_check(args: argparse.Namespace) -> int:
    baseline = Baseline.load(Path(args.baseline))
    viols = run_ruff(args.ruff_cmd or None)

    added: dict[str, set[int]] | None = None
    used_ref = ""
    if not args.no_diff:
        added, used_ref = git_added_lines(args.base)
        if not added:
            print(f"⚠️  无法取得 diff（base={args.base} 不可用）→ 跳过 A2 新增行断言")
            added = None

    fails, stats = evaluate(viols, baseline, added)

    print("=" * 72)
    print("增量阻断门禁 · ci_violation_gate")
    print("=" * 72)
    print(f"实测违规 {stats['total']} 条（{stats['files']} 文件 / {stats['rules']} 规则）"
          f" · 基线 {stats['baseline_total']} 条")
    if added is not None:
        print(f"diff 基准 {used_ref}：{stats['added_files']} 个文件有新增行，"
              f"其中 {stats['on_added_lines']} 条违规落在新增行上")
    print(f"断言：A1 净增长 {stats['a1']} 失败 · A2 新增行 {stats['a2']} 失败 · "
          f"A3 基线完整性 {stats['a3']} 失败")
    base_ver = str(baseline.data.get("ruff_version", ""))
    cur_ver = _ruff_version(args.ruff_cmd or None)
    if base_ver and cur_ver and base_ver != cur_ver:
        print(f"⚠️  ruff 版本与基线不一致（基线 {base_ver} / 当前 {cur_ver}）："
              f"规则集可能变化，基线需重新测量（make lint-baseline）")
    print()
    if not fails:
        print("✅ 通过：无新增违规，存量基线未被抬高。")
        return 0

    print(f"❌ 失败 {len(fails)} 项：")
    for f in fails[:40]:
        print("  " + f.line())
    if len(fails) > 40:
        print(f"  ... 另有 {len(fails) - 40} 项")
    print()
    print("处理方式：")
    print("  ① 优先就地修掉新增违规（多数是分钟级）")
    print("  ② 确属历史遗留且本次无法修：把违规挪到**未改动的行**，或执行")
    print("     `make lint-baseline` 按当前实测收紧基线（只降不升）")
    print("  ③ 需要临时豁免：`update --allow-growth --reason \"...\"` 会写入 "
          f"{GROWTH_APPROVAL_MAX_DAYS} 天有效期的增长审批，到期未还则 CI 失败")
    return 1


def cmd_update(args: argparse.Namespace) -> int:
    path = Path(args.baseline)
    old = Baseline.load(path)
    viols = run_ruff(args.ruff_cmd or None)
    current = measure(viols)
    old_quotas = old.quotas

    grown = [(k, old_quotas.get(k, 0), cnt) for k, cnt in sorted(current.items())
             if cnt > old_quotas.get(k, 0)]
    dropped = [(k, old_quotas.get(k, 0), cnt) for k, cnt in sorted(old_quotas.items())
               if cnt > current.get(k, 0)]

    new = build_baseline(viols, ruff_version=_ruff_version(args.ruff_cmd or None),
                         measured_at=_git_head(), existing=old.data)

    bootstrap = not path.exists()
    if bootstrap:
        print(f"ℹ️  基线文件不存在 → 按当前实测创建初始基线（{len(current)} 格，"
              f"{len(viols)} 条违规）")
    elif grown and not args.allow_growth:
        print("❌ 拒绝更新基线：以下 (文件,规则) 违规数上升，基线只降不升。")
        for (f, r), b, c in grown[:20]:
            print(f"   {f} · {r}: {b} → {c}")
        print()
        print("处理：① 先修掉新增违规；② 或显式 `--allow-growth --reason \"...\"` "
              f"写入 {GROWTH_APPROVAL_MAX_DAYS} 天有效期的增长审批（CI 到期会失败）。")
        return 1

    if grown and not bootstrap:
        if not args.reason:
            print("❌ --allow-growth 必须同时给 --reason")
            return 2
        new["growth_approved"] = {
            "reason": args.reason,
            "approved_at": date.today().isoformat(),
            "expires": (date.today() + timedelta(days=GROWTH_APPROVAL_MAX_DAYS)).isoformat(),
            "grew": [{"file": f, "rule": r, "from": b, "to": c} for (f, r), b, c in grown],
        }
        print(f"⚠️ 已写入基线增长审批（{GROWTH_APPROVAL_MAX_DAYS} 天内须偿还）：{args.reason}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(f"✅ 基线已更新：{path}")
    print(f"   总量 {old.total} → {new['total']}"
          f"（收紧 {len(dropped)} 格，放宽 {len(grown)} 格）")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    b = Baseline.load(Path(args.baseline))
    d = b.data
    print(f"基线 {args.baseline}")
    print(f"  version={d.get('version')} created={d.get('created')} "
          f"updated={d.get('updated')}")
    print(f"  measured_at={d.get('measured_at', '-')} ruff={d.get('ruff_version', '-')}")
    print(f"  total={d.get('total')} files={d.get('files')}")
    top = sorted(d.get("by_file_rule", {}).items(),
                 key=lambda kv: -sum(kv[1].values()))[:10]
    print("  违规最多的文件：")
    for f, rules in top:
        print(f"    {sum(rules.values()):>4}  {f}  {rules}")
    growth = d.get("growth_approved")
    if growth:
        print(f"  ⚠️ 基线增长审批：{growth.get('reason')}"
              f"（{growth.get('approved_at')} → {growth.get('expires')}）")
    return 0


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, cwd=str(ROOT))
        return out.stdout.strip()
    except Exception:
        return ""


def _ruff_version(ruff_cmd: list[str] | None = None) -> str:
    cmd = list(ruff_cmd or [sys.executable, "-m", "ruff"])
    try:
        out = subprocess.run([*cmd, "--version"], capture_output=True, text=True,
                             cwd=str(ROOT))
        return out.stdout.strip()
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI 增量阻断门禁")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="执行断言（CI 用）")
    p_check.add_argument("--base", default="origin/main", help="diff 基准 ref")
    p_check.add_argument("--no-diff", action="store_true", help="只跑基线断言，跳过 A2")
    p_check.add_argument("--baseline", default=str(BASELINE_PATH))
    p_check.add_argument("--ruff-cmd", nargs="*", help="自定义 ruff 调用")
    p_check.set_defaults(func=cmd_check)

    p_update = sub.add_parser("update", help="按实测收紧基线（只降不升）")
    p_update.add_argument("--baseline", default=str(BASELINE_PATH))
    p_update.add_argument("--allow-growth", action="store_true", help="允许基线上升（需 --reason）")
    p_update.add_argument("--reason", default="")
    p_update.add_argument("--ruff-cmd", nargs="*")
    p_update.set_defaults(func=cmd_update)

    p_show = sub.add_parser("show", help="查看基线概览")
    p_show.add_argument("--baseline", default=str(BASELINE_PATH))
    p_show.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
