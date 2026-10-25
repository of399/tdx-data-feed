"""同一交易日同一只票只出一张委托单，首次触发优先（设计 §4.1 / §4.2 第 6 步）。

timer 每 5 分钟命中一次，一只票从 09:40 封到 14:50 会被反复扫到。如果每轮都补一张单，
人工就会在同一只票上按 20 次，试验仓的额度与样本口径同时失真。所以 upsert 的键是
**(交易日, code)**，已存在的 code 直接丢弃——保留的是第一次触发时的价格与理由，
因为打板要抢的就是那一次触发。

`data/` 在 `.gitignore:42` 里，就地覆写当日文件前必须留带日期备份：git 救不回来。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402


@dataclass
class Intent:
    code: str
    name: str
    price: float
    shares: int
    amount: float
    stop_loss: float = 9.5
    take_profit: float = 11.0
    score: float = 0.62
    sector: str = "板块"
    reason: str = "封单比 1.8"
    created_at: str = "2026-10-26 09:40:00"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "shares": self.shares,
            "amount": self.amount,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "score": self.score,
            "sector": self.sector,
            "reason": self.reason,
            "created_at": self.created_at,
        }


def md_file(root: Path, trade_date: str) -> Path:
    return root / "data" / "daban" / "tickets" / f"ticket_{trade_date}.md"


def json_file(root: Path, trade_date: str) -> Path:
    return root / "data" / "daban" / "tickets" / f"ticket_{trade_date}.json"


def test_sector_column_is_labelled_as_a_code(tmp_path):
    """真实扫描给出的 sector 是行业代码，不是名字——列名写「板块」会读错。

    实测（2026-10-23 一轮真扫描）：`Scanner.sector_names` 是空 dict，`Candidate.sector_name`
    回落到 `sector_code`，三只票依次是 630101 / 280203 / 640701。人判断「这三只是不是同一个概念
    的一注」需要的是名字（可读的是 `concept_name`，走 `OrderIntent` 之外的字段），
    这里至少不能把代码标成名字。
    """
    day = trial.load_day(tmp_path, "20261026")
    trial.upsert_tickets(
        tmp_path, "20261026", [Intent("600001", "甲", 10.0, 1000, 10000.0, sector="630101")], day=day
    )
    text = md_file(tmp_path, "20261026").read_text(encoding="utf-8")
    header = next(ln for ln in text.splitlines() if ln.startswith("| 代码 |"))
    assert "| 板块代码 |" in header, header
    assert "| 板块 |" not in header, header
    assert "630101" in text


def test_second_touch_of_same_code_same_day_is_dropped(tmp_path):
    day = trial.load_day(tmp_path, "20261026")
    first = trial.upsert_tickets(tmp_path, "20261026", [Intent("600001", "甲", 10.0, 1000, 10000.0)], day=day)
    assert [t.code for t in first.new] == ["600001"]

    day = first.day
    later = trial.upsert_tickets(
        tmp_path, "20261026", [Intent("600001", "甲", 10.0, 900, 9000.0, created_at="2026-10-26 14:50:00")],
        day=day,
    )
    assert later.new == []
    assert [t.code for t in later.dropped] == ["600001"]
    # 首触发生效：留下的必须是 09:40 那次，股数不被后面那次改写
    assert day["tickets"][0]["created_at"] == "2026-10-26 09:40:00"
    assert day["tickets"][0]["shares"] == 1000


def test_ticket_files_written_with_banner_and_are_the_whole_day(tmp_path):
    day = trial.load_day(tmp_path, "20261026")
    res = trial.upsert_tickets(
        tmp_path,
        "20261026",
        [Intent("600001", "甲", 10.0, 1000, 10000.0), Intent("600002", "乙", 20.0, 500, 10000.0)],
        day=day,
    )
    day = res.day

    text = md_file(tmp_path, "20261026").read_text(encoding="utf-8")
    assert trial.BANNER_HEAD in text, "委托单顶部少了负期望横幅"
    assert "600001" in text and "600002" in text
    assert text.count("| 600001") == 1, "同一张票在文件里出现了两次"

    payload = json.loads(json_file(tmp_path, "20261026").read_text(encoding="utf-8"))
    assert payload["trade_date"] == "20261026"
    assert [t["code"] for t in payload["tickets"]] == ["600001", "600002"]


def test_reload_from_disk_keeps_committed_amount(tmp_path):
    day = trial.load_day(tmp_path, "20261026")
    trial.upsert_tickets(tmp_path, "20261026", [Intent("600001", "甲", 10.0, 1000, 10000.0)], day=day)

    reread = trial.load_day(tmp_path, "20261026")
    assert trial.committed_amount(reread) == 10000.0
    assert trial.remaining_budget(reread) == 10000.0


def test_new_trading_day_resets_the_ledger(tmp_path):
    day = trial.load_day(tmp_path, "20261026")
    trial.upsert_tickets(tmp_path, "20261026", [Intent("600001", "甲", 10.0, 1000, 10000.0)], day=day)

    fresh = trial.load_day(tmp_path, "20261027")
    assert fresh["tickets"] == []
    assert trial.remaining_budget(fresh) == trial.TOTAL_CAP


def test_overwriting_the_day_file_keeps_a_dated_backup(tmp_path):
    """覆写当日委托单前留 .bak：data/ 被 gitignore，写坏了 git 救不回来。"""
    day = trial.load_day(tmp_path, "20261026")
    trial.upsert_tickets(tmp_path, "20261026", [Intent("600001", "甲", 10.0, 1000, 10000.0)], day=day)
    before = md_file(tmp_path, "20261026").read_text(encoding="utf-8")

    trial.upsert_tickets(tmp_path, "20261026", [Intent("600003", "丙", 8.0, 1250, 10000.0)], day=day)
    backups = sorted((tmp_path / "data" / "daban" / "tickets").glob("ticket_20261026.md.bak-*"))
    assert backups, "就地覆写没留备份"
    assert backups[0].read_text(encoding="utf-8") == before
