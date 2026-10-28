#!/usr/bin/env python3
"""v2 治理端到端验证脚本（Step 6）。

【目的】
后台下载 + 迁移执行完成后，一键验证：
1. schema 合规率（v2 date32 + 7 列 daily / 5 列 adj）
2. 关键数据集文件数（daily / adj / hfq / min15）
3. predict.py 抽样 5 只跨板块股票 + 1 只 ETF
4. canary 健康检查
5. 输出 Markdown / JSON 验证报告

【用法】
    # 完整验证（推荐）
    venv/bin/python scripts/verify_v2.py

    # 仅 schema 与文件数（快速）
    venv/bin/python scripts/verify_v2.py --skip-predict

    # 机读 JSON
    venv/bin/python scripts/verify_v2.py --json

    # 自定义抽样代码
    venv/bin/python scripts/verify_v2.py --codes sh600519,sz000001,bj920047,sh510300,sh113001
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from _common import adj_factor_dir, daily_dir, hfq_factor_dir, minute_dir  # noqa: E402

DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
HFQ_DIR = hfq_factor_dir()
MIN15_DIR = minute_dir("min15")
MIN5_DIR = minute_dir("min5")
CHECK_SCHEMA = ROOT / "scripts" / "check_schema.py"
PREDICT_SCRIPT = ROOT / "scripts" / "predict.py"
CANARY_SCRIPT = ROOT / "scripts" / "canary_health_check.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify_v2")

DEFAULT_CODES = ["sh600519", "sz000001", "sz300750", "sh688981", "bj920047", "sh510300"]


def run_subprocess(cmd: list[str], timeout: int = 300) -> dict:
    """运行子命令并返回结果摘要。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "status": "ok" if r.returncode == 0 else "fail",
            "exit_code": r.returncode,
            "stdout_tail": r.stdout[-800:] if r.stdout else "",
            "stderr_tail": r.stderr[-800:] if r.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stdout_tail": "", "stderr_tail": f"timeout {timeout}s"}
    except Exception as e:
        return {"status": "error", "stderr_tail": str(e)[:200]}


def check_schema() -> dict:
    """调用 check_schema.py --json 获取 v2 合规率。"""
    return run_subprocess(
        [sys.executable, str(CHECK_SCHEMA), "--json"], timeout=180,
    )


def file_count(d: Path) -> dict:
    """目录文件统计（含子目录）。"""
    if not d.exists():
        return {"exists": False, "files": 0}
    files = [p for p in d.glob("*.parquet") if ".bak" not in p.name]
    return {
        "exists": True,
        "files": len(files),
        "size_mb": round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1),
    }


def run_predict_sample(codes: list[str]) -> dict:
    """抽样跑 predict.py（不写 latest 文件）。"""
    cmd = [
        sys.executable, str(PREDICT_SCRIPT),
        "--codes", ",".join(codes),
        "--top", "5",
    ]
    log.info("predict cmd: %s", " ".join(cmd))
    return run_subprocess(cmd, timeout=300)


def run_canary() -> dict:
    """跑 canary 健康检查（6 项）。"""
    return run_subprocess(
        [sys.executable, str(CANARY_SCRIPT), "--quiet"], timeout=120,
    )


def build_report(args: argparse.Namespace) -> dict:
    log.warning("=" * 60)
    log.warning("v2 治理端到端验证（Step 6）")
    log.warning("=" * 60)

    log.info("[1/4] schema 合规率扫描…")
    schema = check_schema()

    log.info("[2/4] 文件数统计…")
    file_counts = {
        "daily": file_count(DAILY_DIR),
        "adj_factor": file_count(ADJ_DIR),
        "hfq_factor": file_count(HFQ_DIR),
        "min15": file_count(MIN15_DIR),
        "min5": file_count(MIN5_DIR),
    }

    log.info("[3/4] predict.py 抽样…")
    if args.skip_predict:
        predict = {"status": "skipped"}
    else:
        predict = run_predict_sample(args.codes)

    log.info("[4/4] canary 健康检查…")
    if args.skip_canary:
        canary = {"status": "skipped"}
    else:
        canary = run_canary()

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "schema_check": schema,
        "file_counts": file_counts,
        "predict_sample": predict,
        "canary": canary,
        "sample_codes": args.codes,
    }


def print_text_report(report: dict) -> None:
    print()
    print("=" * 60)
    print("v2 治理验证报告")
    print("=" * 60)
    print(f"时间:    {report['generated_at']}")
    print(f"抽样代码: {report['sample_codes']}")
    print()

    # Schema
    sc = report["schema_check"]
    print("【1. Schema 合规率】")
    if sc.get("status") == "ok" and sc.get("stdout_tail"):
        try:
            d = json.loads(sc["stdout_tail"])
            for k in ("daily", "adj_factor"):
                rep = d.get(k, {})
                print(f"  {k:12s}: 文件={rep.get('total_files')} 合规率={rep.get('compliant_pct')}% "
                      f"date分布={rep.get('date_type_distribution')}")
        except Exception:
            print(f"  (解析失败): {sc['stdout_tail'][:200]}")
    else:
        print(f"  status={sc.get('status')} stderr={sc.get('stderr_tail','')[:200]}")
    print()

    # File counts
    fc = report["file_counts"]
    print("【2. 文件数】")
    for k, v in fc.items():
        if v.get("exists"):
            print(f"  {k:12s}: {v['files']:>6} files ({v['size_mb']:>6.1f} MB)")
        else:
            print(f"  {k:12s}: (目录不存在)")
    print()

    # Predict
    pr = report["predict_sample"]
    print("【3. predict.py 抽样】")
    print(f"  status: {pr.get('status')}")
    if pr.get("status") == "ok":
        for line in pr.get("stdout_tail", "").splitlines()[-10:]:
            print(f"    {line}")
    elif pr.get("stderr_tail"):
        print(f"  stderr: {pr['stderr_tail'][:300]}")
    print()

    # Canary
    cn = report["canary"]
    print("【4. canary 健康检查】")
    print(f"  status: {cn.get('status')}")
    if cn.get("status") == "ok":
        for line in cn.get("stdout_tail", "").splitlines()[-15:]:
            print(f"    {line}")
    elif cn.get("stderr_tail"):
        print(f"  stderr: {cn['stderr_tail'][:300]}")
    print()

    print("=" * 60)
    # 总结
    parts = []
    if sc.get("status") == "ok":
        parts.append("✅ Schema OK")
    else:
        parts.append("❌ Schema FAIL")
    if pr.get("status") in ("ok", "skipped"):
        parts.append("✅ Predict OK" if pr.get("status") == "ok" else "⏭ Predict skipped")
    else:
        parts.append("❌ Predict FAIL")
    if cn.get("status") in ("ok", "skipped"):
        parts.append("✅ Canary OK" if cn.get("status") == "ok" else "⏭ Canary skipped")
    else:
        parts.append("❌ Canary FAIL")
    print("总体: " + " | ".join(parts))
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description="v2 治理端到端验证")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--skip-predict", action="store_true", help="跳过 predict 抽样")
    ap.add_argument("--skip-canary", action="store_true", help="跳过 canary 健康检查")
    ap.add_argument(
        "--codes",
        default=",".join(DEFAULT_CODES),
        help=f"抽样代码 (默认: {','.join(DEFAULT_CODES)})",
    )
    args = ap.parse_args()

    args.codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    report = build_report(args)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)


if __name__ == "__main__":
    main()
