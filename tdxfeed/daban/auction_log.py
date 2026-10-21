"""竞价阶段快照记录器（§3.2 竞价看确认 / 附录 E 前瞻性积累）。

**为什么需要**：09:15–09:25 集合竞价的封单强度（虚拟开盘价、未撮合买量）是
打板的重要前置信号，但**没有历史数据源**（新浪接口只在实时提供），所以只能
前瞻性记录，积累样本后再做回测。

记录内容（每次轮询一行）：
    时间、代码、名称、虚拟开盘价、涨停价、竞价未撮合买量(股)、竞价金额(元)、
    涨跌幅、是否竞价封板

注意：09:15–09:20 委托可撤，09:20 后不可撤 → 同一只票会有多条记录，
回测时只用 **09:20 之后**的快照（``--after 09:20``）。

落盘：``data/daban/auction/auction_YYYYMMDD.csv``（盘中追加）
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

__all__ = ["AUCTION_DIR", "append_auction_rows", "read_auction_rows"]

AUCTION_DIR = Path(__file__).resolve().parents[2] / "data" / "daban" / "auction"

FIELDS = [
    "trade_date",
    "time",
    "code",
    "name",
    "price",
    "limit_price",
    "seal_shares",
    "seal_amount",
    "pct",
    "auction_sealed",
]


def append_auction_rows(rows: list[dict], out_dir: Path | str = AUCTION_DIR) -> Path:
    """把一批竞价快照追加到当日 CSV。"""
    if not rows:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / "auction_empty.csv"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trade_date = rows[0].get("trade_date", time.strftime("%Y%m%d"))
    path = out_dir / f"auction_{trade_date}.csv"
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return path


def read_auction_rows(path: Path | str) -> list[dict]:
    """读取竞价快照文件。"""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))
