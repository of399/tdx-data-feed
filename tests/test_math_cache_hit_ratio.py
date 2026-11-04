"""命中率必须落在「查表次数」这个分母上，而不是缓存条目数。

钉住的缺陷（实测于 2026-11-04）：`/health` 报
`math_cache {total_entries:190, total_hits:353, hit_ratio:1.858}`、
`derivation_cache {total:0, total_hits:199, hit_ratio:199.0}`。比率能大于 1 只有一种解释——
分母取的是**另一个量**：`total_hits` 是 per-entry `hits` 列的累加（每存一条本身就先记 1），
`total_entries` 是当前行数，两者相除既不是命中率也不是任何可解释的指标。
`docs/archive/math_backlog_priority_20261025.md:30` 早在 2026-10-25 就把它列为 P3（当时是
`165.0` / `1.947`），一直没修。`v5/monitoring/prometheus-alerts.yml:53` 还有一条活规则
`cache_hit_ratio{cache="math"} < 0.5` 绑在这个数上——正因它恒 >1，那条告警永远不响。

A 档口径（负责人 2026-11-04 选定）：两个缓存各自持久化一张 `lookup_stats`，
一次查表只能记到 hits 或 misses 其中一边，命中率 = `lookup_hits / (lookup_hits + lookup_misses)`。
一条读法约定：**从没查过表 ⇒ `hit_ratio` 是 `None`（没测），不是 `0`（全不中）**——
后者会被 Prometheus 与看板读成"缓存彻底失效"。

本文件不碰任何真库：两个模块的 `DB_PATH`/`SIDECAR_DIR` 都改到 tmp 目录，
`math_cache._CONN` 进程内缓存一并复位（否则 monkeypatch 打不到已建立的连接）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
V5 = ROOT / "v5"
if str(V5) not in sys.path:
    sys.path.insert(0, str(V5))

from mcp_math import derivation_cache, math_cache  # noqa: E402

LATEX_A = "\\frac{1}{1}"
LATEX_B = "1"
LATEX_C = "\\sqrt{2}"


@pytest.fixture()
def math_cache_fresh(tmp_path, monkeypatch):
    """指向 tmp 库的 math_cache（模块级 _CONN 必须复位，否则连的还是老库）。"""
    monkeypatch.setattr(math_cache, "DB_PATH", tmp_path / "math_cache.sqlite")
    monkeypatch.setattr(math_cache, "_CONN", None)
    yield math_cache
    monkeypatch.setattr(math_cache, "_CONN", None)


@pytest.fixture()
def derivation_cache_fresh(tmp_path, monkeypatch):
    sidecar = tmp_path / "derivation_sidecar"
    sidecar.mkdir(parents=True)
    monkeypatch.setattr(derivation_cache, "DB_PATH", sidecar / "derivation_cache.sqlite")
    monkeypatch.setattr(derivation_cache, "SIDECAR_DIR", sidecar)
    return derivation_cache


def _dag(start: str, target: str) -> dict:
    return {"start_latex": start, "target_latex": target, "var": "x", "success": True}


# ---------------------------------------------------------------------------
# math_cache
# ---------------------------------------------------------------------------


def test_hit_ratio_denominator_is_lookups_not_entries(math_cache_fresh) -> None:
    """1 中 1 不 ⇒ 命中率恰为 0.5。这是缺陷的核心：旧口径会给出 >1 的数。"""
    mc = math_cache_fresh
    mc.set_cached(LATEX_A, LATEX_B, "equiv", {"answer": True})

    assert mc.get_cached(LATEX_A, LATEX_B, "equiv") is not None  # 命中
    assert mc.get_cached(LATEX_C, LATEX_A, "equiv") is None  # 未命中

    stats = mc.cache_stats()
    assert stats["lookup_hits"] == 1
    assert stats["lookup_misses"] == 1
    assert stats["lookups"] == 2
    assert stats["hit_ratio"] == pytest.approx(0.5)


def test_hit_ratio_is_bounded_for_every_cache(math_cache_fresh) -> None:
    """比率必须落在 [0,1] 或 None —— 这条就是缺陷的可执行定义。"""
    mc = math_cache_fresh
    mc.set_cached(LATEX_A, LATEX_B, "equiv", {"answer": True})
    for _ in range(3):
        mc.get_cached(LATEX_A, LATEX_B, "equiv")
    mc.get_cached(LATEX_C, LATEX_A, "equiv")

    ratio = mc.cache_stats()["hit_ratio"]
    assert ratio is not None and 0.0 <= ratio <= 1.0


def test_no_lookup_yet_reports_none_not_zero(math_cache_fresh) -> None:
    """空表是「没测」不是「全不中」。写 0 会让看板读成缓存彻底失效。"""
    stats = math_cache_fresh.cache_stats()
    assert stats["lookups"] == 0
    assert stats["hit_ratio"] is None


def test_expired_entry_counts_as_miss(math_cache_fresh) -> None:
    """过期条目按未命中记，否则 TTL 一过命中率会虚高。"""
    mc = math_cache_fresh
    mc.set_cached(LATEX_A, LATEX_B, "equiv", {"answer": True}, ttl_s=-1)

    assert mc.get_cached(LATEX_A, LATEX_B, "equiv") is None

    stats = mc.cache_stats()
    assert stats["lookup_misses"] == 1 and stats["lookup_hits"] == 0


def test_unparseable_result_counts_as_miss(math_cache_fresh) -> None:
    """存进去的东西读不出来就没服务到请求：记 miss，不记 hit。

    旧代码在这条路上已经把 per-entry `hits` 加了一次（先 UPDATE 再 json.loads），
    所以那条列本身也在往「命中」里掺水。这里只钉新增的查表口径，不动旧列。
    """
    mc = math_cache_fresh
    mc.set_cached(LATEX_A, LATEX_B, "equiv", {"answer": True})
    conn = mc._get_conn()
    conn.execute("UPDATE cache SET result=? WHERE action=?", ("{ 不是 json", "equiv"))

    assert mc.get_cached(LATEX_A, LATEX_B, "equiv") is None

    stats = mc.cache_stats()
    assert stats["lookup_misses"] == 1 and stats["lookup_hits"] == 0


# ---------------------------------------------------------------------------
# derivation_cache（同一口径，另一张表）
# ---------------------------------------------------------------------------


def test_derivation_hit_ratio_uses_lookup_denominator(derivation_cache_fresh) -> None:
    dc = derivation_cache_fresh
    dc.save_dag(_dag(LATEX_A, LATEX_B), note_id="t1")

    assert dc.get_cached(LATEX_A, LATEX_B) is not None  # 命中
    assert dc.get_cached(LATEX_C, LATEX_A) is None  # 未命中

    stats = dc.cache_stats()
    assert stats["lookup_hits"] == 1
    assert stats["lookup_misses"] == 1
    assert stats["hit_ratio"] == pytest.approx(0.5)


def test_derivation_no_lookup_yet_reports_none(derivation_cache_fresh) -> None:
    stats = derivation_cache_fresh.cache_stats()
    assert stats["lookups"] == 0 and stats["hit_ratio"] is None


def test_derivation_expired_entry_counts_as_miss(derivation_cache_fresh) -> None:
    """30 天 TTL 越界 ⇒ 删行并记 miss（与 math_cache 同一条读法）。"""
    dc = derivation_cache_fresh
    dc.save_dag(_dag(LATEX_A, LATEX_B), note_id="t2")
    conn = sqlite3.connect(str(dc.DB_PATH), isolation_level=None)
    try:
        conn.execute("UPDATE derivation_cache SET created_at=?", (1.0,))
    finally:
        conn.close()

    assert dc.get_cached(LATEX_A, LATEX_B) is None

    stats = dc.cache_stats()
    assert stats["lookup_misses"] == 1 and stats["lookup_hits"] == 0


# ---------------------------------------------------------------------------
# 形状不被悄悄改掉：两个旧键必须还在（下游有断言，不许用新字段换掉旧字段）
# ---------------------------------------------------------------------------


def test_existing_stat_keys_are_kept(math_cache_fresh, derivation_cache_fresh) -> None:
    """`total_entries` 有在用断言（v5/tests/test_math_sympy.py 的 MCP 用例、
    tests/test_mcp_contracts.py 的契约表），新字段只能是**加法**。"""
    m_keys = set(math_cache_fresh.cache_stats())
    d_keys = set(derivation_cache_fresh.cache_stats())
    assert {"total_entries", "total_hits"} <= m_keys
    assert {"total_entries", "total_hits"} <= d_keys
    assert {"lookups", "lookup_hits", "lookup_misses", "hit_ratio"} <= m_keys
    assert {"lookups", "lookup_hits", "lookup_misses", "hit_ratio"} <= d_keys


def test_lookup_stats_table_is_persisted(math_cache_fresh) -> None:
    """计数落库，不落进程内存：两个服务进程（stdio + HTTP）共用同一张库，
    进程内计数器会让 `/health` 与 `/metrics` 各自报自己的数。"""
    mc = math_cache_fresh
    mc.set_cached(LATEX_A, LATEX_B, "equiv", {"answer": True})
    mc.get_cached(LATEX_A, LATEX_B, "equiv")

    conn = sqlite3.connect(str(mc.DB_PATH), isolation_level=None)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = [list(r) for r in conn.execute("SELECT action, hits, misses FROM lookup_stats")]
    finally:
        conn.close()
    assert "lookup_stats" in tables
    assert rows == [["equiv", 1, 0]]
