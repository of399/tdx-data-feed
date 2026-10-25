"""同一概念当日限注 `CONCEPT_CAP` 笔（设计 §9；上限 2 笔由用户 2026-10-25 定）。

为什么「幂等去重」不够：`(交易日, code)` 去重只挡得住同一只票被反复扫到，挡不住
**一整个概念被反复扫到**。2026-10-25 活体彩排实测一轮 92 只候选里评分前 3 名全是
「智能穿戴」——按额度口径这 3 笔会全部出单，当日就是「一注 1.6 万」而不是三注，
样本也不再是三个独立观测（同概念的票同日同涨同跌，n=30 的有效样本远小于 30）。

两条实现约束：

1. 概念必须**跨轮**成立。timer 一天命中约 48 轮，只在本轮候选列表里数概念，第二轮照样会
   把同一概念补到 3 笔——所以概念要像 `limit_src` 一样写进当日账本行，读账本才算得出
   「这个概念今天用掉几笔」。
2. **空概念不并桶**。实测 92 只里 `concept_name` 为空的是 59 只；把空值当同一个概念，限注会
   退化成「全日只出 2 笔」，把额度口径整条推翻。空值不参与限注，也不占别人的桶。

本文件的票档统一用 `limit_price=55.0`：单笔 = 1 手 100 股 × 55 = 5500 元，一轮最多 3 笔
（第 4 笔只剩 3500 元预算、凑不出一手），正好是 2026-10-23 真盘那轮的形状。

概念只进 JSON 账本（判据要能复核），不加进委托单 md 表格——那仍是 §9 的非目标。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402
from tdxfeed.daban.order import OrderIntent  # noqa: E402

PRICE = 55.0  # 单笔 5500 元：一轮 3 笔、当日 4 个坑（20000/5500 向下取整 3 笔 + 尾差）
A = "智能穿戴"
B = "量子科技"


@dataclass
class Cand:
    """`build_intents` 读的字段 + 判据用的 `concept_name`（真候选来自 `scanner.py:112` 同名字段）。"""

    code: str
    name: str
    limit_price: float
    concept_name: str = ""
    score: float = 0.6
    sector_name: str = "630101"
    reasons: tuple[str, ...] = ("封板",)


def mk(code: str, concept: str = "", price: float = PRICE) -> Cand:
    return Cand(code=code, name=f"票{code}", limit_price=price, concept_name=concept)


def empty_day() -> dict:
    return trial.load_day(Path("/nonexistent-root-for-unit-tests"), "20261026")


def test_concept_cap_is_two_tickets_per_day():
    """用户 2026-10-25 定的参数：每概念当日 2 笔。"""
    assert trial.CONCEPT_CAP == 2


def test_third_ticket_of_one_concept_is_skipped_within_a_single_poll():
    """甲,甲,甲,乙 四只候选：出的是 600001 600002 600004，不是 600001 600002 600003。

    被限掉的第三只甲把坑**让给下一个异概念票**（预算本来也只够 3 笔，所以总额不变），
    而不是把额度空在那里。
    """
    day = empty_day()
    intents = trial.intents_for_poll([mk("600001", A), mk("600002", A), mk("600003", A), mk("600004", B)], day)

    assert [i.code for i in intents] == ["600001", "600002", "600004"], [(i.code, i.amount) for i in intents]
    assert [i.amount for i in intents] == [5500.0, 5500.0, 5500.0]


def test_concept_count_carries_across_polls_through_the_day_ledger():
    """第一轮已出 2 笔同概念 ⇒ 第二轮同概念的第 3 只必须 0 票，哪怕账上还有额度。

    第二轮只剩 1 只同概念候选、剩余额度 9000 元（够一手 5500），所以「0 票」只能是概念
    限注挡的——没跨轮持久化的话这轮会照常出票。
    """
    day = empty_day()
    first = trial.intents_for_poll([mk("600001", A), mk("600002", A)], day)
    assert [i.code for i in first] == ["600001", "600002"]
    day = trial.add_tickets(day, first, concept_map={"600001": A, "600002": A})
    assert trial.remaining_budget(day) == 9000.0

    assert trial.intents_for_poll([mk("600003", A)], day) == [], "同概念跨轮没被限住"


def test_blank_concepts_do_not_share_one_bucket():
    """实测 92 只候选里 59 只 `concept_name` 为空：空值并桶会把全日限成 2 笔。"""
    day = empty_day()
    intents = trial.intents_for_poll([mk("600001"), mk("600002"), mk("600003"), mk("600004")], day)

    assert len(intents) == 3, "空概念被并成了同一个桶（只出 2 笔）"


def test_unlabelled_candidate_is_not_blocked_by_a_full_bucket():
    """某概念已满 2 笔时，无标签的候选照旧出票——限注只约束「同一个概念」。"""
    day = empty_day()
    first = trial.intents_for_poll([mk("600001", B), mk("600002", B)], day)
    day = trial.add_tickets(day, first, concept_map={"600001": B, "600002": B})

    later = trial.intents_for_poll([mk("600003", B), mk("600004")], day)
    assert [i.code for i in later] == ["600004"]


def ticket(code: str, name: str) -> OrderIntent:
    """`build_intents` 的真实形状（止损/止盈必有，md 渲染要 float 它们）。"""
    return OrderIntent(
        code=code, name=name, price=PRICE, shares=100, amount=5500.0, stop_loss=52.25, take_profit=60.50
    )


def test_ticket_rows_record_the_concept_and_default_to_blank(tmp_path):
    """概念要能在账本里复核；没传 concept_map 时写空串，且**不占任何桶**。"""
    day = trial.load_day(tmp_path, "20261026")
    labelled = trial.upsert_tickets(
        tmp_path,
        "20261026",
        [ticket("600001", "甲")],
        day=day,
        concept_map={"600001": A},
    )
    assert labelled.day["tickets"][0]["concept"] == A

    unlabelled = trial.upsert_tickets(
        tmp_path,
        "20261026",
        [ticket("600002", "乙")],
        day=labelled.day,
    )
    assert unlabelled.day["tickets"][1]["concept"] == ""
    # 空概念行不记入 A 的桶：A 名义上「已有 2 笔」，实际只占 1 笔 ⇒ 还能出 1 笔
    assert [i.code for i in trial.intents_for_poll([mk("600003", A)], unlabelled.day)] == ["600003"]


# ============== 入口脚本：把限注的生效情况打给人看 ==============
def _load_entry():
    fp = ROOT / "scripts" / "daban_entry.py"
    assert fp.exists(), f"{fp} 不存在"
    espec = importlib.util.spec_from_file_location("daban_entry_concept_under_test", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


SYMBOLS = [f"sh{code}" for code in ("600001", "600002", "600003", "600004")]


class Env:
    """替掉扫描/行情层（同 `test_daban_entry_session_gate.py` 的形状），只关心概念这一件事。"""

    def __init__(self, cands):
        self.cands = cands

    def fetch(self, symbols, **kw):
        return {s: SimpleNamespace(date="2026-10-26") for s in symbols}

    def build_scanner(self, **kwargs):
        outer = self

        class FakeScanner:
            tquotes: ClassVar[dict] = {s: object() for s in SYMBOLS}

            def symbols(self):
                return list(SYMBOLS)

            def scan(self, quotes):
                return SimpleNamespace(trade_date="2026-10-26", candidates=outer.cands, stats={})

        return FakeScanner()


def poll(mod, tmp_path, candidates):
    env = Env(candidates)
    return mod.poll_once(
        tmp_path, now=datetime(2026, 10, 26, 10, 0), build_scanner=env.build_scanner, fetch=env.fetch
    )


def test_entry_prints_how_many_candidates_the_cap_skipped(tmp_path, capsys):
    """被限掉的候选不能静消失：人工复核要能看出本轮少的是哪一类。"""
    mod = _load_entry()
    assert poll(mod, tmp_path, [mk("600001", A), mk("600002", A), mk("600003", A), mk("600004", B)]) == 0
    printed = capsys.readouterr().out
    assert "跳过 1" in printed, printed


def test_entry_says_the_cap_is_inert_when_no_candidate_is_labelled(tmp_path, capsys):
    """概念快照没给标签时限注必然 0 命中——必须明写「未生效」，不能长得像「已生效且无需跳过」。"""
    mod = _load_entry()
    assert poll(mod, tmp_path, [mk("600001"), mk("600002"), mk("600003"), mk("600004")]) == 0
    printed = capsys.readouterr().out
    assert "未生效" in printed, printed


def test_entry_writes_the_concept_into_the_ledger_row(tmp_path):
    mod = _load_entry()
    poll(mod, tmp_path, [mk("600001", A), mk("600002", B)])
    payload = json.loads(
        (tmp_path / "data" / "daban" / "tickets" / "ticket_20261026.json").read_text(encoding="utf-8")
    )
    assert [t["concept"] for t in payload["tickets"]] == [A, B], payload
