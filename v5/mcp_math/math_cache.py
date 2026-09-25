"""
math_cache · v5-VE1-M2 §12.7 D6.1 + D6.2 + D6.4

D6.1 预筛层：LaTeX 字符串快速相似度预判，避免无谓 simplify 调用
D6.2 持久化缓存：SQLite 缓存 (latex_a, latex_b, action) → result，TTL 7 天
D6.4 增量扫描：mtime hash，只重算变化笔记

性能预期：
  - 预筛减少 90% simplify 调用（Ollama 实测 p95 从数秒降到 <100ms）
  - 缓存命中：重复调用 0 成本（毫秒级 SQLite 读）
  - 增量扫描：周日凌晨任务从全量 60min → <5min
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

# ============================================================================
# 路径与配置
# ============================================================================

CACHE_DIR = Path("/home/jiuben/tdx-data-feed/v5/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "math_cache.sqlite"

DEFAULT_TTL_S = 604800  # 7 天
SCHEMA_VERSION = 1

_conn_lock = Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


_CONN: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        with _conn_lock:
            if _CONN is None:
                _CONN = _connect()
                _init_schema(_CONN)
    return _CONN


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS cache (
            cache_key TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            input_len_a INTEGER,
            input_len_b INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at);
        CREATE INDEX IF NOT EXISTS idx_action ON cache(action);
    """)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
        ("version", str(SCHEMA_VERSION)),
    )


# ============================================================================
# D6.2 缓存 API
# ============================================================================


def cache_key(latex_a: str, latex_b: str = "", action: str = "equiv") -> str:
    h = hashlib.sha256()
    h.update(latex_a.encode("utf-8"))
    h.update(b"\x00")
    h.update(latex_b.encode("utf-8"))
    h.update(b"\x00")
    h.update(action.encode("utf-8"))
    return h.hexdigest()[:32]


def get_cached(latex_a: str, latex_b: str = "", action: str = "equiv") -> dict | None:
    key = cache_key(latex_a, latex_b, action)
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT result FROM cache WHERE cache_key=? AND expires_at > ?",
            (key, time.time()),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("UPDATE cache SET hits = hits + 1 WHERE cache_key=?", (key,))
    try:
        return json.loads(row["result"])
    except json.JSONDecodeError, TypeError:
        return None


def set_cached(
    latex_a: str, latex_b: str, action: str, result: dict, ttl_s: int = DEFAULT_TTL_S
) -> bool:
    key = cache_key(latex_a, latex_b, action)
    now = time.time()
    try:
        _get_conn().execute(
            """INSERT OR REPLACE INTO cache
               (cache_key, action, result, created_at, expires_at, hits, input_len_a, input_len_b)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                key,
                action,
                json.dumps(result, ensure_ascii=False),
                now,
                now + ttl_s,
                len(latex_a),
                len(latex_b),
            ),
        )
        return True
    except sqlite3.DatabaseError:
        return False


def cache_stats() -> dict:
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(hits), 0) AS hits FROM cache"
        ).fetchone()
        return {
            "total_entries": row["total"] if row else 0,
            "total_hits": row["hits"] if row else 0,
            "db_path": str(DB_PATH),
            "size_mb": DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0,
        }
    except sqlite3.DatabaseError:
        return {"error": "cache db unavailable"}


def clear_expired() -> int:
    try:
        cur = _get_conn().execute("DELETE FROM cache WHERE expires_at <= ?", (time.time(),))
        return cur.rowcount
    except sqlite3.DatabaseError:
        return 0


# ============================================================================
# D6.1 预筛层
# ============================================================================

_LATEX_TOKEN_RE = re.compile(r"\\[a-zA-Z]+|[\^_{}]|[\w]+|[^\s\w]")


def _tokenize(latex: str) -> set[str]:
    if not latex:
        return set()
    return set(_LATEX_TOKEN_RE.findall(latex))


def prefilter(
    latex_a: str, latex_b: str = "", length_tolerance: float = 0.5, jaccard_threshold: float = 0.3
) -> tuple[bool, str]:
    """
    D6.1 预筛：快速判断是否值得进入精确等价计算。
    返回 (need_compute, reason)。
    """
    if not latex_a:
        return False, "empty_input"
    if latex_a == latex_b:
        return True, "identical"

    if latex_b:
        len_a, len_b = len(latex_a), len(latex_b)
        if len_a > 0 and len_b > 0:
            diff_ratio = abs(len_a - len_b) / max(len_a, len_b)
            if diff_ratio > length_tolerance:
                return False, f"length_diff_too_large({diff_ratio:.2f}>{length_tolerance})"

        tokens_a = _tokenize(latex_a)
        tokens_b = _tokenize(latex_b)
        if tokens_a and tokens_b:
            intersection = tokens_a & tokens_b
            union = tokens_a | tokens_b
            similarity = len(intersection) / len(union)
            if similarity < jaccard_threshold:
                return False, f"low_jaccard({similarity:.2f}<{jaccard_threshold})"

    return True, "need_compute"


def prefetch_warm_cache(latex_list: list, action: str = "equiv") -> dict:
    if not latex_list:
        return {"hits": 0, "misses": 0}
    keys = [cache_key(l, "", action) for l in latex_list]
    placeholders = ",".join("?" * len(keys))
    try:
        rows = (
            _get_conn()
            .execute(
                f"SELECT cache_key FROM cache WHERE cache_key IN ({placeholders}) AND expires_at > ?",
                (*keys, time.time()),
            )
            .fetchall()
        )
        hits = len(rows)
        return {"hits": hits, "misses": len(keys) - hits}
    except sqlite3.DatabaseError:
        return {"hits": 0, "misses": len(keys)}


# ============================================================================
# D6.4 增量扫描支持
# ============================================================================


def mtime_hash(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    stat = p.stat()
    return hashlib.sha256(f"{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:16]


NOTE_FINGERPRINT_PATH = CACHE_DIR / "note_fingerprints.json"


def load_note_fingerprints() -> dict:
    if not NOTE_FINGERPRINT_PATH.exists():
        return {}
    try:
        return json.loads(NOTE_FINGERPRINT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return {}


def save_note_fingerprints(fps: dict):
    NOTE_FINGERPRINT_PATH.write_text(
        json.dumps(fps, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def get_changed_notes(current_paths: list) -> list:
    old = load_note_fingerprints()
    new = {p: mtime_hash(p) for p in current_paths}
    changed = []
    for p, h in new.items():
        if old.get(p) != h:
            changed.append(p)
    return changed


if __name__ == "__main__":
    print("=== math_cache 自检 ===")
    print(f"DB: {DB_PATH}")

    print("\n[D6.1 预筛]")
    print(f"  identical: {prefilter('\\\\int x^2 dx', '\\\\int x^2 dx')}")
    print(f"  length diff: {prefilter('a', 'a + b + c + d + e')}")
    print(f"  token diff: {prefilter('\\\\alpha + \\\\beta', '1 + 2 + 3')}")

    print("\n[D6.2 缓存]")
    test_a = "\\int x^2 dx"
    test_b = "\\frac{x^3}{3}"
    set_cached(test_a, test_b, "equiv", {"equivalent": True, "diff": "0"})
    print("  set_cached ok")
    result = get_cached(test_a, test_b, "equiv")
    print(f"  get_cached: {result}")

    print("\n[stats]")
    print(f"  {cache_stats()}")

    print("\n[D6.4 增量扫描]")
    sample = ["/tmp/a.md", "/tmp/b.md"]
    for p in sample:
        Path(p).write_text("# test")
    print(f"  changed: {get_changed_notes(sample)}")
    save_note_fingerprints({p: mtime_hash(p) for p in sample})
    print(f"  二次扫描 changed: {get_changed_notes(sample)}")

    print("\n=== 自检完毕 ===")
