#!/usr/bin/env python3
"""scan_obsidian_vault.py · Obsidian Vault calc 块批量扫描器

扫描指定目录下所有 .md 文件，提取 ```calc ... ``` 块（D3.2 格式），
并通过 math-sympy-http (8002) 验证每个 step 的可达性（D3.3 verify）。

用法：
  # 扫描 Obsidian vault 目录
  python v5/scripts/scan_obsidian_vault.py --vault /home/jiuben/StockVault

  # 扫描 v5 规划文档
  python v5/scripts/scan_obsidian_vault.py --vault /home/jiuben/CodeBuddy/20260916194646

  # 扫描 sample 目录（默认）
  python v5/scripts/scan_obsidian_vault.py

  # 仅列出含 calc 块的文件 + JSON 输出
  python v5/scripts/scan_obsidian_vault.py --vault <path> --json

  # 控制并发
  python v5/scripts/scan_obsidian_vault.py --vault <path> --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

MATH_HTTP_BASE = "http://127.0.0.1:8002"
DEFAULT_VAULT = "/home/jiuben/CodeBuddy/20260916194646/.demo-calc"


def find_md_files(vault: Path, recursive: bool = True) -> list[Path]:
    """收集所有 .md 文件"""
    if recursive:
        return sorted(vault.rglob("*.md"))
    return sorted(vault.glob("*.md"))


async def scan_one(client: httpx.AsyncClient, md_file: Path, vault: Path, verify: bool) -> dict:
    """扫描单个 .md 文件：调 math_step_extract"""
    try:
        content = md_file.read_text(encoding="utf-8")
    except Exception as e:
        return {"path": str(md_file), "error": f"read failed: {e}"}

    try:
        r = await client.post(
            "/tools/math_step_extract",
            json={
                "markdown": content,
                "source_path": str(md_file),
                "verify": verify,
                "var": "x",
            },
        )
        r.raise_for_status()
        d = r.json()
        return {
            "path": str(md_file),
            "rel_path": str(md_file.relative_to(vault)),
            "total_blocks": d.get("total_blocks", 0),
            "valid_steps": d.get("valid_steps", 0),
            "invalid_blocks": d.get("invalid_blocks", 0),
            "verification": d.get("verification"),
            "steps": [
                {
                    "start": s["start"],
                    "rule": s["rule"],
                    "target": s["target"],
                    "line_no": s["line_no"],
                }
                for s in d.get("steps", [])
            ],
            "errors": [
                {"line_no": e.get("line_no"), "error": e.get("error", "")[:100]}
                for e in d.get("errors", [])
            ],
        }
    except httpx.ConnectError as e:
        return {"path": str(md_file), "error": f"math-sympy-http 不可达: {e}"}
    except httpx.HTTPStatusError as e:
        return {
            "path": str(md_file),
            "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
        }


async def scan_vault(
    vault: Path,
    recursive: bool,
    verify: bool,
    concurrency: int,
    base_url: str = MATH_HTTP_BASE,
) -> dict:
    """并发扫描整个 vault"""
    md_files = find_md_files(vault, recursive)
    if not md_files:
        return {
            "vault_path": str(vault),
            "files_scanned": 0,
            "message": "目录下无 .md 文件",
            "files": [],
        }

    semaphore = asyncio.Semaphore(concurrency)
    t0 = time.time()

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:

        async def _scan(md_file: Path) -> dict:
            async with semaphore:
                return await scan_one(client, md_file, vault, verify)

        results = await asyncio.gather(*[_scan(f) for f in md_files])

    # 汇总
    files_with_calc = sum(1 for r in results if r.get("total_blocks", 0) > 0)
    total_blocks = sum(r.get("total_blocks", 0) for r in results)
    total_valid = sum(r.get("valid_steps", 0) for r in results)
    total_invalid = sum(r.get("invalid_blocks", 0) for r in results)
    verified = sum(
        r.get("verification", {}).get("verified", 0) for r in results if r.get("verification")
    )
    verifiable = sum(
        r.get("verification", {}).get("total", 0) for r in results if r.get("verification")
    )
    ratio = verified / verifiable if verifiable else 1.0

    return {
        "vault_path": str(vault),
        "recursive": recursive,
        "verify": verify,
        "files_scanned": len(md_files),
        "files_with_calc": files_with_calc,
        "total_calc_blocks": total_blocks,
        "total_valid_steps": total_valid,
        "total_invalid_blocks": total_invalid,
        "verified_steps": verified,
        "verification_ratio": round(ratio, 3),
        "elapsed_s": round(time.time() - t0, 3),
        "files": sorted(results, key=lambda r: -r.get("total_blocks", 0)),
    }


def print_text_report(report: dict) -> None:
    """人类可读报告"""
    print("=" * 70)
    print("Obsidian Vault calc 块扫描报告")
    print("=" * 70)
    print(f"vault:            {report['vault_path']}")
    print(f"递归:             {report.get('recursive', True)}")
    print(f"verify:           {report.get('verify', False)}")
    print(f"扫描 .md 文件:    {report['files_scanned']}")
    print(f"含 calc 块的文件: {report['files_with_calc']}")
    print(f"calc 块总数:      {report['total_calc_blocks']}")
    print(f"  ├ valid steps:  {report['total_valid_steps']}")
    print(f"  └ invalid:      {report['total_invalid_blocks']}")
    if report.get("verify"):
        print(
            f"验证:             {report['verified_steps']}/{report['verified_steps'] + (report['total_valid_steps'] - report['verified_steps'])} ({report['verification_ratio'] * 100:.1f}%)"
        )
    print(f"耗时:             {report['elapsed_s']}s")
    print()
    print("-" * 70)
    print(f"{'文件':<50} {'blocks':>7} {'valid':>6} {'invalid':>8}")
    print("-" * 70)
    for f in report["files"]:
        rel = f.get("rel_path") or f.get("path", "?")
        if len(rel) > 48:
            rel = "..." + rel[-45:]
        blocks = f.get("total_blocks", 0)
        valid = f.get("valid_steps", 0)
        invalid = f.get("invalid_blocks", 0)
        err = f.get("error")
        if err:
            print(f"{rel:<50}  ERR: {err[:50]}")
        elif blocks > 0:
            print(f"{rel:<50} {blocks:>7} {valid:>6} {invalid:>8}")
        else:
            print(f"{rel:<50} {blocks:>7} {valid:>6} {invalid:>8}  (无 calc)")
    print()

    # 显示每个 valid step 详情（最多 5 个文件）
    files_with_steps = [f for f in report["files"] if f.get("steps")]
    if files_with_steps:
        print("-" * 70)
        print("Step 详情（最多 5 个文件，每个前 3 个 step）")
        print("-" * 70)
        for f in files_with_steps[:5]:
            rel = f.get("rel_path") or f.get("path", "?")
            print(f"\n📄 {rel}")
            for s in f["steps"][:3]:
                verif = ""
                if f.get("verification"):
                    # verify 返回的是 verified/total，无法精确到每个 step
                    verif = "  (verified)" if f["verification"].get("verified_ratio", 0) > 0 else ""
                print(
                    f"   L{s['line_no']:>3}  {s['start'][:35]}  ---[{s['rule']}]--->  {s['target'][:35]}{verif}"
                )
            if len(f["steps"]) > 3:
                print(f"   ... ({len(f['steps']) - 3} more)")


def main():
    parser = argparse.ArgumentParser(
        description="Obsidian Vault calc 块扫描器（D3.2 + D3.3）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--vault",
        default=DEFAULT_VAULT,
        help=f"Vault 目录路径（默认: {DEFAULT_VAULT}）",
    )
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="不递归子目录",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="不验证 step 可达性（更快）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="并发扫描数（默认 4）",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出 JSON 格式",
    )
    parser.add_argument(
        "--math-http",
        default=MATH_HTTP_BASE,
        help=f"math-sympy-http 地址（默认: {MATH_HTTP_BASE}）",
    )
    args = parser.parse_args()

    vault = Path(args.vault)
    if not vault.exists():
        print(f"✗ vault 路径不存在: {vault}", file=sys.stderr)
        sys.exit(1)
    if not vault.is_dir():
        print(f"✗ 不是目录: {vault}", file=sys.stderr)
        sys.exit(1)

    # 扫描 vault（base_url 作为参数传入，避免全局变量）
    report = asyncio.run(
        scan_vault(
            vault,
            args.recursive,
            args.verify,
            args.concurrency,
            base_url=args.math_http,
        )
    )

    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)

    # exit code：有 invalid 块 → 2
    if report.get("total_invalid_blocks", 0) > 0:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
