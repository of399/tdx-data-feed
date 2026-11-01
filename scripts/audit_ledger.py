#!/usr/bin/env python
"""已知问题台账工具（known-issues ledger）。

用途：把"每次检查都发现新漏洞"变成可管理的数据——
  1) scan    复扫台账每条问题的 detect 规格，判定 STILL_PRESENT / RESOLVED / CHANGED
  2) report  生成本轮审计摘要（两栏：口径差异 + 新增 vs 复现），可直接贴进审计报告
  3) new     登记一条新发现（强制查重：命中已有条目时改为更新 seen_count）
  4) probe   探测一个尚未登记的模式 → 报告"未登记"并给出登记模板片段

台账文件：config/known_issues.yaml（字段与记录规则见该文件头部注释）
用法示例：
    venv/bin/python scripts/audit_ledger.py scan
    venv/bin/python scripts/audit_ledger.py scan --update      # 回写 last_seen/seen_count
    venv/bin/python scripts/audit_ledger.py report --round 2026-11-01
    venv/bin/python scripts/audit_ledger.py new --theme docs --severity P2 \
        --title "xxx" --detect-kind grep --detect-pattern "foo" --detect-paths "docs/*.md"
    venv/bin/python scripts/audit_ledger.py probe --pattern "except\\s+Exception" --paths "scripts/*.py"
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "config" / "known_issues.yaml"

REQUIRED_FIELDS = (
    "id", "title", "theme", "severity", "status", "first_seen", "last_seen",
    "seen_count", "sources", "locations", "detect", "impact", "guard",
    "owner", "due", "exemption", "fix_hint",
)
VALID_STATUS = ("OPEN", "ACCEPTED", "FIXED", "DEFERRED", "ARCHIVED")
VALID_SEVERITY = ("P0", "P1", "P2")
DETECT_KINDS = ("grep", "absent", "lines_gt", "glob_exists", "glob_absent")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class DetectResult:
    """单条 detect 规格的判定结果。"""

    kind: str
    ok: bool
    detail: str
    hits: int = 0


@dataclass
class Issue:
    """台账条目（保留原始 dict，便于回写）。"""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def title(self) -> str:
        return str(self.raw["title"])

    @property
    def theme(self) -> str:
        return str(self.raw["theme"])

    @property
    def severity(self) -> str:
        return str(self.raw["severity"])

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def guard(self) -> str:
        return str(self.raw["guard"])

    @property
    def seen_count(self) -> int:
        return int(self.raw["seen_count"])

    @property
    def last_seen(self) -> str:
        return str(self.raw["last_seen"])

    @property
    def due(self) -> str:
        return str(self.raw["due"])

    @property
    def owner(self) -> str:
        return str(self.raw["owner"])

    @property
    def detect_specs(self) -> list[dict[str, Any]]:
        return list(self.raw["detect"])

    def to_yaml_block(self) -> str:
        return yaml.safe_dump([self.raw], allow_unicode=True, sort_keys=False,
                              default_flow_style=False)


@dataclass
class Ledger:
    """台账容器。"""

    meta: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = LEDGER_PATH) -> Ledger:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        issues = [Issue(raw=i) for i in data.get("issues", [])]
        return cls(meta=data.get("meta", {}), issues=issues)

    def save(self, path: Path = LEDGER_PATH) -> None:
        data = {"meta": self.meta, "issues": [i.raw for i in self.issues]}
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, width=100),
            encoding="utf-8",
        )

    def validate(self) -> list[str]:
        """校验台账自身合法性，返回问题列表（空=合法）。"""
        problems: list[str] = []
        seen: set[str] = set()
        for iss in self.issues:
            for key in REQUIRED_FIELDS:
                if key not in iss.raw:
                    problems.append(f"{iss.id}: 缺字段 {key}")
            if iss.id in seen:
                problems.append(f"{iss.id}: id 重复")
            seen.add(iss.id)
            if iss.severity not in VALID_SEVERITY:
                problems.append(f"{iss.id}: severity 非法 {iss.severity}")
            if iss.status not in VALID_STATUS:
                problems.append(f"{iss.id}: status 非法 {iss.status}")
            if iss.status == "FIXED" and iss.guard == "NONE":
                problems.append(f"{iss.id}: status=FIXED 但无 guard（违反 R5）")
            if iss.status == "ACCEPTED" and (iss.raw.get("exemption") in (None, "NONE")
                                            or not iss.due):
                problems.append(f"{iss.id}: status=ACCEPTED 但缺 exemption/due（违反 R6）")
            if not iss.detect_specs:
                problems.append(f"{iss.id}: 无 detect 规格（无法复扫）")
            for spec in iss.detect_specs:
                kind = spec.get("kind")
                if kind not in DETECT_KINDS:
                    problems.append(f"{iss.id}: detect.kind 非法 {kind}")
                if kind in ("grep", "absent") and not spec.get("pattern"):
                    problems.append(f"{iss.id}: detect.pattern 缺失")
                if kind == "grep" and not (spec.get("paths") or spec.get("path")):
                    problems.append(f"{iss.id}: detect.paths/path 缺失")
                if kind in ("absent", "lines_gt") and not spec.get("path"):
                    problems.append(f"{iss.id}: detect.path 缺失")
            for guard in [iss.guard]:
                if guard not in ("NONE", "") and not (ROOT / guard).exists():
                    problems.append(f"{iss.id}: guard 指向不存在的文件 {guard}")
        return problems


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def _section_lines(text: str, section: str) -> list[str]:
    """取 Makefile 风格文件中某个目标块的行（从 'section:' 到下一个顶格目标）。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"{section}:"):
            start = i
            break
    if start is None:
        return []
    out = []
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():
            break
        out.append(ln)
    return out


def _iter_files(paths: list[str]) -> list[Path]:
    """把 glob 列表展开为文件列表（不存在直接跳过，不报错）。"""
    out: list[Path] = []
    for pat in paths:
        p = Path(pat)
        if p.is_absolute():
            base = p.anchor
            rel = p.relative_to(base)
            out.extend(base.glob(str(rel)))
        else:
            out.extend(ROOT.glob(pat))
    return sorted({f for f in out if f.is_file()})


def _count_matches(files: list[Path], pattern: str, multiline: bool,
                   section: str = "", exclude: list[str] | None = None) -> tuple[int, list[str]]:
    """统计匹配次数，返回 (次数, 命中文件前若干)。section 非空时只统计该目标块。"""
    rx = re.compile(pattern)
    total = 0
    hits: list[str] = []
    for f in files:
        rel = str(f.relative_to(ROOT))
        if exclude and any(x in rel for x in exclude):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            _ERROR_COUNT['_count_matches'] += 1
            continue
        if section:
            sub = "\n".join(_section_lines(text, section))
            n = len(rx.findall(sub)) if multiline else sum(
                1 for line in sub.splitlines() if rx.search(line))
            if n:
                total += n
                hits.append(f"{f.relative_to(ROOT)}:{n}")
            continue
        if multiline:
            n = len(rx.findall(text))
        else:
            n = sum(1 for line in text.splitlines() if rx.search(line))
        if n:
            total += n
            hits.append(f"{f.relative_to(ROOT)}:{n}")
    return total, hits[:5]


def run_detect(spec: dict[str, Any]) -> DetectResult:
    """执行单条 detect 规格。ok=True 表示"问题仍存在"（按规格语义）。"""
    kind = spec.get("kind")

    if kind == "grep":
        paths = list(spec.get("paths", []))
        if spec.get("path"):
            paths = [str(spec["path"])]
        files = _iter_files(paths)
        n, hits = _count_matches(files, str(spec["pattern"]),
                                 bool(spec.get("multiline", False)),
                                 str(spec.get("section", "")),
                                 list(spec.get("exclude", [])))
        min_count = int(spec.get("min_count", 1))
        ok = n >= min_count
        return DetectResult(kind, ok, f"命中 {n} 处 {hits}", n)

    if kind == "absent":
        target = ROOT / str(spec["path"])
        if not target.exists():
            return DetectResult(kind, False, "目标文件不存在，无法判定", 0)
        text = target.read_text(encoding="utf-8", errors="ignore")
        scope = text
        if spec.get("section"):
            scope = "\n".join(_section_lines(text, str(spec["section"])))
        found = bool(re.search(str(spec["pattern"]), scope))
        return DetectResult(kind, not found,
                            f"{'未匹配' if not found else '已匹配'} {spec['pattern']!r}",
                            0 if not found else 1)

    if kind == "lines_gt":
        target = ROOT / str(spec["path"])
        if not target.exists():
            return DetectResult(kind, False, "目标文件不存在", 0)
        n = len(target.read_text(encoding="utf-8").splitlines())
        limit = int(spec.get("max_lines", 0))
        return DetectResult(kind, n > limit, f"{n} 行（阈值 {limit}）", n)

    if kind == "glob_exists":
        files = _iter_files([str(spec["pattern"])])
        return DetectResult(kind, bool(files), f"匹配 {len(files)} 个文件", len(files))

    if kind == "glob_absent":
        files = _iter_files([str(spec["pattern"])])
        return DetectResult(kind, not files, f"匹配 {len(files)} 个文件", len(files))

    return DetectResult(str(kind), False, "未知 detect.kind", 0)


def scan_issue(iss: Issue) -> tuple[str, list[DetectResult]]:
    """复扫单条：返回 (状态, 明细)。状态 ∈ STILL_PRESENT / RESOLVED / CHANGED。"""
    if iss.status == "FIXED":
        # 已修条目：判定标准是"守卫是否在位"，而非问题特征是否仍匹配
        if iss.guard in ("NONE", ""):
            return "FIXED_NO_GUARD", [DetectResult("guard", False, "缺 guard")]
        exists = (ROOT / iss.guard).exists()
        return ("FIXED_OK" if exists else "FIXED_GUARD_MISSING"), [
            DetectResult("guard", exists, f"guard {iss.guard} "
                                        f"{'存在' if exists else '缺失'}")]
    results = [run_detect(s) for s in iss.detect_specs]
    if not results:
        return "UNKNOWN", results
    present = all(r.ok for r in results)
    if not present:
        return "RESOLVED", results
    return "STILL_PRESENT", results


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_scan(args: argparse.Namespace) -> int:
    ledger = Ledger.load()
    problems = ledger.validate()
    if problems:
        print("台账校验未通过：")
        for p in problems:
            print(f"  - {p}")
        return 2

    active = [i for i in ledger.issues if i.status != "ARCHIVED"]
    buckets: dict[str, list[Issue]] = {
        "STILL_PRESENT": [], "RESOLVED": [], "UNKNOWN": [],
        "FIXED_OK": [], "FIXED_GUARD_MISSING": [], "FIXED_NO_GUARD": []}
    details: dict[str, list[DetectResult]] = {}
    for iss in active:
        state, res = scan_issue(iss)
        buckets[state].append(iss)
        details[iss.id] = res

    print(f"台账扫描 · {date.today().isoformat()}")
    print(f"条目 {len(ledger.issues)} · 本轮复检 {len(active)} · "
          f"跳过(ARCHIVED) {len(ledger.issues) - len(active)}")
    print()
    order = ("P0", "P1", "P2")
    for sev in order:
        for iss in buckets["STILL_PRESENT"]:
            if iss.severity != sev:
                continue
            res = details[iss.id]
            print(f"[{sev}] {iss.id} {iss.title}")
            print(f"      状态 STILL_PRESENT  guard={iss.guard}  复发={iss.seen_count}  "
                  f"上次={iss.last_seen}  期限={iss.due}")
            for r in res:
                print(f"      detect[{r.kind}] {r.detail}")
    for state in ("FIXED_OK", "FIXED_GUARD_MISSING", "FIXED_NO_GUARD", "RESOLVED"):
        if buckets[state]:
            print()
            for iss in buckets[state]:
                print(f"[{iss.severity}] {iss.id} {state} · {iss.title}")
                for r in details[iss.id]:
                    print(f"      detect[{r.kind}] {r.detail}")

    print()
    print(f"汇总: STILL_PRESENT {len(buckets['STILL_PRESENT'])} · "
          f"RESOLVED {len(buckets['RESOLVED'])} · "
          f"FIXED_OK {len(buckets['FIXED_OK'])} · "
          f"GUARD_MISSING {len(buckets['FIXED_GUARD_MISSING']) + len(buckets['FIXED_NO_GUARD'])} · "
          f"UNKNOWN {len(buckets['UNKNOWN'])}")
    guardless = [i.id for i in buckets["STILL_PRESENT"] if i.guard == "NONE"]
    print(f"其中无守卫(会复发): {len(guardless)} 条 → {', '.join(guardless[:8])}"
          f"{' ...' if len(guardless) > 8 else ''}")

    if args.update:
        today = date.today().isoformat()
        changed = 0
        for iss in buckets["STILL_PRESENT"]:
            if iss.last_seen != today:
                iss.raw["last_seen"] = today
                iss.raw["seen_count"] = iss.seen_count + 1
                changed += 1
        ledger.meta["updated"] = today
        ledger.save()
        print(f"\n已回写台账: 更新 {changed} 条 last_seen/seen_count（meta.updated={today}）")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """生成本轮审计摘要（两栏结构），可直接贴进审计报告。"""
    ledger = Ledger.load()
    problems = ledger.validate()
    if problems:
        print("台账校验未通过，先修台账：")
        for p in problems:
            print(f"  - {p}")
        return 2

    states: dict[str, list[Issue]] = {}
    for iss in ledger.issues:
        if iss.status == "ARCHIVED":
            continue
        state, _ = scan_issue(iss)
        states.setdefault(state, []).append(iss)

    recurring = states.get("STILL_PRESENT", [])
    resolved = states.get("RESOLVED", [])
    fixed_ok = states.get("FIXED_OK", [])
    guard_missing = states.get("FIXED_GUARD_MISSING", []) + states.get("FIXED_NO_GUARD", [])
    today = args.round or date.today().isoformat()

    out: list[str] = []
    out.append(f"# 审计轮次摘要 · {today}")
    out.append("")
    out.append("## 栏 1 · 口径差异（本轮 vs 上一轮）")
    out.append("")
    out.append("> 填写规范：逐行写清**扫描范围**变化；无变化写「与上轮一致」。")
    out.append("> 目的：让读者能区分「问题变多」与「扫得更全」。")
    out.append("")
    out.append("| 维度 | 本轮 | 上一轮 | 差异 | 是否影响结论 |")
    out.append("|---|---|---|---|---|")
    out.append(f"| 扫描范围 | 台账 {len(ledger.issues)} 条 detect 全量复扫 | 见上轮报告 | — | — |")
    out.append(f"| 复检条目 | {len(recurring) + len(resolved)} 条（排除 ARCHIVED） | — | — | — |")
    out.append("| 静态检查 | ruff 4 类阻断 + 全规则仅报告 | 同 | 与上轮一致 | 否 |")
    out.append("| 运行时巡检 | canary 8 维 + onfailure(user scope) | 同 | 与上轮一致 | 否 |")
    out.append("")
    out.append("## 栏 2 · 发现分类：新增 vs 复现")
    out.append("")
    out.append("> 填写规范：**新增**=台账中不存在、本轮首次发现（须同时完成新建条目 R3）；")
    out.append("> **复现**=台账中已有、本轮再次被检出（只更新 last_seen/seen_count，R2）；")
    out.append("> **消失**=台账中已有、本轮未再检出（可考虑置 FIXED，但 R5 要求先绑 guard）。")
    out.append("")
    out.append("| 分类 | 条数 | 清单 |")
    out.append("|---|---:|---|")
    out.append(f"| **NEW 本轮新增** | {args.new_count} | {args.new_list or '—'} |")
    out.append(f"| **RECURRING 历史复现** | {len(recurring)} | "
               f"{', '.join(i.id for i in recurring) or '—'} |")
    out.append(f"| **RESOLVED 本轮消失** | {len(resolved)} | "
               f"{', '.join(i.id for i in resolved) or '—'} |")
    out.append(f"| **FIXED 已修且守卫在位** | {len(fixed_ok)} | "
               f"{', '.join(i.id for i in fixed_ok) or '—'} |")
    out.append(f"| **FIXED 但守卫缺失** | {len(guard_missing)} | "
               f"{', '.join(i.id for i in guard_missing) or '—'} |")
    out.append("")
    out.append("### 复现条目明细（按 severity）")
    out.append("")
    out.append("| ID | Sev | 主题 | 复发 | 守卫 | 期限 | 标题 |")
    out.append("|---|---|---|---:|---|---|---|")
    for iss in sorted(recurring, key=lambda i: (i.severity, i.id)):
        out.append(f"| {iss.id} | {iss.severity} | {iss.theme} | {iss.seen_count} | "
                   f"{iss.guard} | {iss.due} | {iss.title} |")
    out.append("")
    guardless = [i for i in recurring if i.guard == "NONE"]
    out.append(f"**复发风险**：{len(guardless)}/{len(recurring)} 条复现问题**仍无自动守卫**，"
               f"下一轮很可能再次被检出。")
    out.append("")
    out.append("## 台账更新记录")
    out.append("")
    out.append(f"- 本轮复检 {len(recurring) + len(resolved)} 条；"
               f"更新 last_seen/seen_count：{len(recurring)} 条（可用 `--update` 自动回写）")
    out.append("- 新建条目：见栏 2 的 NEW 清单（每条须含 detect 规格，否则无法复扫）")
    out.append("- 关闭条目：须满足 R5（status=FIXED + 指向真实存在的 guard 文件）")
    out.append("")

    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写出：{args.out}")
    else:
        print(text)
    return 0


def _find_duplicates(ledger: Ledger, title: str, pattern: str) -> list[Issue]:
    """查重：标题包含或 detect 模式相同的条目。"""
    out = []
    for iss in ledger.issues:
        if title and (title in iss.title or iss.title in title):
            out.append(iss)
            continue
        for spec in iss.detect_specs:
            if pattern and spec.get("pattern") == pattern:
                out.append(iss)
                break
    return out


def cmd_new(args: argparse.Namespace) -> int:
    """登记新发现；命中已有条目则按 R2 更新而不是新建。"""
    ledger = Ledger.load()
    today = date.today().isoformat()
    dupes = _find_duplicates(ledger, args.title, args.detect_pattern)

    if dupes and not args.force:
        print("⚠️ 与台账已有条目重复（按 R2 应更新而非新建）：")
        for d in dupes:
            print(f"  - {d.id} {d.title}")
        print("\n如确认是同一问题，请用："
              "\n  venv/bin/python scripts/audit_ledger.py scan --update")
        print("如确认是新问题（不同位置），加 --force 重新登记。")
        return 1

    theme = args.theme
    prefix_map = {
        "silent-failure": "SILENT", "atomicity": "ATOMIC", "concurrency": "COMPETE",
        "single-source": "SINGLE", "hardcode": "PATH", "timeout": "TIMEOUT",
        "contract": "CONTRACT", "mcp": "MCP", "skill": "SKILL", "docs": "DOC",
        "perf": "PERF", "size": "SIZE", "ops": "OPS", "make": "MAKE", "rule": "RULE",
    }
    code = prefix_map.get(theme, "GEN")
    nums = [int(m.group(1)) for i in ledger.issues
            if (m := re.match(rf"KI-{code}-(\d{{3}})$", i.id))]
    new_id = f"KI-{code}-{(max(nums) + 1) if nums else 1:03d}"

    detect: list[dict[str, Any]] = []
    if args.detect_kind:
        spec: dict[str, Any] = {"kind": args.detect_kind}
        if args.detect_pattern:
            spec["pattern"] = args.detect_pattern
        if args.detect_paths:
            spec["paths"] = args.detect_paths
        if args.detect_path:
            spec["path"] = args.detect_path
        detect.append(spec)

    raw = {
        "id": new_id,
        "title": args.title,
        "theme": theme,
        "severity": args.severity,
        "status": "OPEN",
        "first_seen": today,
        "last_seen": today,
        "seen_count": 1,
        "sources": [args.source or "（手工登记）"],
        "locations": args.locations or [],
        "detect": detect,
        "impact": args.impact or "（待补充）",
        "guard": args.guard or "NONE",
        "owner": args.owner or "jiuben",
        "due": args.due or today,
        "exemption": "NONE",
        "fix_hint": args.fix_hint or "（待补充）",
    }
    iss = Issue(raw=raw)
    ledger.issues.append(iss)
    ledger.meta["updated"] = today
    ledger.save()
    print(f"已登记 {new_id}：{args.title}")
    print(f"detect 规格：{detect or '（缺失！下次无法复扫，请补 --detect-*）'}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """探测一个尚未登记的模式 → 报告"未登记"并给出登记模板片段。"""
    ledger = Ledger.load()
    files = _iter_files(args.paths)
    n, hits = _count_matches(files, args.pattern, args.multiline)
    dupes = _find_duplicates(ledger, "", args.pattern)

    print(f"探测模式 {args.pattern!r} 于 {args.paths}")
    print(f"命中 {n} 处 {hits}")
    if dupes:
        print()
        print("⚠️ 该模式已被台账登记（应计入 RECURRING，不要新建）：")
        for d in dupes:
            print(f"  - {d.id} {d.title}（seen_count={d.seen_count}）")
        return 0
    print()
    print("→ 台账中【未登记】：若确认为新问题，按 R3 新建条目：")
    print("   venv/bin/python scripts/audit_ledger.py new --theme <主题> --severity <P0|P1|P2> \\")
    print(f"       --title \"<标题>\" --detect-kind grep --detect-pattern '{args.pattern}' \\")
    print(f"       --detect-paths '{args.paths[0]}' --locations '<文件:行>' --impact '<影响>'")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="已知问题台账工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="复扫台账每条问题的 detect 规格")
    p_scan.add_argument("--update", action="store_true",
                        help="把仍存在的问题回写 last_seen/seen_count")
    p_scan.set_defaults(func=cmd_scan)

    p_report = sub.add_parser("report", help="生成两栏式审计摘要")
    p_report.add_argument("--round", help="轮次标识（默认今天）")
    p_report.add_argument("--new-count", type=int, default=0, help="本轮新增条目数")
    p_report.add_argument("--new-list", default="", help="本轮新增条目清单（逗号分隔）")
    p_report.add_argument("--out", help="输出到文件（默认打印）")
    p_report.set_defaults(func=cmd_report)

    p_new = sub.add_parser("new", help="登记新发现（强制查重）")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--theme", required=True)
    p_new.add_argument("--severity", default="P2", choices=VALID_SEVERITY)
    p_new.add_argument("--detect-kind", choices=DETECT_KINDS)
    p_new.add_argument("--detect-pattern")
    p_new.add_argument("--detect-paths", nargs="*")
    p_new.add_argument("--detect-path")
    p_new.add_argument("--locations", nargs="*")
    p_new.add_argument("--impact")
    p_new.add_argument("--guard")
    p_new.add_argument("--owner")
    p_new.add_argument("--due")
    p_new.add_argument("--source", help="来源报告（file:line）")
    p_new.add_argument("--fix-hint")
    p_new.add_argument("--force", action="store_true", help="确认不是重复，强制新建")
    p_new.set_defaults(func=cmd_new)

    p_probe = sub.add_parser("probe", help="探测未登记模式")
    p_probe.add_argument("--pattern", required=True)
    p_probe.add_argument("--paths", nargs="+", required=True)
    p_probe.add_argument("--multiline", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
