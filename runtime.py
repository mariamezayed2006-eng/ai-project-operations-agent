"""Runtime integration for the AI Project Operations Agent (Project 4).

Final phase: owns per-request lifecycle without touching safety behavior.

- ``request_connection``: opens a workflow-owned SQLite connection per
  request and always closes it (never a global connection).
- ``AppContext``: plain frozen data (user/project/role) forwarded to the
  SDK as ``context``; it never carries a live connection.
- ``get_session``: file-backed ``SQLiteSession`` for conversation
  continuity (``sessions.db``); one id per end user / browser session.
- ``run_health_update`` / ``run_coordination``: traced entrypoints used by
  the UI and demo layers. Tracing wraps WF-01/WF-02 inside ``workflows.py``
  itself (SDD Section 8.4); these helpers add session/context plumbing.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import agents as project_agents  # noqa: F401  (registers the real SDK as `agents_sdk`)
import agents_sdk
import database

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = database.DEFAULT_DB_PATH
SESSIONS_DB_PATH = REPO_ROOT / "sessions.db"


@dataclass(frozen=True)
class AppContext:
    """Per-request context: identity only, no connections, no secrets."""

    user_id: str
    project_id: str
    role: str | None = None


@contextmanager
def request_connection(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Yield a request-scoped connection; always closed afterwards."""
    conn = database.get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def new_session_id() -> str:
    """Mint an opaque session id for a new end user / browser session."""
    return uuid.uuid4().hex


def scope_session_id(user_id: str, project_id: str, session_id: str) -> str:
    """Namespace a raw session id per user and project.

    SDK sessions are keyed by id alone, so sharing one id across projects
    would bleed conversational context between them. Scoping keeps each
    (user, project) history isolated while preserving multi-turn continuity
    inside it. Pure string function — no I/O, fully testable.
    """
    return f"{user_id}:{project_id}:{session_id}"


def get_session(session_id: str, db_path: str | Path = SESSIONS_DB_PATH) -> Any:
    """Return the file-backed SDK session for this id (created on demand)."""
    return agents_sdk.SQLiteSession(session_id, db_path=db_path)


def resolve_role(conn: sqlite3.Connection, project_id: str, user_id: str) -> str | None:
    """Look up a user's stored role for display/context (never from user text)."""
    member = database.get_team_member(conn, user_id)
    if member is not None and member["project_id"] == project_id:
        return str(member["role"])
    return None


def list_projects(db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """List projects for UI selectors."""
    with request_connection(db_path) as conn:
        return database.list_projects(conn)


def list_members(db_path: str | Path, project_id: str) -> list[dict[str, Any]]:
    """List team members of a project for UI selectors."""
    with request_connection(db_path) as conn:
        return database.list_team_members(conn, project_id)


async def run_health_update(
    db_path: str | Path,
    *,
    session_id: str,
    user_id: str,
    project_id: str,
    message: str,
) -> Any:
    """Traced WF-01 with session continuity and request context."""
    import workflows

    session = get_session(scope_session_id(user_id, project_id, session_id))
    with request_connection(db_path) as conn:
        context = AppContext(user_id=user_id, project_id=project_id, role=resolve_role(conn, project_id, user_id))
        return await workflows.health_update(conn, project_id, user_id, message, session, context)


async def run_coordination(
    db_path: str | Path,
    store: Any,
    *,
    session_id: str,
    user_id: str,
    project_id: str,
    action_type: str,
    task_id: str | None = None,
    values: dict[str, Any] | None = None,
) -> Any:
    """Traced WF-02 with a caller-owned approval store (process-scoped in the UI)."""
    import workflows

    _ = get_session(scope_session_id(user_id, project_id, session_id))  # continuity side effect
    with request_connection(db_path) as conn:
        return await workflows.coordinate_task(conn, project_id, user_id, action_type, task_id, values, store)
