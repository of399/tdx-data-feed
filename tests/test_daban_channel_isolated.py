"""委托单是**新的一条文件通道**，不许碰 plan_e 的实盘账本（设计 §1）。

2026-10-24 那条决定（`scripts/plan_e_journal.py:667` 的 `DABAN_LIVE_BLOCK`）仍然有效：
`--from-predict-list --mode real` 照旧拒绝。本试验仓产出的是给人抄进券商 App 的文本，
落在 `data/daban/tickets/`，与 `journal.csv` / `positions.json` 没有任何代码路径相连。

这条守卫用「整棵树里除了 tickets 什么都没多出文件」来钉住边界——比 mock 掉写账本的函数
更难被日后的改动绕过。
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_entry():
    fp = ROOT / "scripts" / "daban_entry.py"
    assert fp.exists(), f"{fp} 不存在"
    espec = importlib.util.spec_from_file_location("daban_entry_isolated", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


@dataclass
class Cand:
    code: str
    name: str
    limit_price: float
    score: float = 0.6
    sector_name: str = "板块"
    reasons: tuple = ("封板",)


class FakeScanner:
    def __init__(self, candidates):
        self._candidates = candidates
        self.tquotes = {"sh600001": object(), "sh600002": object()}

    def symbols(self):
        return ["sh600001", "sh600002"]

    def scan(self, quotes):
        return SimpleNamespace(trade_date="20261026", candidates=self._candidates, stats={})


def test_poll_writes_only_the_ticket_channel(tmp_path):
    mod = _load_entry()

    def fetch(symbols, **kw):
        return {s: SimpleNamespace(date="20261026") for s in symbols}

    rc = mod.poll_once(
        tmp_path,
        now=datetime(2026, 10, 26, 10, 0),
        build_scanner=lambda **kw: FakeScanner([Cand("600001", "甲", 10.0), Cand("600002", "乙", 10.0)]),
        fetch=fetch,
    )
    assert rc == 0

    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert written, "一轮连续竞价轮询什么都没产出"
    for rel in written:
        assert rel.startswith("data/daban/tickets/"), rel

    live = tmp_path / "data" / "plan_e" / "live"
    assert not (live / "journal.csv").exists()
    assert not (live / "positions.json").exists()
    assert not (tmp_path / "data" / "plan_e").exists(), "试验仓不该创建 plan_e 目录树"


def test_entry_module_does_not_import_the_live_journal(tmp_path):
    """入口不该把 `plan_e_journal` 拉进进程：那条通道一旦被 import，就被它的路径常量牵着走。"""
    mod = _load_entry()
    assert not hasattr(mod, "JOURNAL_FILE")
    assert not hasattr(mod, "LIVE_DIR")
    assert "plan_e_journal" not in str(mod.__file__)
