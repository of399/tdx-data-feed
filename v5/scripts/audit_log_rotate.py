#!/usr/bin/env python3
"""audit_log_rotate.py · M3 W6 audit log 增长管理

功能：
1. TTL 清理：删除 fallback-chain.jsonl 中超过 N 天的记录
2. 滚动归档：按天拆分 archive/fallback-chain-YYYY-MM-DD.jsonl.gz
3. 报告：当前文件大小 + 总记录数 + 估算保留天数

用法：
    # 立即执行（保留 30 天 + 归档）
    venv/bin/python v5/scripts/audit_log_rotate.py --keep-days 30

    # 每日 03:00 自动（推荐用 systemd timer 调度）
    0 3 * * * venv/bin/python v5/scripts/audit_log_rotate.py --keep-days 30

环境变量：
    MATH_AUDIT_DIR: audit log 目录（默认 v5/audit）
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
ARCHIVE_DIR_NAME = "archive"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def list_audit_files(audit_dir: Path) -> list[Path]:
    """列出所有 fallback / canary jsonl（不含 archive 子目录）"""
    files = []
    for name in ["fallback-chain.jsonl", "fallback-canary.jsonl", "llm-calls.jsonl"]:
        p = audit_dir / name
        if p.exists():
            files.append(p)
    return files


def rotate_file(jsonl_path: Path, keep_days: int, archive_dir: Path, compress: bool = True):
    """单文件清理 + 归档"""
    if not jsonl_path.exists():
        return {"file": str(jsonl_path), "skipped": "not exists"}

    cutoff = _utc_now() - timedelta(days=keep_days)
    file_size_before = jsonl_path.stat().st_size

    keep_lines: list[str] = []
    rotate_lines: list[str] = []
    rotate_dates: set[str] = set()

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 坏行直接丢弃
                ts_str = entry.get("ts", "")
                if not ts_str:
                    continue
                try:
                    ts = _parse_ts(ts_str)
                except ValueError:
                    continue
                if ts >= cutoff:
                    keep_lines.append(line)
                else:
                    rotate_lines.append(line)
                    date_key = ts.strftime("%Y-%m-%d")
                    rotate_dates.add(date_key)
    except OSError as e:
        return {"file": str(jsonl_path), "error": str(e)}

    # 写回 keep_lines（原子替换：先写临时文件再 rename）
    if keep_lines:
        tmp = jsonl_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write("\n".join(keep_lines) + "\n")
        tmp.replace(jsonl_path)
    else:
        # 全被清空，保留空文件
        jsonl_path.write_text("")

    # 按日期归档
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_files: list[str] = []
    if compress and rotate_lines:
        for date_key in sorted(rotate_dates):
            archive_file = archive_dir / f"{jsonl_path.stem}-{date_key}.jsonl.gz"
            date_lines = []
            for line in rotate_lines:
                try:
                    e = json.loads(line)
                    d = _parse_ts(e.get("ts", "")).strftime("%Y-%m-%d")
                    if d == date_key:
                        date_lines.append(line)
                except json.JSONDecodeError, ValueError:
                    continue
            if date_lines:
                with gzip.open(archive_file, "at", encoding="utf-8") as gz:
                    gz.write("\n".join(date_lines) + "\n")
                archived_files.append(str(archive_file))

    file_size_after = jsonl_path.stat().st_size

    return {
        "file": jsonl_path.name,
        "size_before_bytes": file_size_before,
        "size_after_bytes": file_size_after,
        "kept_lines": len(keep_lines),
        "rotated_lines": len(rotate_lines),
        "rotated_dates": sorted(rotate_dates),
        "archive_files": archived_files,
    }


def estimate_growth_rate(jsonl_path: Path, days: int = 7) -> dict:
    """估算增长率（基于最近 N 天）"""
    if not jsonl_path.exists():
        return {"file": str(jsonl_path), "skipped": "not exists"}

    cutoff = _utc_now() - timedelta(days=days)
    recent_lines = 0
    total_size = jsonl_path.stat().st_size

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts = _parse_ts(entry.get("ts", ""))
                    if ts >= cutoff:
                        recent_lines += 1
                except json.JSONDecodeError, ValueError:
                    continue
    except OSError:
        pass

    lines_per_day = recent_lines / days if days else 0
    bytes_per_line = total_size / max(recent_lines, 1) if recent_lines else 0
    bytes_per_day = lines_per_day * bytes_per_line

    return {
        "file": jsonl_path.name,
        "recent_lines": recent_lines,
        "days_window": days,
        "lines_per_day": round(lines_per_day, 1),
        "bytes_per_line": round(bytes_per_line, 0),
        "bytes_per_day": round(bytes_per_day, 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="audit log 滚动归档 + TTL 清理")
    ap.add_argument("--keep-days", type=int, default=30, help="保留天数（默认 30）")
    ap.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="只统计，不实际写入")
    ap.add_argument("--estimate-only", action="store_true", help="只估算增长率，不清理")
    args = ap.parse_args()

    audit_dir = args.audit_dir
    archive_dir = audit_dir / ARCHIVE_DIR_NAME

    print("📦 audit log 滚动归档")
    print(f"  audit_dir: {audit_dir}")
    print(f"  archive_dir: {archive_dir}")
    print(f"  keep_days: {args.keep_days}")
    print(f"  dry_run: {args.dry_run}")
    print()

    files = list_audit_files(audit_dir)
    if not files:
        print("⚠️ 无 audit log 文件")
        return 0

    # 1. 增长率估算
    print("===== 增长率估算（最近 7 天）=====")
    for f in files:
        est = estimate_growth_rate(f)
        if "error" not in est:
            print(
                f"  {est['file']:<28}  {est['lines_per_day']:>6.1f} 行/天  "
                f"({est['bytes_per_day'] / 1024:.1f} KB/天)"
            )
    print()

    if args.estimate_only:
        return 0

    # 2. 实际清理 + 归档
    print("===== 清理 + 归档 =====")
    for f in files:
        if args.dry_run:
            print(f"  [DRY-RUN] {f.name}: 跳过写入")
            continue
        result = rotate_file(f, args.keep_days, archive_dir, compress=True)
        if "error" in result:
            print(f"  ❌ {result['file']}: {result['error']}")
        elif "skipped" in result:
            print(f"  ⏭️  {result['file']}: {result['skipped']}")
        else:
            print(f"  ✅ {result['file']}")
            print(f"     大小: {result['size_before_bytes']} → {result['size_after_bytes']} bytes")
            print(f"     保留: {result['kept_lines']} 行  归档: {result['rotated_lines']} 行")
            if result["rotated_dates"]:
                print(f"     归档日期: {', '.join(result['rotated_dates'])}")

    # 3. archive 目录统计
    if archive_dir.exists():
        archive_files = list(archive_dir.glob("*.jsonl.gz"))
        total_size = sum(f.stat().st_size for f in archive_files)
        print()
        print("===== archive 目录 =====")
        print(f"  归档文件: {len(archive_files)} 个")
        print(f"  总大小: {total_size / 1024 / 1024:.2f} MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
