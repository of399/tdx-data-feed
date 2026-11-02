#!/usr/bin/env python3
"""全市场同步完成哨兵（KI-COMPETE-001 的修复点之一）。

由 `systemd/system/tdx-sync.service` 的一条**不带 `-` 前缀**的 `ExecStartPost` 调用：
`full_sync`（ExecStart）成功返回才会执行 ExecStartPost，失败则整条 ExecStartPost 链不执行
=> 同步失败必然不写哨兵。`predict-daily.service` 的 ExecStartPre 据此判定是否出榜。

写入 `data/.state/sync-done`（JSON：ts + 本轮 manifest.last_date），走 `tdxfeed.atomic`
的原子落位，避免半个哨兵文件被读到。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.atomic import atomic_write_text  # noqa: E402

SENTINEL = ROOT / "data" / ".state" / "sync-done"
MANIFEST = ROOT / "data" / ".state" / "manifest.json"

# 吞错计数（SEM002）：manifest 解析失败仍写哨兵（last_date=None），但必须留痕
_ERROR_COUNT: Counter[str] = Counter()


def main() -> int:
    last_date = None
    if MANIFEST.exists():
        try:
            last_date = json.loads(MANIFEST.read_text(encoding="utf-8")).get("last_date")
        except Exception:
            _ERROR_COUNT["main"] += 1
            last_date = None
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "last_date": last_date,
    }
    atomic_write_text(SENTINEL, json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[sentinel] 已写同步完成哨兵 {SENTINEL} last_date={last_date}")
    return 0


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="写同步完成哨兵（由 tdx-sync.service 的 ExecStartPost 调用，无参数）"
    ).parse_known_args()
    sys.exit(main())
