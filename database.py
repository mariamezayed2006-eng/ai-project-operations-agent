"""SQLite access layer for the AI Project Operations Agent (Project 4).

Only deterministic Python may use this module (via future ``tools.py``).
Agents never receive direct access to it (SDD Sections 5-6, 9).

Tables (SDD Section 5.1): ``projects``, ``tasks``, ``team_members``, ``comments``.
No audit table: the SDD requires auditability of workflow/action/approval
outcomes (Sections 6.3, 9) but defines no ``audit_records`` schema; audit data
is recorded via tool confirmations, session history, and SDK traces instead.

Concurrency model (verified, see tests/test_database.py):

- One workflow-owned connection is shared with SDK tool-execution threads.
- Every public function below holds the reentrant module ``_LOCK`` for its
  whole body, so each call is atomic: single-statement writes commit or
  roll back as a unit, and reads never observe torn state. Partial
  application is impossible (no multi-statement transactions exist).
- Failed writes roll back before raising ``DatabaseError``, so the
  connection stays usable and later calls are unaffected.
- Limitations: direct ``conn.execute`` calls bypass the lock (only tests do
  this today — keep it that way); close a shared connection only when no
  workflow using it is still running (``runtime.request_connection`` and the
  SDK run-loop both guarantee this); separate connections serialize on the
  same process-wide lock (correct, slightly contended — never deadlocked,
  since there is exactly one lock and critical sections never block on I/O).
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "projectops.db"

# The Agents SDK executes sync function tools in worker threads, so one
# workflow-owned connection is routinely shared across threads. CPython
# cursors on a shared connection interleave under concurrency and can
# return WRONG rows silently (observed live: mismatched ids, None rows).
# Every function below therefore runs under this lock; check_same_thread
# permits the cross-thread use, the lock makes it correct. Any residual
# SQLite contention still surfaces as DatabaseError (safe failure).
_LOCK = threading.RLock()

_F = TypeVar("_F", bound=Callable[..., Any])


def _synchronized(fn: _F) -> _F:
    """Run a database function atomically w.r.t. all other such functions."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _LOCK:
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    deadline TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT,
    priority TEXT NOT NULL,
    deadline TEXT,
    completion_percent INTEGER NOT NULL DEFAULT 0 CHECK (completion_percent BETWEEN 0 AND 100),
    blocker INTEGER NOT NULL DEFAULT 0 CHECK (blocker IN (0, 1))
);

CREATE TABLE IF NOT EXISTS team_members (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    availability TEXT NOT NULL,
    current_workload INTEGER NOT NULL DEFAULT 0 CHECK (current_workload >= 0)
);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_memory (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    health TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class DatabaseError(Exception):
    """Raised when a database operation fails."""


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with row-dict access and FK enforcement.

    ``check_same_thread=False`` is required: the Agents SDK executes sync
    function tools in worker threads, so a workflow-owned connection is
    routinely used off-thread. All access through this module's functions
    is additionally serialized by a lock (see ``_LOCK``); direct
    ``conn.execute`` calls from multiple threads are not safe.
    """
    try:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not open database {db_path}: {exc}") from exc


@_synchronized
def init_db(conn: sqlite3.Connection) -> None:
    """Create the Phase 1 tables. Safe to call on an existing database."""
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"Schema init failed: {exc}") from exc


@_synchronized
def table_names(conn: sqlite3.Connection) -> list[str]:
    """Return sorted user-table names (used by tests and reporting)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Best-effort rollback so a failed write never poisons later calls."""
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --- Projects ---


@_synchronized
def insert_project(conn: sqlite3.Connection, project_id: str, name: str, deadline: str) -> None:
    """Insert one project. Raises DatabaseError on duplicates or constraint violations."""
    try:
        conn.execute(
            "INSERT INTO projects (id, name, deadline) VALUES (?, ?, ?)",
            (project_id, name, deadline),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"insert_project failed: {exc}") from exc


@_synchronized
def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    """Return one project by id, or None."""
    try:
        return _row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    except sqlite3.Error as exc:
        raise DatabaseError(f"get_project failed: {exc}") from exc


@_synchronized
def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all projects ordered by id."""
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]
    except sqlite3.Error as exc:
        raise DatabaseError(f"list_projects failed: {exc}") from exc


# --- Tasks ---


@_synchronized
def insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    project_id: str,
    title: str,
    status: str,
    priority: str,
    owner_id: str | None = None,
    deadline: str | None = None,
    completion_percent: int = 0,
    blocker: bool = False,
) -> None:
    """Insert one task. Raises DatabaseError on PK/FK/CHECK violations."""
    try:
        conn.execute(
            """INSERT INTO tasks
               (id, project_id, title, status, owner_id, priority, deadline, completion_percent, blocker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, project_id, title, status, owner_id, priority, deadline, completion_percent, int(blocker)),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"insert_task failed: {exc}") from exc


@_synchronized
def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """Return one task by id, or None when unknown (callers treat as unavailable evidence)."""
    try:
        return _row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    except sqlite3.Error as exc:
        raise DatabaseError(f"get_task failed: {exc}") from exc


@_synchronized
def list_tasks_by_project(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """Return all tasks of a project ordered by id."""
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
        ]
    except sqlite3.Error as exc:
        raise DatabaseError(f"list_tasks_by_project failed: {exc}") from exc


@_synchronized
def update_task_fields(conn: sqlite3.Connection, task_id: str, fields: dict[str, Any]) -> bool:
    """Apply a validated partial update to one task. Returns True when a row changed.

    Only allow-listed columns may be updated; callers (tools.py) validate
    values beforehand. Parameterized SQL only.
    """
    allowed = {"status", "owner_id", "priority", "deadline", "completion_percent", "title", "blocker"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    try:
        set_clause = ", ".join(f"{col} = ?" for col in updates)
        cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", (*updates.values(), task_id))
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"update_task_fields failed: {exc}") from exc


# --- Team members ---


@_synchronized
def get_team_member(conn: sqlite3.Connection, member_id: str) -> dict[str, Any] | None:
    """Return one team member by id (any project), or None when unknown."""
    try:
        return _row_to_dict(conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone())
    except sqlite3.Error as exc:
        raise DatabaseError(f"get_team_member failed: {exc}") from exc


@_synchronized
def insert_team_member(
    conn: sqlite3.Connection,
    member_id: str,
    project_id: str,
    name: str,
    role: str,
    availability: str,
    current_workload: int = 0,
) -> None:
    """Insert one team member. Raises DatabaseError on violations."""
    try:
        conn.execute(
            """INSERT INTO team_members
               (id, project_id, name, role, availability, current_workload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (member_id, project_id, name, role, availability, current_workload),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"insert_team_member failed: {exc}") from exc


@_synchronized
def list_team_members(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """Return all members of a project ordered by id."""
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM team_members WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        raise DatabaseError(f"list_team_members failed: {exc}") from exc


# --- Comments ---


@_synchronized
def insert_comment(
    conn: sqlite3.Connection,
    comment_id: str,
    project_id: str,
    author: str,
    content: str,
    created_at: str,
) -> None:
    """Insert one project comment. Raises DatabaseError on violations."""
    try:
        conn.execute(
            "INSERT INTO comments (id, project_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (comment_id, project_id, author, content, created_at),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"insert_comment failed: {exc}") from exc


@_synchronized
def list_comments(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """Return all comments of a project ordered by id."""
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM comments WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
        ]
    except sqlite3.Error as exc:
        raise DatabaseError(f"list_comments failed: {exc}") from exc


# --- Durable project memory (bonus: continuity beyond one conversation) ---


@_synchronized
def get_project_memory(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    """Return the stored health summary for a project, or None when never recorded."""
    try:
        return _row_to_dict(
            conn.execute("SELECT * FROM project_memory WHERE project_id = ?", (project_id,)).fetchone()
        )
    except sqlite3.Error as exc:
        raise DatabaseError(f"get_project_memory failed: {exc}") from exc


@_synchronized
def upsert_project_memory(
    conn: sqlite3.Connection, project_id: str, summary: str, health: str, updated_at: str
) -> None:
    """Record (or replace) one project's health summary. Raises DatabaseError on violations."""
    try:
        conn.execute(
            """INSERT INTO project_memory (project_id, summary, health, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 summary = excluded.summary, health = excluded.health, updated_at = excluded.updated_at""",
            (project_id, summary, health, updated_at),
        )
        conn.commit()
    except sqlite3.Error as exc:
        _rollback_quietly(conn)
        raise DatabaseError(f"upsert_project_memory failed: {exc}") from exc
