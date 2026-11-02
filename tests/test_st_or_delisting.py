"""退市整理（「退」）与 ST 的合并判定必须单源（P1-12 Phase 2b，方案 A）。

方案 A = 新增 `tdxfeed/names.py::is_st_or_delisting`，**行为等价**地收编两处内联
（`scripts/daban_min60_features.py` 的 5% 档判定、`tdxfeed/daban/scanner.py` 的候选过滤）。
唯一语义差是排版空格：权威 `is_st_name` 先去空格，所以 "*S T康佳A" 旧写法漏判。
2026-11-02 探针：宇宙 5221 只 / hfq 样本 17794 行翻转数 0，故这是收编不是口径变更。

第三处「退」（`tdxfeed/daban/paper.py:299`）**不收编**：它把退市整理判成
`status="risk_warning"`（同一行的 `is_st` 列另走权威转发），是状态分类而非候选过滤，
合并会让 ST 名称也被打上 risk_warning，属改口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed import names  # noqa: E402
from tdxfeed.daban.scanner import Scanner  # noqa: E402

NEEDS_DATA = False


@pytest.mark.parametrize("name,expected", [
    ("*ST康佳A", True),
    ("ST康佳A", True),
    ("*S T康佳A", True),      # 排版空格拆开 ST：权威判 ST，旧内联写法漏判
    ("退市海润", True),
    ("海润退", True),
    ("康佳A", False),
    ("万  科Ａ", False),
    ("", False),
    (None, False),
])
def test_is_st_or_delisting_semantics(name, expected):
    assert names.is_st_or_delisting(name) is expected


def test_helper_delegates_to_is_st_name(monkeypatch):
    """ST 那一半必须真的走权威，而不是再抄一份字符串判定。"""
    monkeypatch.setattr(names, "is_st_name", lambda n: False)
    assert names.is_st_or_delisting("*ST康佳A") is False
    assert names.is_st_or_delisting("退市海润") is True

    monkeypatch.setattr(names, "is_st_name", lambda n: True)
    assert names.is_st_or_delisting("康佳A") is True


class _Pool:
    """Scanner.symbols() 只用到 universe / allow_st，用轻量替身避免构造真实扫描器。"""

    def __init__(self, universe, allow_st):
        self.universe = universe
        self.allow_st = allow_st


@pytest.mark.parametrize("allow_st,expected", [
    (False, ["sh600000"]),
    (True, ["sz000016", "sh600000", "sz000003"]),
])
def test_scanner_symbols_filters_st_and_delisting(allow_st, expected):
    pool = _Pool({"000016": "*S T康佳A", "600000": "浦发银行", "000003": "退市海润"}, allow_st)
    assert Scanner.symbols(pool) == expected


def test_min60_source_delegates():
    src = (ROOT / "scripts" / "daban_min60_features.py").read_text(encoding="utf-8")
    assert "is_st_or_delisting" in src, "5% 档的 ST/退判定应走权威合并入口"
    assert '"ST" in str(name).upper()' not in src, "不得在 build_one 内联判 ST"
