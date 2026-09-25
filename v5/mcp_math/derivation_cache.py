"""D3.3 中间结果持久化（M2 W11）

依据：roadmap §13.4 W11 D3.3

为每条 DAG 推导链提供 sidecar 持久化：
- 文件 sidecar: <note_id>.derivation.json（可读、可 diff、可 Git 管理）
- SQLite 索引：by_expr_hash 快速查找 + 命中率统计
- 缓存命中率预期：首跑 0% → 迭代 ≥40% → ≥70%

侧车路径：
  v5/cache/derivation_sidecar/<note_id>.json
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SIDECAR_DIR = Path("/home/jiuben/tdx-data-feed/v5/cache/derivation_sidecar")
SIDECAR_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = SIDECAR_DIR / "derivation_cache.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============ Schema 初始化 ============

_INIT_LOCK = threading.Lock()


def _init_schema(conn: sqlite3.Connection):
    """创建 sidecar 缓存表"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS derivation_cache (
            start_hash TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            var TEXT NOT NULL,
            dag_json TEXT NOT NULL,
            steps INTEGER NOT NULL,
            success INTEGER NOT NULL,
            reason TEXT,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (start_hash, target_hash, var)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_start_hash ON derivation_cache(start_hash)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_target_hash ON derivation_cache(target_hash)
    """)
    conn.commit()


@contextmanager
def _connect():
    """sqlite3 连接（线程安全）"""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    _init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


# ============ Hash 与序列化 ============


def _hash_expr(latex: str) -> str:
    """LaTeX 表达式 → 12 字符 hash"""
    return hashlib.md5(latex.encode("utf-8")).hexdigest()[:12]


def _hash_pair(start: str, target: str, var: str) -> tuple[str, str, str]:
    return (
        _hash_expr(start),
        _hash_expr(target),
        var,
    )


# ============ 持久化 API ============


def save_dag(
    dag_dict: dict,
    note_id: str | None = None,
    ttl_days: int = 30,
) -> str:
    """保存 DAG 到 sidecar JSON + SQLite 索引"""
    start_hash, target_hash, var = _hash_pair(
        dag_dict["start_latex"],
        dag_dict["target_latex"],
        dag_dict.get("var", "x"),
    )

    if note_id is None:
        note_id = f"{start_hash}_{target_hash}_{var}"

    sidecar_path = SIDECAR_DIR / f"{note_id}.derivation.json"
    now = time.time()

    # 1. 写 sidecar JSON（含 TTL）
    sidecar = {
        **dag_dict,
        "_meta": {
            "note_id": note_id,
            "saved_at": now,
            "ttl_days": ttl_days,
            "start_hash": start_hash,
            "target_hash": target_hash,
        },
    }
    sidecar_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2. 写 SQLite 索引
    with _INIT_LOCK, _connect() as conn:
        conn.execute(
            """
                INSERT OR REPLACE INTO derivation_cache
                  (start_hash, target_hash, var, dag_json, steps, success, reason,
                   created_at, last_accessed, hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT hits FROM derivation_cache
                     WHERE start_hash=? AND target_hash=? AND var=?),
                    0
                ))
            """,
            (
                start_hash,
                target_hash,
                var,
                json.dumps(dag_dict, ensure_ascii=False),
                dag_dict.get("steps", 0),
                int(bool(dag_dict.get("success"))),
                dag_dict.get("reason", ""),
                now,
                now,
                start_hash,
                target_hash,
                var,
            ),
        )

    return str(sidecar_path)


def get_cached(
    start_latex: str,
    target_latex: str,
    var: str = "x",
) -> dict | None:
    """查找缓存的 DAG（命中 +1）"""
    start_hash, target_hash, var_key = _hash_pair(start_latex, target_latex, var)

    with _INIT_LOCK, _connect() as conn:
        row = conn.execute(
            """
                SELECT dag_json, hits, created_at FROM derivation_cache
                WHERE start_hash=? AND target_hash=? AND var=?
            """,
            (start_hash, target_hash, var_key),
        ).fetchone()

        if row is None:
            return None

        dag_json, _hits, created_at = row
        age_days = (time.time() - created_at) / 86400
        ttl_days = 30
        if age_days > ttl_days:
            conn.execute(
                """
                    DELETE FROM derivation_cache
                    WHERE start_hash=? AND target_hash=? AND var=?
                """,
                (start_hash, target_hash, var_key),
            )
            return None

        conn.execute(
            """
                UPDATE derivation_cache
                SET last_accessed=?, hits=hits+1
                WHERE start_hash=? AND target_hash=? AND var=?
            """,
            (time.time(), start_hash, target_hash, var_key),
        )

        return json.loads(dag_json)


def cache_stats() -> dict:
    """缓存统计"""
    with _INIT_LOCK, _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM derivation_cache").fetchone()[0]
        total_hits = conn.execute("SELECT COALESCE(SUM(hits), 0) FROM derivation_cache").fetchone()[
            0
        ]
        success_count = conn.execute(
            "SELECT COUNT(*) FROM derivation_cache WHERE success=1"
        ).fetchone()[0]

        sidecar_count = sum(1 for _ in SIDECAR_DIR.glob("*.derivation.json"))

        try:
            size_mb = sum(p.stat().st_size for p in SIDECAR_DIR.iterdir()) / 1024 / 1024
        except Exception:
            size_mb = 0

        return {
            "total_entries": total,
            "success_entries": success_count,
            "total_hits": total_hits,
            "sidecar_files": sidecar_count,
            "size_mb": round(size_mb, 4),
            "sidecar_dir": str(SIDECAR_DIR),
            "db_path": str(DB_PATH),
        }


def list_cached(limit: int = 50) -> list[dict]:
    """列出最近缓存的 DAG（按 last_accessed 倒序）"""
    with _INIT_LOCK, _connect() as conn:
        rows = conn.execute(
            """
                SELECT start_hash, target_hash, var, steps, success, reason,
                       hits, created_at, last_accessed
                FROM derivation_cache
                ORDER BY last_accessed DESC
                LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "start_hash": r[0],
                "target_hash": r[1],
                "var": r[2],
                "steps": r[3],
                "success": bool(r[4]),
                "reason": r[5],
                "hits": r[6],
                "created_at": r[7],
                "last_accessed": r[8],
            }
            for r in rows
        ]


def clear_expired(ttl_days: int = 30) -> int:
    """清理过期缓存"""
    cutoff = time.time() - ttl_days * 86400
    with _INIT_LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM derivation_cache WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ============ 便捷包装：build_chain + cache 联动 ============


def build_chain_with_cache(
    start_latex: str,
    target_latex: str,
    var: str = "x",
    max_depth: int = 4,
    rules: list[str] | None = None,
    timeout_s: float = 5.0,
    note_id: str | None = None,
) -> dict:
    """构建 DAG（先查缓存，未命中再 build + 持久化）"""
    cached = get_cached(start_latex, target_latex, var)
    if cached is not None:
        return {"dag": cached, "source": "cache", "cache_stats": cache_stats()}

    from mcp_math.derivation import build_chain

    dag = build_chain(
        start_latex=start_latex,
        target_latex=target_latex,
        var=var,
        max_depth=max_depth,
        rules=rules,
        timeout_s=timeout_s,
    )

    dag_dict = dag.to_dict()
    save_dag(dag_dict, note_id=note_id)

    return {"dag": dag_dict, "source": "computed", "cache_stats": cache_stats()}
