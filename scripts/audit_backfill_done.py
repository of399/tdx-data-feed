#!/usr/bin/env python
"""done 集与磁盘对账：回填「续跑状态」是否还在说真话。

    venv/bin/python scripts/audit_backfill_done.py            # 看人话摘要
    venv/bin/python scripts/audit_backfill_done.py --json     # 机器读

异常（退出码 1）：done 有记录但磁盘无文件；done 有记录但最近深度审计仍判 extend。
待补积压（extend ∖ done）只报告不算异常——那是回填还没跑完，不是状态说谎。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.backfill_audit import list_daily_symbols, reconcile  # noqa: E402
from tdxfeed.backfill_history import RESUME_PATH, load_done  # noqa: E402
from tdxfeed.paths import daily_dir  # noqa: E402

SHOW = 10


def latest_audit() -> Path | None:
    fs = sorted(glob.glob(str(ROOT / "data" / "audit" / "history_depth_*.json")))
    return Path(fs[-1]) if fs else None


def load_audit_states(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(r["symbol"]): str(r.get("state")) for r in data.get("rows", [])
            if "symbol" in r}


def _show(title: str, symbols, out) -> None:
    if not symbols:
        return
    head = ", ".join(symbols[:SHOW])
    more = f" …(+{len(symbols) - SHOW})" if len(symbols) > SHOW else ""
    print(f"[{title}] {len(symbols)} 只: {head}{more}", file=out, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", default=RESUME_PATH)
    ap.add_argument("--daily", default=str(daily_dir()))
    ap.add_argument("--audit", default=None, help="深度审计 JSON；缺省取 data/audit 下最新")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    audit_path = Path(args.audit) if args.audit else latest_audit()
    if audit_path is not None and not audit_path.exists():
        print(f"[WARN] 审计产物不存在：{audit_path}，跳过深度对账", file=sys.stderr)
        audit_path = None
    states = load_audit_states(audit_path)
    report = reconcile(load_done(args.resume), list_daily_symbols(args.daily), states)
    report["audit"] = str(audit_path) if audit_path else None

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(f"done {report['done']} 只 / 磁盘 {report['files']} 只"
          f" / 审计 {audit_path.name if audit_path else '（无，跳过深度对账）'}", flush=True)
    if not states:
        print("[提示] 没有可用的深度审计产物：done_shallow 与 pending_backfill 未检查",
              flush=True)
    _show("异常·done 有记录但磁盘无文件", report["done_no_file"], sys.stdout)
    _show("异常·done 有记录但审计仍判 extend", report["done_shallow"], sys.stdout)
    _show("待补·extend 且未 done", report["pending_backfill"], sys.stdout)
    _show("提示·done 但审计不再要求补", report["done_not_needed"], sys.stdout)
    if report["ok"]:
        print("对账通过：没有状态说谎", flush=True)
        return 0
    print(f"对账失败：{len(report['anomalies'])} 只异常", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
