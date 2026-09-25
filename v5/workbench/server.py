"""
v5-VE1-M2 Workbench 后端 · FastAPI + SQLite

单文件服务，提供 5 个模块的 REST API：
  - /api/state       全局状态（版本、能力矩阵）
  - /api/tasks       路线图任务（CRUD）
  - /api/notes       反思笔记（CRUD）
  - /api/metrics     指标面板（CRUD + 时序记录）
  - /api/graph       概念图谱（节点 + 边）

启动：
  python v5/workbench/server.py
  → http://127.0.0.1:9000
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================================
# 配置
# ============================================================================

WORKBENCH_DIR = Path("/home/jiuben/tdx-data-feed/v5/workbench")
DB_PATH = WORKBENCH_DIR / "workbench.db"
STATIC_DIR = WORKBENCH_DIR / "static"
SEED_PATH = WORKBENCH_DIR / "seed.json"

WORKBENCH_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="v5-VE1-M2 Workbench", version="1.0.0")


def _utcnow():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ============================================================================
# SQLite 初始化
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    phase TEXT NOT NULL,
    week TEXT,
    priority INTEGER DEFAULT 3,
    status TEXT DEFAULT 'todo',
    progress INTEGER DEFAULT 0,
    dependencies TEXT,
    due_date TEXT,
    category TEXT,
    roi REAL,
    tags TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    linked_task_id INTEGER,
    reflection_type TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    unit TEXT,
    description TEXT,
    current_value REAL,
    target_value REAL,
    baseline_value REAL,
    display_type TEXT DEFAULT 'card',
    direction TEXT DEFAULT 'up',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS metric_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id INTEGER NOT NULL,
    value REAL NOT NULL,
    recorded_at TEXT,
    note TEXT,
    FOREIGN KEY (metric_id) REFERENCES metrics(id)
);

CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    description TEXT,
    capability_level INTEGER,
    related_metrics TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    label TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.close()


init_db()


# ============================================================================
# Pydantic 模型
# ============================================================================


class TaskIn(BaseModel):
    title: str
    description: str | None = None
    phase: str
    week: str | None = None
    priority: int = 3
    status: str = "todo"
    progress: int = 0
    dependencies: list[int] = []
    due_date: str | None = None
    category: str | None = None
    roi: float | None = None
    tags: list[str] = []


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    phase: str | None = None
    week: str | None = None
    priority: int | None = None
    status: str | None = None
    progress: int | None = None
    dependencies: list[int] | None = None
    due_date: str | None = None
    category: str | None = None
    roi: float | None = None
    tags: list[str] | None = None


class NoteIn(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    linked_task_id: int | None = None
    reflection_type: str | None = None


class NotePatch(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    linked_task_id: int | None = None
    reflection_type: str | None = None


class MetricIn(BaseModel):
    name: str
    category: str | None = None
    unit: str | None = None
    description: str | None = None
    current_value: float | None = None
    target_value: float | None = None
    baseline_value: float | None = None
    display_type: str = "card"
    direction: str = "up"


class MetricPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    unit: str | None = None
    description: str | None = None
    current_value: float | None = None
    target_value: float | None = None
    baseline_value: float | None = None
    display_type: str | None = None
    direction: str | None = None


class MetricRecord(BaseModel):
    value: float
    note: str | None = None


# ============================================================================
# 工具函数
# ============================================================================


def _row_to_task(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["dependencies"] = json.loads(d.get("dependencies") or "[]")
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


def _row_to_note(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


def _row_to_metric(row: sqlite3.Row) -> dict:
    return dict(row)


def _row_to_concept(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["related_metrics"] = json.loads(d.get("related_metrics") or "[]")
    return d


# ============================================================================
# State API
# ============================================================================


@app.get("/api/state")
def get_state():
    conn = get_conn()
    rows = conn.execute("SELECT key, value, updated_at FROM state").fetchall()
    conn.close()
    return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}


@app.put("/api/state/{key}")
def put_state(key: str, request: Request):
    payload = request.json()
    value = payload.get("value", "")
    conn = get_conn()
    conn.execute(
        "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), _utcnow()),
    )
    conn.close()
    return {"ok": True, "key": key, "value": value}


# ============================================================================
# Tasks API
# ============================================================================


@app.get("/api/tasks")
def list_tasks(phase: str | None = None, status: str | None = None, week: str | None = None):
    conn = get_conn()
    sql = "SELECT * FROM tasks WHERE 1=1"
    args = []
    if phase:
        sql += " AND phase = ?"
        args.append(phase)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if week:
        sql += " AND week = ?"
        args.append(week)
    sql += " ORDER BY priority ASC, week ASC, id ASC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [_row_to_task(r) for r in rows]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "task not found")
    return _row_to_task(row)


@app.post("/api/tasks")
def create_task(task: TaskIn):
    conn = get_conn()
    now = _utcnow()
    cur = conn.execute(
        """INSERT INTO tasks
           (title, description, phase, week, priority, status, progress,
            dependencies, due_date, category, roi, tags, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task.title,
            task.description,
            task.phase,
            task.week,
            task.priority,
            task.status,
            task.progress,
            json.dumps(task.dependencies),
            task.due_date,
            task.category,
            task.roi,
            json.dumps(task.tags),
            now,
            now,
        ),
    )
    conn.close()
    return {"id": cur.lastrowid, "ok": True}


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, patch: TaskPatch):
    fields = {k: v for k, v in patch.dict(exclude_none=True).items()}
    if not fields:
        return {"ok": True, "updated": 0}
    if "dependencies" in fields:
        fields["dependencies"] = json.dumps(fields["dependencies"])
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"])
    fields["updated_at"] = _utcnow()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = [*list(fields.values()), task_id]
    conn = get_conn()
    cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    conn.close()
    return {"ok": True, "updated": cur.rowcount}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_conn()
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.close()
    return {"ok": True, "deleted": cur.rowcount}


# ============================================================================
# Notes API
# ============================================================================


@app.get("/api/notes")
def list_notes(
    tag: str | None = None, linked_task: int | None = None, reflection_type: str | None = None
):
    conn = get_conn()
    sql = "SELECT * FROM notes WHERE 1=1"
    args = []
    if tag:
        sql += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    if linked_task:
        sql += " AND linked_task_id = ?"
        args.append(linked_task)
    if reflection_type:
        sql += " AND reflection_type = ?"
        args.append(reflection_type)
    sql += " ORDER BY updated_at DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [_row_to_note(r) for r in rows]


@app.post("/api/notes")
def create_note(note: NoteIn):
    conn = get_conn()
    now = _utcnow()
    cur = conn.execute(
        """INSERT INTO notes (title, content, tags, linked_task_id,
                             reflection_type, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            note.title,
            note.content,
            json.dumps(note.tags),
            note.linked_task_id,
            note.reflection_type,
            now,
            now,
        ),
    )
    conn.close()
    return {"id": cur.lastrowid, "ok": True}


@app.patch("/api/notes/{note_id}")
def patch_note(note_id: int, patch: NotePatch):
    fields = {k: v for k, v in patch.dict(exclude_none=True).items()}
    if not fields:
        return {"ok": True, "updated": 0}
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"])
    fields["updated_at"] = _utcnow()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = [*list(fields.values()), note_id]
    conn = get_conn()
    cur = conn.execute(f"UPDATE notes SET {set_clause} WHERE id = ?", values)
    conn.close()
    return {"ok": True, "updated": cur.rowcount}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int):
    conn = get_conn()
    cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.close()
    return {"ok": True, "deleted": cur.rowcount}


# ============================================================================
# Metrics API
# ============================================================================


@app.get("/api/metrics")
def list_metrics(category: str | None = None):
    conn = get_conn()
    sql = "SELECT * FROM metrics WHERE 1=1"
    args = []
    if category:
        sql += " AND category = ?"
        args.append(category)
    sql += " ORDER BY category, id"
    rows = conn.execute(sql, args).fetchall()
    metrics = [_row_to_metric(r) for r in rows]
    for m in metrics:
        recs = conn.execute(
            "SELECT value, recorded_at, note FROM metric_records "
            "WHERE metric_id = ? ORDER BY recorded_at DESC LIMIT 30",
            (m["id"],),
        ).fetchall()
        m["records"] = [dict(r) for r in recs]
    conn.close()
    return metrics


@app.post("/api/metrics")
def create_metric(m: MetricIn):
    conn = get_conn()
    now = _utcnow()
    cur = conn.execute(
        """INSERT INTO metrics
           (name, category, unit, description, current_value, target_value,
            baseline_value, display_type, direction, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            m.name,
            m.category,
            m.unit,
            m.description,
            m.current_value,
            m.target_value,
            m.baseline_value,
            m.display_type,
            m.direction,
            now,
            now,
        ),
    )
    if m.current_value is not None:
        conn.execute(
            "INSERT INTO metric_records (metric_id, value, recorded_at) VALUES (?, ?, ?)",
            (cur.lastrowid, m.current_value, now),
        )
    conn.close()
    return {"id": cur.lastrowid, "ok": True}


@app.patch("/api/metrics/{metric_id}")
def patch_metric(metric_id: int, patch: MetricPatch):
    fields = {k: v for k, v in patch.dict(exclude_none=True).items()}
    if not fields:
        return {"ok": True, "updated": 0}
    fields["updated_at"] = _utcnow()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = [*list(fields.values()), metric_id]
    conn = get_conn()
    cur = conn.execute(f"UPDATE metrics SET {set_clause} WHERE id = ?", values)
    conn.close()
    return {"ok": True, "updated": cur.rowcount}


@app.post("/api/metrics/{metric_id}/record")
def record_metric(metric_id: int, rec: MetricRecord):
    conn = get_conn()
    now = _utcnow()
    cur = conn.execute(
        "INSERT INTO metric_records (metric_id, value, recorded_at, note) VALUES (?, ?, ?, ?)",
        (metric_id, rec.value, now, rec.note),
    )
    conn.execute(
        "UPDATE metrics SET current_value=?, updated_at=? WHERE id=?",
        (rec.value, now, metric_id),
    )
    conn.close()
    return {"id": cur.lastrowid, "ok": True}


@app.delete("/api/metrics/{metric_id}")
def delete_metric(metric_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM metric_records WHERE metric_id = ?", (metric_id,))
    cur = conn.execute("DELETE FROM metrics WHERE id = ?", (metric_id,))
    conn.close()
    return {"ok": True, "deleted": cur.rowcount}


# ============================================================================
# Concepts / Graph API
# ============================================================================


@app.get("/api/concepts")
def list_concepts(category: str | None = None):
    conn = get_conn()
    sql = "SELECT * FROM concepts WHERE 1=1"
    args = []
    if category:
        sql += " AND category = ?"
        args.append(category)
    sql += " ORDER BY category, id"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [_row_to_concept(r) for r in rows]


@app.get("/api/graph")
def get_graph():
    conn = get_conn()
    concepts = conn.execute("SELECT * FROM concepts").fetchall()
    edges = conn.execute("SELECT * FROM edges").fetchall()
    conn.close()
    return {
        "nodes": [_row_to_concept(r) for r in concepts],
        "edges": [dict(r) for r in edges],
    }


# ============================================================================
# Seed / Stats
# ============================================================================


@app.post("/api/seed")
def seed_data():
    if not SEED_PATH.exists():
        raise HTTPException(404, "seed.json not found")
    with open(SEED_PATH, encoding="utf-8") as f:
        seed = json.load(f)
    conn = get_conn()
    for tbl in ("state", "tasks", "notes", "metrics", "metric_records", "concepts", "edges"):
        conn.execute(f"DELETE FROM {tbl}")
    now = _utcnow()
    for k, v in seed.get("state", {}).items():
        conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            (k, str(v), now),
        )
    for t in seed.get("tasks", []):
        conn.execute(
            """INSERT INTO tasks
               (title, description, phase, week, priority, status, progress,
                dependencies, due_date, category, roi, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["title"],
                t.get("description", ""),
                t["phase"],
                t.get("week"),
                t.get("priority", 3),
                t.get("status", "todo"),
                t.get("progress", 0),
                json.dumps(t.get("dependencies", [])),
                t.get("due_date"),
                t.get("category"),
                t.get("roi"),
                json.dumps(t.get("tags", [])),
                now,
                now,
            ),
        )
    for n in seed.get("notes", []):
        conn.execute(
            """INSERT INTO notes (title, content, tags, linked_task_id,
                                 reflection_type, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                n["title"],
                n["content"],
                json.dumps(n.get("tags", [])),
                n.get("linked_task_id"),
                n.get("reflection_type"),
                now,
                now,
            ),
        )
    for m in seed.get("metrics", []):
        conn.execute(
            """INSERT INTO metrics
               (name, category, unit, description, current_value, target_value,
                baseline_value, display_type, direction, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                m["name"],
                m.get("category"),
                m.get("unit"),
                m.get("description"),
                m.get("current_value"),
                m.get("target_value"),
                m.get("baseline_value"),
                m.get("display_type", "card"),
                m.get("direction", "up"),
                now,
                now,
            ),
        )
    for c in seed.get("concepts", []):
        conn.execute(
            """INSERT INTO concepts (name, category, description, capability_level,
                                     related_metrics, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                c["name"],
                c.get("category"),
                c.get("description"),
                c.get("capability_level", 1),
                json.dumps(c.get("related_metrics", [])),
                now,
            ),
        )
    for e in seed.get("edges", []):
        conn.execute(
            """INSERT INTO edges (source_id, target_id, relation, weight, label)
               VALUES (?, ?, ?, ?, ?)""",
            (
                e["source_id"],
                e["target_id"],
                e["relation"],
                e.get("weight", 1.0),
                e.get("label", ""),
            ),
        )
    conn.close()
    return {
        "ok": True,
        "seeded": {
            "state": len(seed.get("state", {})),
            "tasks": len(seed.get("tasks", [])),
            "notes": len(seed.get("notes", [])),
            "metrics": len(seed.get("metrics", [])),
            "concepts": len(seed.get("concepts", [])),
            "edges": len(seed.get("edges", [])),
        },
    }


@app.get("/api/stats")
def stats():
    conn = get_conn()
    return {
        "tasks": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
        "notes": conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"],
        "metrics": conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()["c"],
        "concepts": conn.execute("SELECT COUNT(*) c FROM concepts").fetchone()["c"],
        "edges": conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"],
    }


# ============================================================================
# 静态文件
# ============================================================================

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return {"error": "index.html not found"}
    return FileResponse(str(idx))


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print(f"v5-VE1-M2 Workbench starting | DB={DB_PATH}")
    print("Open: http://127.0.0.1:9000")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="info")
