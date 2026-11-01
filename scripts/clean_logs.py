#!/usr/bin/env python3
"""日志轮转：删过期文件，并按大小截断只追加的日志。

`find -mtime +7 -delete` 对 systemd `append:` 写法的日志永远不触发——文件一直在被写，
mtime 永远是今天，于是 canary_history.jsonl 这类只会一直长。这里两条规则：

1. 超过 `--days` 天没动过的文件删除；
2. 仍在追加的日志超过 `--max-bytes` 时保留尾部（按行边界），原地截断。

原地截断保留 inode；systemd 的 `append:` 每次 open(O_APPEND)，不依赖旧偏移。
与正在写入的进程仍有极小的竞态窗口（读尾与截断之间新追加的行可能丢），
所以调度放在每周一 03:00，日志写入最稀疏的时段。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_DAYS = 7
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _tail_on_line_boundary(path: Path, max_bytes: int) -> bytes:
    """读末尾 max_bytes，并丢掉可能被截断的第一行。"""
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(size - max_bytes)
        data = f.read()
    nl = data.find(b"\n")
    return data[nl + 1:] if nl >= 0 else data


def clean(log_dir: Path, *, days: int = DEFAULT_DAYS,
          max_bytes: int = DEFAULT_MAX_BYTES, now: datetime | None = None,
          dry_run: bool = False) -> dict:
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    deleted: list[str] = []
    truncated: list[tuple[str, int, int]] = []
    skipped: list[str] = []

    if not log_dir.is_dir():
        return {"deleted": deleted, "truncated": truncated, "skipped": skipped}

    for path in sorted(log_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            st = path.stat()
            if datetime.fromtimestamp(st.st_mtime) < cutoff:
                if not dry_run:
                    path.unlink()
                deleted.append(str(path.relative_to(log_dir)))
            elif st.st_size > max_bytes:
                tail = _tail_on_line_boundary(path, max_bytes)
                if not dry_run:
                    with path.open("r+b") as f:
                        f.write(tail)
                        f.truncate()
                truncated.append((str(path.relative_to(log_dir)), st.st_size, len(tail)))
        except PermissionError:
            # logs/ 里有 root 写的文件（如 tdx-systemd.log）；用户级单元删不动它，
            # 报出来但不算失败，否则每周都推一次同样的告警。
            skipped.append(str(path.relative_to(log_dir)))

    return {"deleted": deleted, "truncated": truncated, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tdx-data-feed 日志轮转")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = clean(args.log_dir, days=args.days, max_bytes=args.max_bytes,
                    dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}logs: deleted={len(summary['deleted'])} "
          f"truncated={len(summary['truncated'])} skipped={len(summary['skipped'])}")
    for name in summary["deleted"]:
        print(f"  deleted: {name}")
    for name, before, after in summary["truncated"]:
        print(f"  truncated: {name} {before} -> {after} bytes")
    for name in summary["skipped"]:
        print(f"  skipped (permission): {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
